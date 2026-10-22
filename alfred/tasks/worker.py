"""One run of a background task: a Claude Code session in auto mode (alfred.llm.claude.auto_mode).

  code_change    edits the project folder in place (uncommitted), or a worktree + branch when in_place=false
  code_question  answers about the live checkout
  research       web plus account tools (reads, Sheets and Docs edits) and connections; works in data/tasks/ID

Every task gets Claude Code's tools and subagents, the user's own Claude Code setup and the project's, and nothing is
sandboxed: auto mode's check clears each action. AutoGate is reached only by ask rules (each change through a
connection) and when auto mode stops to ask; either way the user answers on their phone. A task working in the user's
own checkout can't push, reset or switch branches there. A read-only task (a carried-on lookup) can't edit files or
change anything through its connections. The CLI's env has .env values blanked.
GitLab connections also get repo_files, which fetches whole files and projects with the connection's key. Tasks never
write Alfred's data files; they propose edits. Granted access (alfred.tasks.access: accounts, artifacts) shapes
`_options`. Alfred's enabled skills load as a plugin; Alfred's helper subagents sit beside Claude Code's own. A run
doesn't end while background subagents run, nor without a final report. Its token use, per model and subagent, is
reported however it ends (alfred.tasks.tokens).
"""

import asyncio
import logging
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from alfred import projects
from alfred.config import DATA_DIR, SCHEDULE_EFFORT, TASK_ANSWER_EFFORT, TASK_EFFORT, TASK_MAX_TURNS
from alfred.integrations import mcp_servers
from alfred.llm import claude_logins, retry, usage
from alfred.llm.claude import AUTO_DENIED, FIVE_MINUTE_CACHE, LimitWatch, Spend, auto_mode
from alfred.llm.roles import Role
from alfred.llm.sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    RateLimitEvent,
    ResultMessage,
    SystemMessage,
    TaskNotificationMessage,
    TaskStartedMessage,
    TaskUpdatedMessage,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)
from alfred.safety import host
from alfred.skills import home as skills
from alfred.tasks import access, repo_files, tokens
from alfred.tasks.briefs import _system_prompt
from alfred.tasks.connection_calls import ConnectionGate
from alfred.tasks.connection_watch import Watch
from alfred.tasks.gate import _FILE_ARG, _IN_PLACE_DENIED, _WRITES, ARTIFACT, AutoGate
from alfred.tasks.reporting import (
    PAUSES,
    REQUEST_ACCESS,
    Reporter,
    _nudge_hook,
    _task_server,
    answer,
    nudge_text,
    with_nudges,  # noqa: F401
)
from alfred.tasks.subagents import _AGENT_TOOLS, RESEARCHER, SEARCHER, TESTER, Subagents, _agents, own_models, result_text  # noqa: F401

log = logging.getLogger("alfred.tasks.worker")


@dataclass(frozen=True)
class SessionTools:
    """Alfred's own tools for a task's session, and the session's options with tool results screened. They sit above
    the tasks (alfred.brain.sdk), so each process hands them in at start (alfred.bootstrap.wire)."""
    server: Callable[..., tuple[dict, list[str]]]
    options: Callable[..., ClaudeAgentOptions]


_session_tools: SessionTools | None = None


def use(tools: SessionTools) -> None:
    global _session_tools
    _session_tools = tools


def _wired() -> SessionTools:
    if _session_tools is None:
        raise RuntimeError("Task sessions aren't wired to Alfred's tools: the process must call alfred.bootstrap.wire()")
    return _session_tools

READ_ONLY_TOOLS = ["calendar_events", "calendar_event", "calendar_free_time", "gmail_search", "gmail_read",
                   "sheets_search", "sheets_read", "docs_search", "docs_read", "slides_search", "slides_read",
                   "drive_search", "chat_unread", "chat_spaces", "chat_read", "chat_search", "gmail_attachments",
                   "gmail_attachment_read", "gmail_attachment_transcribe"]
