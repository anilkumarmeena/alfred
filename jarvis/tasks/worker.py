"""One run of a background task: a Claude Code session limited to what its kind allows.

  code_change    edits the project folder in place (uncommitted), or a worktree + branch when in_place=false
  code_question  reads the live checkout; shell is read-only git only
  research       web plus read-only account tools and research MCP servers (read-only); works in data/tasks/ID

Safety: every tool call passes TaskGate (also as a PreToolUse hook, since skills' allowed-tools become allow rules).
Reads stay in task folders, TASK_READ_DIRS and voice logs; writes in the worktree. Secrets are hidden from file tools
and the sandbox. code_change shell runs sandboxed (writes: worktree + cache; network: open); escaping is denied.
Host-level work goes through host tools (host_tools), gated by `_host_gate`. The CLI's env has .env values blanked.
Tasks never write JARVIS's data files; they propose edits. Extra access (jarvis.tasks.access) shapes `_options`.
Code tasks load the project's Claude setup (project_claude); JARVIS's enabled skills load as a plugin. JARVIS's
helper subagents win name clashes. A run doesn't end while background subagents run, nor without a final report.
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
from jarvis.core import platform, profile
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
from jarvis.tasks.reporting import PAUSES, REQUEST_ACCESS, Reporter, _nudge_hook, _task_server, answer, nudge_text
from jarvis.tasks.subagents import _AGENT_TOOLS, Subagents, _agents

# Re-exported: other modules and tests import these from here
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
    result: str  # final message
    error: str  # "" on success
    cost: float
    kind: str = ""  # failure kind (jarvis.brain.retry)


ARTIFACT_ENV = {"CLAUDE_CODE_ARTIFACT": "1"}  # undocumented; the CLI omits Artifact under the SDK without it


# "*" allows every domain through the sandbox proxy. configd (macOS network settings) is needed by uv and other
# Rust tools that read the system proxy, or they crash on start; Mach lookups are a Seatbelt rule, so Mac only.
OPEN_NETWORK = {"allowedDomains": ["*"]} | (
    {"allowMachLookup": ["com.apple.SystemConfiguration.configd"]} if platform.MAC else {})


def _cache_env() -> dict[str, str]:
    """Env pointing build/package caches into TASK_CACHE_DIR (sandbox-writable)."""
    dirs = {"GOCACHE": "go-build", "GOMODCACHE": "go-mod", "npm_config_cache": "npm", "UV_CACHE_DIR": "uv",
            "PIP_CACHE_DIR": "pip", "XDG_CACHE_HOME": "xdg", "PYTHONPYCACHEPREFIX": "pycache"}
    for sub in dirs.values():
        (TASK_CACHE_DIR / sub).mkdir(parents=True, exist_ok=True)
    return {var: str(TASK_CACHE_DIR / sub) for var, sub in dirs.items()}


def _blank_env() -> dict[str, str]:
    """Blank every .env key (except CLAUDE_*) plus MCP secrets and the vault key, so commands can't inherit them.

    The MCP launcher reads the vault key from .env itself.
    """
    return {k: "" for k in host.env_keys() if not k.startswith("CLAUDE_")} | mcp_servers.session_env()


def _model(task: dict) -> tuple[str, str]:
    """(model, effort): scheduled non-code runs get SCHEDULE_MODEL; usage.available falls back if a limit is out."""
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
        cwd = Path(task["worktree"])  # the project folder when in place
        denied = _IN_PLACE_DENIED if task["in_place"] else []
        builtins = ["Read", "Glob", "Grep", "Edit", "Write", "Bash", "TodoWrite", "WebSearch", "WebFetch"]
        read_roots, write_roots = [cwd, TASK_CACHE_DIR, host.TASK_JOBS_DIR, *TASK_READ_DIRS, profile.current().voice_logs], [cwd]
        add_dirs = [TASK_CACHE_DIR]
        env |= _cache_env()
        task_temp(task["id"]).mkdir(parents=True, exist_ok=True)
        sandbox = {"enabled": True, "autoAllowBashIfSandboxed": True, "allowUnsandboxedCommands": False,
                   "network": OPEN_NETWORK}
        mcp["host"] = _host_server(task, cwd, reporter.host_jobs)
        allowed += [JOB_STATUS, JOB_WAIT]  # read-only; host_run/start_job are gated
        host_gate = _host_gate(task, cwd, reporter)
    elif kind == "code_question":
        cwd = projects.find(task["project"]).path
        builtins = ["Read", "Glob", "Grep", "Bash", "WebSearch", "WebFetch"]
        read_roots, write_roots = [cwd, *TASK_READ_DIRS, profile.current().voice_logs], []
        bash = _read_only_git
        # Bash is gated to read-only git; the sandbox is a second wall.
        sandbox = {"enabled": True, "autoAllowBashIfSandboxed": False, "allowUnsandboxedCommands": False,
                   "network": {"allowedDomains": []}}
    else:
        cwd = DATA_DIR / "tasks" / str(task["id"])
        cwd.mkdir(parents=True, exist_ok=True)
        builtins = ["WebSearch", "WebFetch"]
        read_roots, write_roots = [], []
    # Default + granted access. code_question gets its own folder (like research): its project is the live checkout.
    can = access.has(task)
    own = DATA_DIR / "tasks" / str(task["id"]) if kind == "code_question" else cwd
    more_writes, edit_denied = [], []  # extra edit roots; sandbox write denials
    if kind != "code_change":  # code_change already has shell and host tools
        if can & {"artifacts", "shell", "mac"}:
            own.mkdir(parents=True, exist_ok=True)
            builtins, write_roots = builtins + ["Write"], [own]  # own folder only
        if can & {"shell", "mac"}:
            task_temp(task["id"]).mkdir(parents=True, exist_ok=True)
        if "shell" in can:
            builtins.append("Bash")
            bash = None  # sandboxed; requests to leave the sandbox are denied
            add_dirs += [d for d in (own, TASK_CACHE_DIR) if d != cwd]  # extra writable dirs
            env |= _cache_env()
            sandbox = {"enabled": True, "autoAllowBashIfSandboxed": True, "allowUnsandboxedCommands": False,
                       "network": OPEN_NETWORK}
            if kind == "code_question" and "projects" not in can:
                # Block writes in the user's checkout except the path to its own folder (inside only for JARVIS's
                # repo). Edit deny rules also act as sandbox write rules.
                edit_denied = [r for p in _hide_except(cwd.resolve(), own.resolve()) for r in (f"Edit(/{p})", f"Edit(/{p}/**)")]
        if "mac" in can:
            # Host commands start in its own folder; for code_question, treat the checkout as in-place (no git changes).
            on = task | {"in_place": 1} if kind == "code_question" else task
            mcp["host"] = _host_server(on, own, reporter.host_jobs)
            allowed += [JOB_STATUS, JOB_WAIT]
            host_gate = _host_gate(on, own, reporter)
    if "projects" in can:
        builtins += ["Read", "Glob", "Grep", "Edit", "Write"]
        read_roots = read_roots or [own, *TASK_READ_DIRS, profile.current().voice_logs]  # research has none otherwise
        more_writes = list(TASK_READ_DIRS)  # TaskGate still blocks secrets and JARVIS's own files
    if "accounts" in can:
        mcp["jarvis"], jarvis_allowed = jarvis_server(READ_ONLY_TOOLS)
        # Research MCP servers, read-only
        servers = mcp_servers.session(mcp_servers.for_research(), read_only=True)
        mcp |= servers.configs
        allowed += jarvis_allowed + servers.allowed
        denied = denied + servers.blocked
    if "artifacts" in can:
        env |= ARTIFACT_ENV
        builtins, allowed = builtins + [ARTIFACT], allowed + [ARTIFACT]  # each call still checked by TaskGate.artifact
    # Project's own agents, skills, commands and CLAUDE.md, never its settings. Research has no project.
    setup = None
    helpers = _agents(kind)
    # Hide secrets in read roots, and in projects too when it has a shell
    hidden_in = read_roots + (TASK_READ_DIRS if "shell" in can else [])
    flags: dict = _secret_rules(hidden_in) if hidden_in else {}
    if edit_denied:
        flags["permissions"]["deny"] += edit_denied
    if kind != "research" and task["project"]:
        setup = project_claude.load(cwd, task["project"], DATA_DIR / "tasks" / str(task["id"]) / "claude-plugin")
        if setup.claude_md:  # not tampered
            add_dirs.append(cwd)  # CLI reads CLAUDE.md from additional dirs (CLAUDE_MD_ENV)
            env |= project_claude.CLAUDE_MD_ENV
        if setup.flagged:
            reporter.flag(task["id"], f"Left out of {task['project']}'s setup, as they look like hidden instructions: "
                                      f"{'; '.join(setup.flagged)}")
        flags["disableAllHooks"] = True  # project hooks off; SDK callback hooks still run
    # JARVIS's enabled skills, as a per-run plugin; on a machine, the ones the main computer sent ("skills")
    mine_at = DATA_DIR / "tasks" / str(task["id"]) / "jarvis-skills"
    mine = skills.plugin(mine_at, skills.shipped(task["skills"]) if "skills" in task else None)
    if mine:
        flags |= skills.FLAGS  # no hooks or !`commands` from them
    skill_names = (setup.skills if setup else []) + [skills.qualified(s) for s in mine]
    plugins = ([{"type": "local", "path": str(setup.plugin)}] if setup and setup.plugin else []) + (
        [{"type": "local", "path": str(mine_at)}] if mine else [])
    agents = (setup.agents if setup else {}) | helpers  # helpers win name clashes
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
        skills=skill_names,  # each allowed as Skill(name); others hidden
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
    if name == HOST_RUN:
        return f"running on the computer: {args.get('command', '')[:80]}"
    if name == START_JOB:
        return f"starting job: {args.get('title') or args.get('command', '')[:60]}"
    if name == JOB_WAIT:
        return f"waiting for job {args.get('id')}"
    if name.startswith("mcp__jarvis__"):
        return f"using {name.removeprefix('mcp__jarvis__')}"
    if name.startswith("mcp__") and name.count("__") >= 2:
        server, _, tool = name.removeprefix("mcp__").partition("__")
        return f"using {server}: {tool}"
    return None  # incl. Agent: Subagents reports its start


def _error(msg: ResultMessage, limits: LimitWatch, api_error: str | None) -> tuple[str, str]:
    """(reason, retry kind) for a failed result; ("", "") on success."""
    if not msg.is_error:
        return "", ""
    if msg.subtype == "error_max_turns":
        return "it ran out of steps", retry.ERROR
    text = msg.result or "; ".join(str(e) for e in msg.errors or []) or msg.subtype
    limit = limits.describe(text)  # first: it clears rejections whose window has reset
    kind = retry.result_kind(msg.api_error_status, msg.terminal_reason, limits.rejected is not None, api_error)
    return (f"it hit {limit}" if limit else text[:200]), kind


# A turn may end with background subagents still running: wait up to SUBAGENT_WAIT for them, then up to WAKE_GRACE
# for the CLI to start a turn with their reports, else prompt the session ourselves.
SUBAGENT_WAIT = 3600.0
WAKE_GRACE = 30.0
SUBAGENTS_DONE = ("Your background subagents have all finished; their reports are above. Check what they found, "
                  "finish the task, and end with your summary as before.")
REPORT_MISSING = ("You ended without a summary. Write it now: its first line one plain sentence JARVIS will say "
                  "aloud, the details on the following lines, covering the whole task.")


class _Ended(Exception):
    """Session stream ended, or the wait for a message timed out."""


async def run(task: dict, prompt: str, reporter: Reporter) -> Outcome:
    """Send `prompt` to the task's session (resuming if any) and wait for its final report.

    Finishes only once all subagents have ended and a report exists (unless it asked a question or failed).
    """
    asked = False
    turn_began = False  # a new turn has started
    api_error: str | None = None  # AssistantMessage.error on the latest main-session message
    limits = LimitWatch(f"task {task['id']}")
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
                result = await read()  # its total_cost_usd covers the whole run
        except _Ended as e:
            if waiting := helpers.waiting:
                who = ", ".join(waiting)
                why = (f"its subagents ({who}) were still running after {SUBAGENT_WAIT / 60:g} minutes"
                       if str(e) == "timeout" else f"the session ended while its subagents ({who}) were still running")
            else:
                why = "the session ended before its final report"
            kind = retry.ERROR if waiting else retry.PASSING  # e.g. CLI died mid-run: transient
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
