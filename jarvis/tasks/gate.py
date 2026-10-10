"""The gate on a task's tool calls (jarvis.tasks.worker): `TaskGate` decides every call the CLI doesn't allow on its
own. Reads stay inside the task's folders, the user's projects (TASK_READ_DIRS) and JARVIS's voice session logs, and
edits inside its folder; host_run and start_job meet `_host_gate` (jarvis.tasks.host_tools). Secrets are hidden from
both the file tools and the sandbox (`_secret`, `_secret_rules`).
"""

import re
import shlex
from collections.abc import Callable, Iterable
from pathlib import Path

from claude_agent_sdk import HookMatcher, PermissionResultAllow, PermissionResultDeny, ToolPermissionContext

from jarvis.core import profile
from jarvis.core.config import CLAUDE_CONFIG_DIR, ROOT, SECRETS_DIR
from jarvis.safety import commands
from jarvis.tasks.host_tools import HOST_COMMANDS, _inside, _resolve
from jarvis.tasks.subagents import _AGENT_TOOLS

ARTIFACT = "Artifact"

_FILE_ARG = {"Read": "file_path", "Glob": "path", "Grep": "path", "Edit": "file_path", "Write": "file_path", "NotebookEdit": "notebook_path"}
_WRITES = {"Edit", "Write", "NotebookEdit"}
_GIT_READS = {"log", "show", "diff", "blame", "status", "shortlog", "ls-files", "rev-parse"}
_GIT_BLOCKED = ("--output", "--ext-diff", "--no-index")  # write files, run programs, or read outside the repo
_SHELL_META = re.compile(r"[;&|`$<>()\\\n]")
# In-place tasks share the user's checkout: git commands that commit, push, move branches or drop changes are refused
_IN_PLACE_DENIED = [f"Bash(git {c}:*)" for c in commands.IN_PLACE_GIT]  # and on the Mac, by commands.refused
_ARTIFACT_ACTIONS = {"publish", "read", "list"}
_ARTIFACT_PATHS = ("file_path", "file_paths", "out_dir", "paths")  # the local files and folders an Artifact call takes


# ── Secrets ─────────────────────────────────────────────────────
# Hidden from tasks everywhere they can read: by `TaskGate` for the file tools, and by Read deny rules
# (`_secret_rules`), which the CLI applies to its file tools and its sandbox alike. JARVIS's secrets/
# holds its Claude config, and in it the voice session logs, the one part tasks may read. The lists
# are jarvis.safety.commands', shared with the voice shell and the host profile.

def _secret(path: Path) -> bool:
    """Whether a task must not read `path` (resolved): secrets folders, .env files, keys and credentials."""
    if path.is_relative_to(profile.current().voice_logs.resolve()):
        return False
    return commands.secret_path(path)


def _hide_except(root: Path, keep: Path) -> list[Path]:
    """What to hide so that everything in `root` is hidden but `keep`: each thing beside it on the way down."""
    if not keep.is_relative_to(root):
        return [root]
    hidden, folder = [], root
    for part in keep.relative_to(root).parts:
        try:
            hidden += [entry for entry in folder.iterdir() if entry.name != part]
        except OSError:  # not there (yet): nothing further down to hide
            break
        folder = folder / part
    return hidden


def _secret_folders(folder: Path, keep: Path) -> list[str]:
    """Rules hiding every secrets/ folder in `folder`, except around `keep` (which `_hide_except` covers)."""
    if not keep.is_relative_to(folder):
        return [f"Read(/{folder}/**/secrets/**)"] + ([f"Read(/{folder}/**)"] if folder.name == "secrets" else [])
    if keep == folder:
        return []
    try:
        return [r for entry in folder.iterdir() if entry.is_dir() for r in _secret_folders(entry, keep)]
    except OSError:
        return []