# Sheets and Docs edits, as the voice conversation has them; deletes and sends need a spoken yes, so stay out
GOOGLE_EDIT_TOOLS = ["sheets_write", "sheets_append", "sheets_format", "sheets_add_tab", "docs_insert",
                     "docs_replace_text", "docs_format", "docs_beautify", "docs_insert_image"]


@dataclass
class Outcome:
    result: str  # final message
    error: str  # "" on success
    cost: float
    kind: str = ""  # failure kind (alfred.llm.retry)
    resets: float | None = None  # when the usage limit it hit resets, if Claude said


ARTIFACT_ENV = {"CLAUDE_CODE_ARTIFACT": "1"}  # undocumented; the CLI omits Artifact under the SDK without it


def _blank_env() -> dict[str, str]:
    """Blank every .env key (except CLAUDE_*) plus MCP secrets and the vault key, so commands can't inherit them.

    The MCP launcher reads the vault key from .env itself.
    """
    return {k: "" for k in host.env_keys() if not k.startswith("CLAUDE_")} | mcp_servers.session_env()


def _on_demand(server: dict) -> dict:
    """An in-process server whose tools are listed by name only until the session loads one with ToolSearch, while
    tool search is on (else loaded up front: with search off, a deferred tool could never load)."""
    return server | {"alwaysLoad": False} if mcp_servers.ON_DEMAND else server


def role(task: dict) -> Role:
    """The role a task's session runs on: scheduled non-code runs the schedule's, research its own, else the task's."""
    if task.get("job") is not None and task["kind"] != "code_change":
        return Role.SCHEDULE
    return Role.RESEARCH if task["kind"] == "research" else Role.TASK


def _model(task: dict) -> tuple[str, str]:
    """(model, effort): tasks that change no code get the lighter effort; usage.available falls back if a limit is out."""
    if (on := role(task)) == Role.SCHEDULE:
        return usage.available(on), SCHEDULE_EFFORT
    return usage.available(on), TASK_EFFORT if task["kind"] == "code_change" else TASK_ANSWER_EFFORT


