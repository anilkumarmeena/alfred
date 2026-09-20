"""One run of a background task: a Claude Code session limited to what its kind allows.

  code_change    edits the project's own folder in place, leaving the changes uncommitted; for projects
                 set to worktrees (in_place false), a git worktree of the project on the task's own branch instead
  code_question  reads the project's live checkout; the only shell commands are read-only git ones
  research       web search plus JARVIS's read-only tools (mail, calendar, Google Sheets, Docs, Slides and Drive), and
                 the MCP servers marked for research (GitHub, GitLab, whatever's connected), started read-only
                 (jarvis.integrations.mcp_servers)

Every file tool passes through `TaskGate`: reads stay inside the task's folders, the user's
projects (TASK_READ_DIRS) and JARVIS's voice session logs, and edits inside the worktree.
Secrets are hidden from both the file tools and the sandbox (`_secret`, `_secret_rules`).
code_change shell commands run in Claude Code's sandbox, which lets them write only to the
worktree and the shared cache folder, and reach any host (`OPEN_NETWORK`); a Bash command that
asks to leave the sandbox is denied. What needs the Mac itself (a signed Xcode build, devicectl,
Homebrew) goes through the host tools instead (`_host_server`): host_run for a command of up to
10 minutes, start_job, job_status and job_wait for longer ones. They run in the runner, under
jarvis.safety.host's Seatbelt profile and without JARVIS's keys in their environment, and each call
meets `_host_gate` first: a command naming a secret or JARVIS's controls is refused
(commands.refused), an ordinary one runs, and a risky one (commands.risky) waits for the user's
yes, which the runner asks for (Reporter.request_approval) by push and by voice; once it's given,
the identical command runs, in the run it resumes. One the user has said to allow for the task's session or always
runs under that rule (jarvis.safety.remembered), and one the user's own words sent with the task order ("merge and
push") runs as they said, both of which Reporter.cleared checks too. Deleting only the task's own files (in its folder
or its temp folder, `task_temp`) and stopping only processes it started (this command's $!, or those in the
sessions its host commands and jobs ran in, `TaskJobs.ran`) run without asking, where the command
itself proves it (commands.Own). An approved package install that runs its steps in a macOS sandbox of its own
(brew install, commands.own_sandbox) runs outside the Seatbelt profile, since macOS won't start one sandbox inside
another; the line may hold nothing but the install. Sessions don't commit: the runner commits worktree code_change
work when a run ends; in-place work stays uncommitted.

The CLI itself gets none of .env's values either: they're blanked in its environment (`_blank_env`),
so a sandboxed command can't read them.

No task can write JARVIS's own data files (the project list, the to-do list, memory). It
suggests edits to them with propose_data_edit instead, which the runner keeps on the task for
JARVIS to apply or dismiss (jarvis.tasks.data_edits).

Messages sent to a running task (nudges) reach the session after its next tool call, through
a PostToolUse hook. Any still waiting when it ends its turn go to it as one more prompt.

Each session can hand side jobs to its subagents (the Agent tool), on cheaper models: a
read-only code searcher for code tasks, a test runner for code_change, and a web researcher
for research. Their tool calls meet the same gate and sandbox, and the gate refuses any
edit from them. Their starts and ends go in the task's log, and what they're doing shows as
its activity. A run doesn't end while any is still running in the background, nor without a
final report (`run`).

code_change and research tasks can also publish pages to claude.ai with Claude Code's Artifact tool (TASK_ARTIFACTS),
which the bundled CLI only loads with CLAUDE_CODE_ARTIFACT=1 set. A publish sends a local file off the Mac, so the gate
(`TaskGate.artifact`) lets it publish only files in the task's own folder, never secrets, and only publish, read and
list: no deleting or sharing. Research gets Write in its own folder for its pages, and nothing else to write with.

A task can be granted more than its kind has (jarvis.tasks.access), when it starts or on a reply, and asks for what it
lacks with request_access, which pauses it like a question. Granted, it's resumed in the same session with options
built from its kind and its grants (`_options`): artifacts, sandboxed shell commands, the host tools, edits in the
user's other projects (`TaskGate`'s `more_writes`, never secrets or JARVIS's own files, `_jarvis_own`), or the
read-only account tools. A code_question's own folder for these is data/tasks/ID, as research's is; its sandboxed
commands can't write in the user's checkout, and its host commands treat that as in-place work.

Code tasks also load the project's own Claude Code setup (jarvis.tasks.project_claude): its CLAUDE.md,
subagents, skills and slash commands, but not its settings or hooks. JARVIS's helpers win a name
clash. The project's own subagents may edit files, where the task itself may. Skills can grant
tools (allowed-tools), so the gate runs again as a PreToolUse hook (`TaskGate.hook`), ahead of
any allow rule. Every kind also gets JARVIS's own skills that are switched on (jarvis.skills.home),
as a plugin of the run's own, under jarvis-skills:, with no hooks or !`commands` run from them.

Its parts: what the session is told (jarvis.tasks.briefs); the Reporter, the task's own tools and nudges
(jarvis.tasks.reporting); the tools on the Mac and their gate (jarvis.tasks.host_tools); the gate on every other call and
the secrets it hides (jarvis.tasks.gate); and its subagents (jarvis.tasks.subagents). Here: the options a run's session
gets, and the run.
"""