def _secret_rules(read_dirs: list[Path]) -> dict:
    """Settings for a task's CLI: Read deny rules for every secret it could otherwise see."""
    keep = profile.current().voice_logs.resolve()
    hidden = {p for root in (SECRETS_DIR, Path(CLAUDE_CONFIG_DIR)) for p in _hide_except(root.resolve(), keep)}
    rules = [r for p in sorted(hidden) for r in (f"Read(/{p})", f"Read(/{p}/**)")]
    rules += [r for p in commands.HOME_SECRETS for r in (f"Read({p})", f"Read({p}/**)")]
    for folder in dict.fromkeys(d.resolve() for d in read_dirs):
        rules += [f"Read(/{folder}/{pattern})" for pattern in commands.SECRET_PATTERNS]
        rules += _secret_folders(folder, SECRETS_DIR.resolve())
    return {"permissions": {"deny": list(dict.fromkeys(rules))}}


def _jarvis_own(path: Path) -> bool:
    """JARVIS's own files (its code, data and secrets), which no grant lets a task write; a task's own worktree or
    folder in it is among its usual write roots, which the gate checks first."""
    mine = profile.current()
    return path.is_relative_to(ROOT.resolve()) or path in (mine.db.resolve(), mine.memory_file.resolve())


def _read_only_git(command: str) -> bool:
    if _SHELL_META.search(command):
        return False
    try:
        argv = shlex.split(command)
    except ValueError:
        return False
    return (
        len(argv) >= 2 and argv[0] == "git" and argv[1] in _GIT_READS
        and not any(a.startswith(_GIT_BLOCKED) for a in argv[2:])
    )


Decision = PermissionResultAllow | PermissionResultDeny