def _options(task: dict, reporter: Reporter, live: Watch | None = None) -> ClaudeAgentOptions:
    """The task session's options: Claude Code's tools, subagents and setup, the user's and the project's, with no
    sandbox, auto mode's check clearing each action. Alfred's reporting tools, and with accounts its Google tools and
    connections, are added; a connection's changes and whatever auto mode stops to ask about go to the user's phone.
    `live` keeps its connections current while it runs."""
    live = live or Watch()
    kind, can, read_only = task["kind"], access.has(task), bool(task.get("read_only"))
    own = DATA_DIR / "tasks" / str(task["id"])
    cwd = Path(task["worktree"]) if kind == "code_change" else projects.find(task["project"]).path if kind == "code_question" else own
    if kind != "code_change":  # a code change's own folder is its worktree, made already
        own.mkdir(parents=True, exist_ok=True)
    own = cwd if kind == "code_change" else own
    env: dict[str, str] = _blank_env() | FIVE_MINUTE_CACHE  # a task's calls follow within minutes; subagents' too
    mcp = {"task": _task_server(task["id"], reporter)}
    allowed = ["mcp__task__report_progress", "mcp__task__ask_user", "mcp__task__propose_data_edit", REQUEST_ACCESS]
    denied = AUTO_DENIED + (_IN_PLACE_DENIED if kind == "code_change" and task["in_place"] else [])
    denied += sorted(_WRITES) if read_only else []
    ask: list[str] = []
    connections = None
    if "accounts" in can:
        # Reporting tools load up front; the many account tools wait for ToolSearch
        own_tools, alfred_allowed = _wired().server(READ_ONLY_TOOLS + ([] if read_only else GOOGLE_EDIT_TOOLS))
        mcp["alfred"] = _on_demand(own_tools)
        servers = mcp_servers.session(mcp_servers.for_research(), ask_own=True)
        live.use(servers.servers)
        mcp |= servers.configs
        allowed += alfred_allowed + servers.allowed
        denied += servers.blocked
        changes = sorted(s.tool(t) for s in servers.servers for t in s.named("write", "own"))
        if read_only:  # reads only: not even asked about
            denied += changes
        else:  # each change waits for the user's yes
            connections = ConnectionGate(task["id"], servers.servers, reporter)
            ask += changes
        if found := repo_files.gitlab(servers.servers):  # whole files and projects, with the connection's own key
            mcp[repo_files.NAME] = _on_demand(repo_files.repo_server(task["id"], found))
            allowed += repo_files.TOOLS
    if "artifacts" in can:
        env |= ARTIFACT_ENV
        allowed.append(ARTIFACT)
    else:
        denied.append(ARTIFACT)
    # Alfred's enabled skills, as a per-run plugin, beside the user's and the project's own; on a machine, the ones
    # the main computer sent ("skills")
    mine_at = DATA_DIR / "tasks" / str(task["id"]) / "alfred-skills"  # never in the project's own folder
    mine = skills.plugin(mine_at, skills.shipped(task["skills"]) if "skills" in task else None)
    helpers = _agents(kind)
    model, effort = _model(task)
    # After each call: the user's nudges, and connections restarted since
    after = _nudge_hook(task["id"], reporter) + (live.told if live.running else [])
    # Its own login, or the next one if that's passed over; a new session where there's room (claude_logins.starting)
    login = claude_logins.resume_on(task["session_id"])
    return _wired().options(
        env=env,
        cwd=str(cwd),
        system_prompt=_system_prompt(task, cwd, own, can, mine),
        model=model,
        effort=effort,
        allowed_tools=allowed,
        disallowed_tools=denied,
        agents=helpers,  # Alfred's helpers, beside Claude Code's own and the user's
        plugins=[{"type": "local", "path": str(mine_at)}] if mine else [],
        mcp_servers=mcp,
        can_use_tool=AutoGate(task["id"], cwd, reporter, connections),
        hooks={"PreToolUse": own_models(helpers) + (live.hook if live.running else [])}
        | dict.fromkeys(("PostToolUse", "PostToolUseFailure"), after),
        max_turns=TASK_MAX_TURNS,
        resume=task["session_id"],
        login=claude_logins.starting() if login is None else login,
        stderr=lambda line: log.debug("task %s claude: %s", task["id"], line),
        **auto_mode(ask),
    )


def _describe(block: ToolUseBlock) -> str | None:
    """Short speakable activity for a tool call, or None."""
    name, args = block.name, block.input
    if name == "Bash":
        return f"running {args.get('command', '')[:80]}"
    if name in ("Grep", "Glob"):
        return "searching the code"
    if name in _FILE_ARG:
        return f"{'editing' if name in _WRITES else 'reading'} {Path(args.get(_FILE_ARG[name]) or '').name}"
    if name == "WebSearch":
        return f"searching the web for {args.get('query', '')[:60]}"
    if name == "WebFetch":
        return "reading a web page"
    if name == ARTIFACT and (args.get("action") or "publish") == "publish":
        return f"publishing {Path(args.get('file_path') or '').name} to claude.ai"
    if name.startswith("mcp__alfred__"):
        return f"using {name.removeprefix('mcp__alfred__')}"
    if name.startswith("mcp__") and name.count("__") >= 2:
        server, _, tool = name.removeprefix("mcp__").partition("__")
        return f"using {server}: {tool}"
    return None  # incl. Agent: Subagents reports its start


STEPS_OUT = "it ran out of steps"  # error of a run that hit TASK_MAX_TURNS; the runner carries it on