import asyncio
import json
import logging
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from claude_agent_sdk import (
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

from jarvis.brain import retry, usage
from jarvis.brain.sdk import BUILTIN_AGENTS_DENIED, LimitWatch, jarvis_server, sdk_options
from jarvis.core.config import (
    DATA_DIR,
    SCHEDULE_EFFORT,
    SCHEDULE_MODEL,
    TASK_CACHE_DIR,
    TASK_EFFORT,
    TASK_MAX_TURNS,
    TASK_MODEL,
    TASK_READ_DIRS,
)
from jarvis.core import profile
from jarvis.integrations import mcp_servers
from jarvis.safety import host
from jarvis.skills import home as skills
from jarvis.tasks import access, project_claude, projects
from jarvis.tasks.briefs import _system_prompt
from jarvis.tasks.gate import (
    _FILE_ARG,
    _IN_PLACE_DENIED,
    _WRITES,
    ARTIFACT,
    TaskGate,
    _hide_except,
    _read_only_git,
    _secret_rules,
)
from jarvis.tasks.host_tools import HOST_RUN, JOB_STATUS, JOB_WAIT, START_JOB, _host_gate, _host_server, task_temp
from jarvis.tasks.reporting import PAUSES, REQUEST_ACCESS, Reporter, _nudge_hook, _task_server, nudge_text
from jarvis.tasks.subagents import _AGENT_TOOLS, Subagents, _agents

# Moved to the modules above, and still importable from here
from jarvis.tasks.host_tools import TaskJobs  # noqa: F401
from jarvis.tasks.reporting import with_nudges  # noqa: F401
from jarvis.tasks.subagents import RESEARCHER, SEARCHER, TESTER, result_text  # noqa: F401

log = logging.getLogger("jarvis.tasks.worker")

READ_ONLY_TOOLS = ["calendar_events", "calendar_event", "calendar_free_time", "gmail_search", "gmail_read",
                   "sheets_search", "sheets_read", "docs_search", "docs_read", "slides_search", "slides_read",
                   "drive_search", "chat_unread", "chat_spaces", "chat_read", "chat_search", "gmail_attachments",
                   "gmail_attachment_read", "gmail_attachment_transcribe"]


@dataclass
class Outcome:
    result: str  # the session's final message
    error: str  # empty when the run succeeded
    cost: float
    kind: str = ""  # a failed run's kind of failure (jarvis.brain.retry)


ARTIFACT_ENV = {"CLAUDE_CODE_ARTIFACT": "1"}  # undocumented; without it the CLI leaves the tool out when run by the SDK


# code_change commands may reach any host: "*" matches every domain in the CLI's sandbox proxy. configd is the
# macOS network-settings service; uv and other Rust tools that read the system proxy crash on start without it.
OPEN_NETWORK = {"allowedDomains": ["*"], "allowMachLookup": ["com.apple.SystemConfiguration.configd"]}


def _cache_env() -> dict[str, str]:
    """Point build and package caches into the cache folder, where sandboxed commands may write."""
    dirs = {"GOCACHE": "go-build", "GOMODCACHE": "go-mod", "npm_config_cache": "npm", "UV_CACHE_DIR": "uv",
            "PIP_CACHE_DIR": "pip", "XDG_CACHE_HOME": "xdg", "PYTHONPYCACHEPREFIX": "pycache"}
    for sub in dirs.values():
        (TASK_CACHE_DIR / sub).mkdir(parents=True, exist_ok=True)
    return {var: str(TASK_CACHE_DIR / sub) for var, sub in dirs.items()}


def _blank_env() -> dict[str, str]:
    """Every .env setting, blanked: the CLI would otherwise inherit JARVIS's keys from the runner, and hand them
    to every command it runs. CLAUDE_ settings stay, since they're the CLI's own. Every MCP server's secret variables
    go whatever set them, and the vault's master key: only the MCP servers' launcher needs it, and reads it from .env
    itself (jarvis.integrations.mcp_launch)."""
    return {k: "" for k in host.env_keys() if not k.startswith("CLAUDE_")} | mcp_servers.session_env()


def _model(task: dict) -> tuple[str, str]:
    """The model and effort a run gets: scheduled runs and watch checks (jarvis.tasks.scheduler) are routine, so they
    get a lighter one, unless they change code. A lighter one still stands in while the model's own weekly limit is
    used up (jarvis.brain.usage)."""
    if task.get("job") is not None and task["kind"] != "code_change":
        return usage.available(SCHEDULE_MODEL), SCHEDULE_EFFORT
    return usage.available(TASK_MODEL), TASK_EFFORT


def _options(task: dict, reporter: Reporter) -> ClaudeAgentOptions:
    kind = task["kind"]
    env: dict[str, str] = _blank_env()
    mcp = {"task": _task_server(task["id"], reporter)}
    allowed = ["WebSearch", "WebFetch", "TodoWrite", "mcp__task__report_progress", "mcp__task__ask_user",
               "mcp__task__propose_data_edit", REQUEST_ACCESS]
    add_dirs: list[Path] = []
    denied: list[str] = []
    sandbox = bash = host_gate = None
    if kind == "code_change":
        cwd = Path(task["worktree"])  # for an in-place task, the project's own folder
        denied = _IN_PLACE_DENIED if task["in_place"] else []
        builtins = ["Read", "Glob", "Grep", "Edit", "Write", "Bash", "TodoWrite", "WebSearch", "WebFetch"]
        read_roots, write_roots = [cwd, TASK_CACHE_DIR, host.TASK_JOBS_DIR, *TASK_READ_DIRS, profile.current().voice_logs], [cwd]
        add_dirs = [TASK_CACHE_DIR]
        env |= _cache_env()
        task_temp(task["id"]).mkdir(parents=True, exist_ok=True)
        sandbox = {"enabled": True, "autoAllowBashIfSandboxed": True, "allowUnsandboxedCommands": False,
                   "network": OPEN_NETWORK}
        mcp["host"] = _host_server(task, cwd, reporter.host_jobs)
        allowed += [JOB_STATUS, JOB_WAIT]  # they only read; host_run and start_job meet the gate
        host_gate = _host_gate(task, cwd, reporter)
    elif kind == "code_question":
        cwd = projects.find(task["project"]).path
        builtins = ["Read", "Glob", "Grep", "Bash", "WebSearch", "WebFetch"]
        read_roots, write_roots = [cwd, *TASK_READ_DIRS, profile.current().voice_logs], []
        bash = _read_only_git
        # Bash still goes through the gate (read-only git only); the sandbox is a second wall.
        sandbox = {"enabled": True, "autoAllowBashIfSandboxed": False, "allowUnsandboxedCommands": False,
                   "network": {"allowedDomains": []}}
    else:
        cwd = DATA_DIR / "tasks" / str(task["id"])
        cwd.mkdir(parents=True, exist_ok=True)
        builtins = ["WebSearch", "WebFetch"]
        read_roots, write_roots = [], []
    # What its kind has by default and what it was granted (jarvis.tasks.access). A code_question keeps what it makes
    # in a folder of its own, as research does, since its project is the user's live checkout.
    can = access.has(task)
    own = DATA_DIR / "tasks" / str(task["id"]) if kind == "code_question" else cwd
    more_writes, edit_denied = [], []  # edits beyond write_roots; where sandboxed commands may not write
    if kind != "code_change":  # code_change has its shell and host tools already, set up above
        if can & {"artifacts", "shell", "mac"}:
            own.mkdir(parents=True, exist_ok=True)
            builtins, write_roots = builtins + ["Write"], [own]  # pages to publish and the like, in its own folder only
        if can & {"shell", "mac"}:
            task_temp(task["id"]).mkdir(parents=True, exist_ok=True)
        if "shell" in can:
            builtins.append("Bash")
            bash = None  # sandboxed commands run on their own; one that asks to leave the sandbox is denied
            add_dirs += [d for d in (own, TASK_CACHE_DIR) if d != cwd]  # where they may write, besides cwd
            env |= _cache_env()
            sandbox = {"enabled": True, "autoAllowBashIfSandboxed": True, "allowUnsandboxedCommands": False,
                       "network": OPEN_NETWORK}
            if kind == "code_question" and "projects" not in can:
                # Its commands can't write in the user's checkout: everything there but the way to its own folder
                # (which is inside only for JARVIS's own repo). Edit rules are the sandbox's write rules too.
                edit_denied = [r for p in _hide_except(cwd.resolve(), own.resolve()) for r in (f"Edit(/{p})", f"Edit(/{p}/**)")]
        if "mac" in can:
            # Host commands start in its own folder. A code_question's project is the user's checkout, where git
            # commands that change it are refused, as for in-place work.
            on = task | {"in_place": 1} if kind == "code_question" else task
            mcp["host"] = _host_server(on, own, reporter.host_jobs)
            allowed += [JOB_STATUS, JOB_WAIT]
            host_gate = _host_gate(on, own, reporter)
    if "projects" in can:
        builtins += ["Read", "Glob", "Grep", "Edit", "Write"]
        read_roots = read_roots or [own, *TASK_READ_DIRS, profile.current().voice_logs]  # research reads nothing otherwise
        more_writes = list(TASK_READ_DIRS)  # but for secrets and JARVIS's own files (TaskGate)
    if "accounts" in can:
        mcp["jarvis"], jarvis_allowed = jarvis_server(READ_ONLY_TOOLS)
        # The MCP servers marked for research, started read-only, with only their reads
        servers = mcp_servers.session(mcp_servers.for_research(), read_only=True)
        mcp |= servers.configs
        allowed += jarvis_allowed + servers.allowed
        denied = denied + servers.blocked
    if "artifacts" in can:
        env |= ARTIFACT_ENV
        builtins, allowed = builtins + [ARTIFACT], allowed + [ARTIFACT]  # each call still meets TaskGate.artifact
    # The project's own agents, skills, commands and CLAUDE.md, but never its settings (jarvis.tasks.project_claude).
    # Research works in no project, so it has none.
    setup = None
    helpers = _agents(kind)
    # Secrets hidden wherever it reads, and, for shell commands, in the projects too
    hidden_in = read_roots + (TASK_READ_DIRS if "shell" in can else [])
    flags: dict = _secret_rules(hidden_in) if hidden_in else {}
    if edit_denied:
        flags["permissions"]["deny"] += edit_denied
    if kind != "research" and task["project"]:
        setup = project_claude.load(cwd, task["project"], DATA_DIR / "tasks" / str(task["id"]) / "claude-plugin")
        if setup.claude_md:  # not one that looks like hidden instructions
            add_dirs.append(cwd)  # for its CLAUDE.md, which the CLI reads from additional directories (CLAUDE_MD_ENV)
            env |= project_claude.CLAUDE_MD_ENV
        if setup.flagged:
            reporter.flag(task["id"], f"Left out of {task['project']}'s setup, as they look like hidden instructions: "
                                      f"{'; '.join(setup.flagged)}")
        flags["disableAllHooks"] = True  # hooks in its skills and commands; the SDK's own callbacks still run
    # JARVIS's own skills that are switched on, as a plugin of the run's own (jarvis.skills.home)
    mine_at = DATA_DIR / "tasks" / str(task["id"]) / "jarvis-skills"
    mine = skills.plugin(mine_at)
    if mine:
        flags |= skills.FLAGS  # no hooks or !`commands` from them either
    skill_names = (setup.skills if setup else []) + [skills.qualified(s) for s in mine]
    plugins = ([{"type": "local", "path": str(setup.plugin)}] if setup and setup.plugin else []) + (
        [{"type": "local", "path": str(mine_at)}] if mine else [])
    agents = (setup.agents if setup else {}) | helpers  # JARVIS's helpers win a name clash
    gate = TaskGate(cwd, read_roots, write_roots, bash, agents, host_gate,
                    editors=set(setup.agents) - set(helpers) if setup else (), more_writes=more_writes)
    model, effort = _model(task)

    return sdk_options(
        env=env,
        cwd=str(cwd),
        add_dirs=[str(d) for d in add_dirs],
        system_prompt=_system_prompt(task, cwd, setup, own, mine),
        model=model,
        effort=effort,
        tools=list(dict.fromkeys(builtins + ["Agent"] + (["Skill"] if skill_names else []))),
        allowed_tools=allowed,
        disallowed_tools=denied + BUILTIN_AGENTS_DENIED,
        agents=agents,
        skills=skill_names,  # each allowed as Skill(name); the rest stay hidden
        plugins=plugins,
        mcp_servers=mcp,
        permission_mode="default",
        can_use_tool=gate,
        hooks={"PreToolUse": gate.hook} | dict.fromkeys(("PostToolUse", "PostToolUseFailure"), _nudge_hook(task["id"], reporter)),
        sandbox=sandbox,
        settings=json.dumps(flags) if flags else None,
        max_turns=TASK_MAX_TURNS,
        resume=task["session_id"],
        stderr=lambda line: log.debug("task %s claude: %s", task["id"], line),
    )


def _describe(block: ToolUseBlock) -> str | None:
    """A short, speakable note of what a tool call is doing."""
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
    if name == HOST_RUN:
        return f"running on the Mac: {args.get('command', '')[:80]}"
    if name == START_JOB:
        return f"starting job: {args.get('title') or args.get('command', '')[:60]}"
    if name == JOB_WAIT:
        return f"waiting for job {args.get('id')}"
    if name.startswith("mcp__jarvis__"):
        return f"using {name.removeprefix('mcp__jarvis__')}"
    if name.startswith("mcp__") and name.count("__") >= 2:
        server, _, tool = name.removeprefix("mcp__").partition("__")
        return f"using {server}: {tool}"
    return None  # the Agent tool included: Subagents logs its start, which becomes the activity


def _error(msg: ResultMessage, limits: LimitWatch, api_error: str | None) -> tuple[str, str]:
    """Why the run failed, and the kind of failure (jarvis.brain.retry), from its result, the rate-limit events and the
    CLI's error on its last turn (`api_error`); ("", "") if it didn't."""
    if not msg.is_error:
        return "", ""
    if msg.subtype == "error_max_turns":
        return "it ran out of steps", retry.ERROR
    text = msg.result or "; ".join(str(e) for e in msg.errors or []) or msg.subtype
    limit = limits.describe(text)  # first: it forgets a rejection whose window has reset
    kind = retry.result_kind(msg.api_error_status, msg.terminal_reason, limits.rejected is not None, api_error)
    return (f"it hit {limit}" if limit else text[:200]), kind


# A session's turn can end while subagents it sent to the background are still running. The run waits for them, at
# most SUBAGENT_WAIT seconds, and then for the CLI to wake the session with their reports; if it hasn't begun a new
# turn WAKE_GRACE seconds after the last one ended, the run wakes it itself.
SUBAGENT_WAIT = 3600.0
WAKE_GRACE = 30.0
SUBAGENTS_DONE = ("Your background subagents have all finished; their reports are above. Check what they found, "
                  "finish the task, and end with your summary as before.")
REPORT_MISSING = ("You ended without a summary. Write it now: its first line one plain sentence JARVIS will say "
                  "aloud, the details on the following lines, covering the whole task.")


class _Ended(Exception):
    """The session's stream ended, or the wait for its next message ran out."""


async def run(task: dict, prompt: str, reporter: Reporter) -> Outcome:
    """Send one prompt to the task's session (resumed if it has one) and wait for its final report. The run only
    finishes once every subagent has ended and the session has written a report (a question or a failure aside);
    otherwise it fails, saying why."""
    asked = False
    turn_began = False  # a message of a new turn arrived: the session is working again
    api_error: str | None = None  # the CLI's error on the session's latest message (AssistantMessage.error), if any
    limits = LimitWatch(f"task {task['id']}")
    helpers = Subagents(task["id"], reporter)

    def handle(msg) -> ResultMessage | None:
        nonlocal asked, turn_began, api_error
        reporter.alive(task["id"], msg)
        if isinstance(msg, TaskStartedMessage):
            helpers.task_started(msg)
        elif isinstance(msg, TaskNotificationMessage):  # a background subagent finished
            helpers.ended(msg.tool_use_id, msg.status != "completed")
        elif isinstance(msg, TaskUpdatedMessage):  # sometimes the only word that one ended
            helpers.task_updated(msg)
        elif isinstance(msg, SystemMessage) and msg.subtype == "init" and msg.data.get("session_id"):
            reporter.session(task["id"], msg.data["session_id"])
        elif isinstance(msg, AssistantMessage):
            turn_began = True
            if msg.parent_tool_use_id is None:
                api_error = msg.error
            for block in msg.content:
                if isinstance(block, ToolUseBlock):
                    if msg.parent_tool_use_id is None:  # the task's own session, not a subagent
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
            return msg
        return None

    result: ResultMessage | None = None
    async with ClaudeSDKClient(_options(task, reporter)) as client:
        inbox: asyncio.Queue = asyncio.Queue()

        async def pump() -> None:
            try:
                async for msg in client.receive_messages():
                    inbox.put_nowait(msg)
            finally:
                inbox.put_nowait(None)  # the stream ended

        async def read(timeout: float | None = None, until: Callable[[], bool] = lambda: False) -> ResultMessage | None:
            """Handle messages up to the next result (returned), or until `until()` holds (None). Raises _Ended if the
            stream ends or `timeout` seconds pass first."""
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
        reminded = False
        try:
            await client.query(prompt)
            result = await read()
            while not result.is_error:
                if helpers.waiting:
                    # Its turn ended with subagents still at work (a question too: their reports belong in the
                    # session it resumes). Wait for them, then for a turn the CLI starts itself with their reports.
                    turn_began = False
                    try:
                        if (again := await read(SUBAGENT_WAIT, until=lambda: not helpers.waiting)) is not None:
                            result = again
                            continue
                        if (again := await read(WAKE_GRACE, until=lambda: turn_began)) is None:
                            again = await read()  # the CLI woke it: wait for that turn's result
                    except _Ended as e:
                        if str(e) != "timeout" or helpers.waiting:
                            raise
                        if asked:  # it's waiting for an answer, which brings the reports with it
                            break
                        await prompt_again(SUBAGENTS_DONE)  # no new turn: wake it with the reports
                        again = await read()
                    result = again
                    continue
                if asked:
                    break
                # Messages sent after its last tool call become one more prompt in the same session.
                # A question keeps them for the answer, and a failure for the next reply.
                if late := reporter.take_nudges(task["id"]):
                    await prompt_again(nudge_text(late, finished=True))
                elif not (result.result or "").strip() and not reminded:
                    reminded = True
                    await prompt_again(REPORT_MISSING)  # once: a second silence fails the run below
                else:
                    break
                result = await read()  # total_cost_usd in the next result covers the whole run
        except _Ended as e:
            if waiting := helpers.waiting:
                who = ", ".join(waiting)
                why = (f"its subagents ({who}) were still running after {SUBAGENT_WAIT / 60:g} minutes"
                       if str(e) == "timeout" else f"the session ended while its subagents ({who}) were still running")
            else:
                why = "the session ended before its final report"
            kind = retry.ERROR if waiting else retry.PASSING  # the CLI died mid-run, say: that passes
            return Outcome((result.result or "") if result else "", why, (result.total_cost_usd or 0) if result else 0,
                           kind)
        finally:
            reader.cancel()
            with suppress(asyncio.CancelledError):
                await reader
    reporter.session(task["id"], result.session_id)
    error, kind = _error(result, limits, api_error)
    if not error and not asked and not (result.result or "").strip():
        error, kind = "it ended without a final report", retry.ERROR
    return Outcome(result.result or "", error, result.total_cost_usd or 0, kind)