class TaskGate:
    """Decides every tool call the CLI doesn't allow on its own: calling it is the session's can_use_tool.
    Anything not handled is denied. `bash` approves commands outside the sandbox's auto-allow; without it
    they're all denied. `agents` are the subagents the session may start, and `editors` those of them that
    may edit files: the project's own. JARVIS's helpers never do. `host_gate` decides host_run and start_job,
    for the tasks that have them. `more_writes` are where a task granted projects may edit as well, but for
    secrets and JARVIS's own files.

    A project's skills and commands can carry allowed-tools, which the CLI turns into allow rules, and an
    allowed call never reaches can_use_tool. So `hook`, a PreToolUse hook, runs the same checks first,
    for every call, and denies what the gate would: no allow rule gets past it."""

    def __init__(self, cwd: Path, read_roots: list[Path], write_roots: list[Path], bash: Callable[[str], bool] | None = None,
                 agents: Iterable[str] = (), host_gate: Callable | None = None, editors: Iterable[str] = (),
                 more_writes: Iterable[Path] = ()) -> None:
        self.cwd, self.read_roots, self.write_roots, self.more_writes = cwd, read_roots, write_roots, list(more_writes)
        self.bash, self.agents, self.host_gate, self.editors = bash, set(agents), host_gate, set(editors)
        # What `hook` checks: everything the gate decides, but sandboxed Bash, which the sandbox allows itself
        self.checked = (_AGENT_TOOLS | set(_FILE_ARG) | {ARTIFACT} | ({"Bash"} if bash is not None else set())
                        | (set(HOST_COMMANDS) if host_gate is not None else set()))
        self.kinds: dict[str, str] = {}  # subagent id -> its type, as PreToolUse reports them
        self.decided: dict[str, Decision] = {}  # host commands `hook` already decided, by tool_use_id

    async def __call__(self, name: str, args: dict, ctx) -> Decision:
        if name in HOST_COMMANDS and (done := self.decided.pop(getattr(ctx, "tool_use_id", None) or "", None)):
            return done  # asking again would ask the user twice, or spend an approval that was only given once
        return self.decide(name, args, ctx)

    def decide(self, name: str, args: dict, ctx) -> Decision:
        cwd = self.cwd
        if name in HOST_COMMANDS and self.host_gate is not None:
            return self.host_gate(name, args, ctx)
        if name in _AGENT_TOOLS:
            if args.get("subagent_type") in self.agents:
                return PermissionResultAllow()
            return PermissionResultDeny(message=f"The subagents in this task are {', '.join(sorted(self.agents)) or 'none'}.")
        agent = getattr(ctx, "agent_id", None)
        if name in _WRITES | {ARTIFACT} and agent and self.kinds.get(agent) not in self.editors:
            return PermissionResultDeny(message="Subagents don't edit files; report back and the task's agent will make the change.")
        if name == ARTIFACT:
            return self.artifact(args)
        if name in _FILE_ARG:
            path = args.get(_FILE_ARG[name]) or str(cwd)
            if name not in _WRITES and _secret(_resolve(path, cwd)):
                return PermissionResultDeny(message=f"{name} can't read secrets (.env files, keys, credentials, secrets folders).")
            roots = self.write_roots if name in _WRITES else self.read_roots
            if roots and _inside(path, cwd, roots):
                return PermissionResultAllow()
            if name in _WRITES and self.more_writes and _inside(path, cwd, self.more_writes):
                if _secret(target := _resolve(path, cwd)) or _jarvis_own(target):
                    return PermissionResultDeny(message=f"{name} can't change secrets or JARVIS's own files.")
                return PermissionResultAllow()
            where = " and ".join(str(r) for r in roots + (self.more_writes if name in _WRITES else [])) or "nothing"
            return PermissionResultDeny(message=f"{name} is limited to {where} in this task.")
        if name == "Bash":
            if self.bash is None:
                return PermissionResultDeny(message="Commands must run inside the sandbox.")
            if self.bash(args.get("command", "")):
                return PermissionResultAllow()
            return PermissionResultDeny(message="Only single read-only git commands are allowed, without pipes, redirection or chaining.")
        return PermissionResultDeny(message=f"{name} isn't available in this task.")

    def artifact(self, args: dict) -> Decision:
        """An Artifact call: only publish, read and list, and the local files a publish sends off the Mac (or a read
        saves) must be in the task's own folder, and not secrets. Deleting, sharing and the rest are for the user."""
        if (action := args.get("action") or "publish") not in _ARTIFACT_ACTIONS:
            return PermissionResultDeny(message=f"Tasks only publish, read and list artifacts, not {action}. Give the link "
                                                f"in your summary for {profile.name()} to do the rest.")
        paths: list = []
        for key in _ARTIFACT_PATHS:
            value = args.get(key)
            paths += [value] if isinstance(value, str) else [*value, *value.values()] if isinstance(value, dict) else value or []
        for path in paths:
            if not isinstance(path, str) or _secret(_resolve(path, self.cwd)) or not _inside(path, self.cwd, self.write_roots):
                where = " and ".join(str(r) for r in self.write_roots) or "nowhere"
                return PermissionResultDeny(message=f"Artifact only publishes and saves files in {where}, and never secrets.")
        return PermissionResultAllow()

    async def pre_tool_use(self, data: dict, tool_use_id: str | None, _ctx) -> dict:
        if agent := data.get("agent_id"):
            self.kinds[agent] = data.get("agent_type") or ""
        if (name := data.get("tool_name")) not in self.checked:
            return {}
        ctx = ToolPermissionContext(tool_use_id=tool_use_id, agent_id=agent)
        decision = self.decide(name, data.get("tool_input") or {}, ctx)
        if isinstance(decision, PermissionResultDeny):
            return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                           "permissionDecisionReason": decision.message}}
        if name in HOST_COMMANDS and tool_use_id:
            self.decided[tool_use_id] = decision  # for can_use_tool, if an allow rule doesn't let it straight through
        return {}  # no say: deny rules and can_use_tool still have theirs

    @property
    def hook(self) -> list[HookMatcher]:
        return [HookMatcher(hooks=[self.pre_tool_use])]