def _error(msg: ResultMessage, limits: LimitWatch, api_error: str | None) -> tuple[str, str]:
    """(reason, retry kind) for a failed result; ("", "") on success."""
    if not msg.is_error:
        return "", ""
    if msg.subtype == "error_max_turns":
        return STEPS_OUT, retry.ERROR
    text = msg.result or "; ".join(str(e) for e in msg.errors or []) or msg.subtype
    limit = limits.describe(f"{api_error or ''} {text}")  # first: it clears rejections whose window has reset
    kind = retry.result_kind(msg.api_error_status, msg.terminal_reason, limits.rejected is not None, api_error)
    return (f"it hit {limit}" if limit else text[:200]), kind


# A turn may end with background subagents still running: wait up to SUBAGENT_WAIT for them, then up to WAKE_GRACE
# for the CLI to start a turn with their reports, else prompt the session ourselves.
SUBAGENT_WAIT = 3600.0
WAKE_GRACE = 30.0
SUBAGENTS_DONE = ("Your background subagents have all finished; their reports are above. Check what they found, "
                  "finish the task, and end with your summary as before.")
REPORT_MISSING = ("You ended without a summary. Write it now: its first line one plain sentence Alfred will say "
                  "aloud, the details on the following lines, covering the whole task.")


class _Ended(Exception):
    """Session stream ended, or the wait for a message timed out."""


async def run(task: dict, prompt: str, reporter: Reporter) -> Outcome:
    """Send `prompt` to the task's session (resuming if any) and wait for its final report.

    Finishes only once all subagents have ended and a report exists (unless it asked a question or failed). Its
    token use is reported however it ends, stopped included.
    """
    tally = tokens.Tally()
    ended = tokens.STOPPED  # unless it returns or raises
    try:
        outcome = await _run(task, prompt, reporter, tally)
        ended = tokens.FAILED if outcome.error else tokens.FINISHED
        return outcome
    except Exception:
        ended = tokens.FAILED
        raise
    finally:
        if rows := tally.rows():
            try:
                reporter.tokens(task["id"], rows, ended)
            except Exception as e:  # never in the way of the run's own ending
                log.warning("task %s: couldn't record its token use: %s", task["id"], e)


async def _run(task: dict, prompt: str, reporter: Reporter, tally: tokens.Tally) -> Outcome:
    asked = False
    turn_began = False  # a new turn has started
    api_error: str | None = None  # AssistantMessage.error on the latest main-session message
    limits = LimitWatch(f"task {task['id']}")
    spend = Spend()
    helpers = Subagents(task["id"], reporter)

    def handle(msg) -> ResultMessage | None:
        nonlocal asked, turn_began, api_error
        reporter.alive(task["id"], msg)
        if isinstance(msg, TaskStartedMessage):
            helpers.task_started(msg)
        elif isinstance(msg, TaskNotificationMessage):  # background subagent finished
            helpers.ended(msg.tool_use_id, msg.status != "completed")
        elif isinstance(msg, TaskUpdatedMessage):  # sometimes the only end signal
            helpers.task_updated(msg)
        elif isinstance(msg, SystemMessage) and msg.subtype == "init" and msg.data.get("session_id"):
            reporter.session(task["id"], msg.data["session_id"])
        elif isinstance(msg, AssistantMessage):
            turn_began = True
            spend.saw(msg)
            tally.saw(msg, helpers.who(msg.parent_tool_use_id))
            if msg.parent_tool_use_id is None:
                api_error = msg.error
            for block in msg.content:
                if isinstance(block, ToolUseBlock):
                    if msg.parent_tool_use_id is None:  # main session, not a subagent
                        asked |= block.name in PAUSES
                        if block.name in _AGENT_TOOLS:
                            helpers.started(block)
                    if what := _describe(block):
                        reporter.activity(task["id"], helpers.label(msg.parent_tool_use_id, what))
        elif isinstance(msg, UserMessage) and msg.parent_tool_use_id is None:
            turn_began = True
            for block in msg.content if isinstance(msg.content, list) else []:
                if isinstance(block, ToolResultBlock):
                    helpers.result(block)
        elif isinstance(msg, RateLimitEvent):
            limits.saw(msg)
        elif isinstance(msg, ResultMessage):
            tally.result(msg)
            return msg
        return None

    result: ResultMessage | None = None
    live = Watch()
    async with ClaudeSDKClient(limits.runs_on(_options(task, reporter, live))) as client:
        inbox: asyncio.Queue = asyncio.Queue()

        async def pump() -> None:
            try:
                async for msg in client.receive_messages():
                    inbox.put_nowait(msg)
            finally:
                inbox.put_nowait(None)  # end of stream

        async def read(timeout: float | None = None, until: Callable[[], bool] = lambda: False) -> ResultMessage | None:
            """Handle messages until a result (returned) or `until()` (None). _Ended on stream end or timeout."""
            deadline = None if timeout is None else time.monotonic() + timeout
            while not until():
                try:
                    msg = await asyncio.wait_for(inbox.get(), None if deadline is None else max(0.0, deadline - time.monotonic()))
                except TimeoutError:
                    raise _Ended("timeout") from None
                if msg is None:
                    raise _Ended("ended")
                if (got := handle(msg)) is not None:
                    return got
            return None

        async def prompt_again(text: str) -> None:
            nonlocal turn_began
            turn_began = False
            await client.query(text)

        reader = asyncio.create_task(pump())
        watching = asyncio.create_task(live.run(client))  # a connection changed mid-run is brought up to date
        reminded = False
        try:
            await client.query(prompt)
            result = await read()
            while not result.is_error:
                if helpers.waiting:
                    # Turn ended with subagents still running (even after a question): wait for them, then for the
                    # CLI-started turn carrying their reports.
                    turn_began = False
                    try:
                        if (again := await read(SUBAGENT_WAIT, until=lambda: not helpers.waiting)) is not None:
                            result = again
                            continue
                        if (again := await read(WAKE_GRACE, until=lambda: turn_began)) is None:
                            again = await read()  # CLI woke it: wait for that result
                    except _Ended as e:
                        if str(e) != "timeout" or helpers.waiting:
                            raise
                        if asked:  # the answer will bring the reports
                            break
                        await prompt_again(SUBAGENTS_DONE)  # no new turn: wake it ourselves
                        again = await read()
                    result = again
                    continue
                if asked:
                    break
                # Late nudges become one more prompt (a question or failure keeps them for the next reply).
                if late := await answer(reporter.take_nudges(task["id"])):
                    await prompt_again(nudge_text(late, finished=True))
                elif not (result.result or "").strip() and not reminded:
                    reminded = True
                    await prompt_again(REPORT_MISSING)  # once; a second silence fails the run
                else:
                    break
                result = await read()
        except _Ended as e:
            if waiting := helpers.waiting:
                who = ", ".join(waiting)
                why = (f"its subagents ({who}) were still running after {SUBAGENT_WAIT / 60:g} minutes"
                       if str(e) == "timeout" else f"the session ended while its subagents ({who}) were still running")
            else:
                why = "the session ended before its final report"
            kind = retry.ERROR if waiting else retry.PASSING  # e.g. CLI died mid-run: transient
            reported = (result.total_cost_usd or 0) if result else 0
            return Outcome((result.result or "") if result else "", why, spend.total(reported), kind)
        finally:
            reader.cancel()
            watching.cancel()
            with suppress(asyncio.CancelledError):
                await reader
            with suppress(asyncio.CancelledError):
                await watching
    reporter.session(task["id"], result.session_id)
    error, kind = _error(result, limits, api_error)
    if not error and not asked and not (result.result or "").strip():
        error, kind = "it ended without a final report", retry.ERROR
    resets = limits.rejected.resets_at if kind == retry.LIMIT and limits.rejected else None
    return Outcome(result.result or "", error, spend.total(result.total_cost_usd or 0), kind, resets)
