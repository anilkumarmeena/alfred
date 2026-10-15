"""Permission gate for a task session's tool calls (`TaskGate`).

  reads   task folders, the user's projects (TASK_READ_DIRS), voice session logs
  writes  the task's own folder (plus `more_writes` with the projects grant)
  host    host_run / start_job go to the host gate (jarvis.tasks.host_tools)
  images  docs_insert_image uploads only local files the task may read or write
Secrets are hidden from file tools (`_secret`) and from the sandbox via Read deny rules (`_secret_rules`).
"""

import logging
import re
import shlex
from collections.abc import Awaitable, Callable, Iterable
from pathlib import Path

from claude_agent_sdk import HookMatcher, PermissionResultAllow, PermissionResultDeny, ToolPermissionContext

from jarvis.core import profile
from jarvis.core.config import CLAUDE_CONFIG_DIR, ROOT, SECRETS_DIR
from jarvis.safety import commands
from jarvis.tasks.host_tools import HOST_COMMANDS, _inside, _resolve, answer
from jarvis.tasks.subagents import _AGENT_TOOLS

log = logging.getLogger("jarvis.tasks.gate")

ARTIFACT = "Artifact"

_FILE_ARG = {"Read": "file_path", "Glob": "path", "Grep": "path", "Edit": "file_path", "Write": "file_path", "NotebookEdit": "notebook_path"}
_WRITES = {"Edit", "Write", "NotebookEdit"}
_GIT_READS = {"log", "show", "diff", "blame", "status", "shortlog", "ls-files", "rev-parse"}
_GIT_BLOCKED = ("--output", "--ext-diff", "--no-index")  # write files, run programs, or read outside the repo
_SHELL_META = re.compile(r"[;&|`$<>()\\\n]")
# In-place tasks share the user's checkout: deny git commands that commit, push, move branches or drop changes.
# Commits are left to TaskGate.checkout, which lets one through when the user ordered it. Host side: commands.refused
_IN_PLACE_DENIED = [f"Bash(git {c}:*)" for c in commands.IN_PLACE_GIT if c != "commit"]
_ARTIFACT_ACTIONS = {"publish", "read", "list"}
_ARTIFACT_PATHS = ("file_path", "file_paths", "out_dir", "paths")  # Artifact args naming local paths
DOC_IMAGES = "mcp__jarvis__docs_insert_image"  # uploads local image files, so their paths are checked


# ── Secrets ─────────────────────────────────────────────────────
# Enforced twice: `TaskGate` for file tools, and Read deny rules (`_secret_rules`) that the CLI applies to file tools
# and the sandbox. secrets/ holds the Claude config; its voice session logs are the only readable part.
# Patterns come from jarvis.safety.commands, shared with the voice shell and host profile.

def _secret(path: Path) -> bool:
    """Whether resolved `path` is secret (secrets folders, .env, keys, credentials); voice logs are exempt."""
    if path.is_relative_to(profile.current().voice_logs.resolve()):
        return False
    return commands.secret_path(path)


def _hide_except(root: Path, keep: Path) -> list[Path]:
    """Paths to hide so all of `root` is hidden except `keep`: every sibling along the path down to it."""
    if not keep.is_relative_to(root):
        return [root]
    hidden, folder = [], root
    for part in keep.relative_to(root).parts:
        try:
            hidden += [entry for entry in folder.iterdir() if entry.name != part]
        except OSError:  # doesn't exist (yet): nothing below to hide
            break
        folder = folder / part
    return hidden


def _secret_folders(folder: Path, keep: Path) -> list[str]:
    """Deny rules for every secrets/ folder under `folder`, skipping the path to `keep` (see `_hide_except`)."""
    if not keep.is_relative_to(folder):
        return [f"Read(/{folder}/**/secrets/**)"] + ([f"Read(/{folder}/**)"] if folder.name == "secrets" else [])
    if keep == folder:
        return []
    try:
        return [r for entry in folder.iterdir() if entry.is_dir() for r in _secret_folders(entry, keep)]
    except OSError:
        return []


def _secret_rules(read_dirs: list[Path]) -> dict:
    """CLI settings with Read deny rules for every secret the task could otherwise see."""
    keep = profile.current().voice_logs.resolve()
    hidden = {p for root in (SECRETS_DIR, Path(CLAUDE_CONFIG_DIR)) for p in _hide_except(root.resolve(), keep)}
    rules = [r for p in sorted(hidden) for r in (f"Read(/{p})", f"Read(/{p}/**)")]
    rules += [r for p in commands.HOME_SECRETS for r in (f"Read({p})", f"Read({p}/**)")]
    for folder in dict.fromkeys(d.resolve() for d in read_dirs):
        rules += [f"Read(/{folder}/{pattern})" for pattern in commands.SECRET_PATTERNS]
        rules += _secret_folders(folder, SECRETS_DIR.resolve())
    return {"permissions": {"deny": list(dict.fromkeys(rules))}}


def _jarvis_own(path: Path) -> bool:
    """Whether `path` is JARVIS's code, db or memory, which no grant may write.

    A task's own folder under ROOT is fine: write roots are checked before this."""
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
    """The session's can_use_tool; denies anything it doesn't handle.

    `bash` approves unsandboxed Bash (None: deny all). `agents`: allowed subagents; `editors`: those that may edit
    (project ones only). `host_gate` decides host_run/start_job. `more_writes`: extra write roots from the projects
    grant, never secrets or JARVIS's files. `commit_ordered` (the user's own checkout, in place): whether they ordered
    a commit, which sandboxed Bash may then make (`checkout`).

    Allow rules from project skills skip can_use_tool, so `hook` (PreToolUse) runs the same checks on every call."""

    def __init__(self, cwd: Path, read_roots: list[Path], write_roots: list[Path], bash: Callable[[str], bool] | None = None,
                 agents: Iterable[str] = (), host_gate: Callable | None = None, editors: Iterable[str] = (),
                 more_writes: Iterable[Path] = (), commit_ordered: Callable[[], bool | Awaitable[bool]] | None = None) -> None:
        self.cwd, self.read_roots, self.write_roots, self.more_writes = cwd, read_roots, write_roots, list(more_writes)
        self.bash, self.agents, self.host_gate, self.editors = bash, set(agents), host_gate, set(editors)
        self.commit_ordered = commit_ordered
        # Tools `hook` checks: all the gate decides, plus sandboxed Bash in the user's own checkout
        self.checked = (_AGENT_TOOLS | set(_FILE_ARG) | {ARTIFACT, DOC_IMAGES}
                        | ({"Bash"} if bash is not None or commit_ordered is not None else set())
                        | (set(HOST_COMMANDS) if host_gate is not None else set()))
        self.kinds: dict[str, str] = {}  # subagent id -> type, from PreToolUse
        self.decided: dict[str, Decision] = {}  # host-command decisions made by `hook`, by tool_use_id

    async def __call__(self, name: str, args: dict, ctx) -> Decision:
        if name in HOST_COMMANDS and (done := self.decided.pop(getattr(ctx, "tool_use_id", None) or "", None)):
            return done  # don't ask the user twice or spend a one-time approval again
        return await answer(self.decide(name, args, ctx))

    def decide(self, name: str, args: dict, ctx) -> Decision | Awaitable[Decision]:
        """The decision; for host commands an awaitable one (the host gate may ask the runner, perhaps over a link)."""
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
        if name == DOC_IMAGES:
            return self.doc_images(args)
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
        """Allow only publish/read/list, with every local path inside the write roots and not secret."""
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

    def doc_images(self, args: dict) -> Decision:
        """Allow https links, and local images only where the task may read or write, never secrets."""
        images = args.get("images")
        if not isinstance(images, list):
            return PermissionResultAllow()  # the tool refuses it itself
        roots = self.read_roots + self.write_roots
        for item in images:
            source = item.get("image") if isinstance(item, dict) else item
            path = source.strip() if isinstance(source, str) else ""
            if not path or path.lower().startswith("https://"):
                continue  # a link, or not an image at all: the tool refuses that itself
            if _secret(_resolve(path, self.cwd)) or not _inside(path, self.cwd, roots):
                where = f" and images in {' and '.join(str(r) for r in roots)}, never secrets" if roots else ""
                return PermissionResultDeny(message=f"docs_insert_image takes https links{where} in this task.")
        return PermissionResultAllow()

    async def checkout(self, command: str) -> Decision:
        """Sandboxed Bash in the user's own checkout: no git that changes it, except a plain commit they ordered."""
        if not (found := commands.in_place_git(command)):
            return PermissionResultAllow()
        if set(found) == {"commit"} and await self._commit_ordered():
            return PermissionResultAllow()
        name, other = profile.name(), next((sub for sub in found if sub != "commit"), "")
        if other == "commit --amend":
            return PermissionResultDeny(message=f"Not run: git commit --amend rewrites a commit of {name}'s; make a new one.")
        if other:
            return PermissionResultDeny(message=f"Not run: git {other} changes {name}'s own checkout, which in-place "
                                                "tasks leave to them; list it in your summary instead.")
        return PermissionResultDeny(message=f"Not run: commit here only when {name}'s own words ask for one. Leave "
                                            "your changes uncommitted and list the files you changed in your summary.")

    async def _commit_ordered(self) -> bool:
        """Whether the user ordered a commit; no on any failure, as a hook that fails lets the command run."""
        try:
            return bool(self.commit_ordered and await answer(self.commit_ordered()))
        except Exception:
            log.exception("couldn't check for an order to commit")
            return False

    async def pre_tool_use(self, data: dict, tool_use_id: str | None, _ctx) -> dict:
        if agent := data.get("agent_id"):
            self.kinds[agent] = data.get("agent_type") or ""
        if (name := data.get("tool_name")) not in self.checked:
            return {}
        args = data.get("tool_input") or {}
        if name == "Bash" and self.bash is None:  # sandboxed; can_use_tool denies asking to leave the sandbox
            decision = await self.checkout(str(args.get("command") or ""))
        else:
            decision = await answer(self.decide(name, args, ToolPermissionContext(tool_use_id=tool_use_id, agent_id=agent)))
        if isinstance(decision, PermissionResultDeny):
            return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                           "permissionDecisionReason": decision.message}}
        if name in HOST_COMMANDS and tool_use_id:
            self.decided[tool_use_id] = decision  # reused by can_use_tool if it gets called
        return {}  # no opinion: deny rules and can_use_tool still apply

    @property
    def hook(self) -> list[HookMatcher]:
        return [HookMatcher(hooks=[self.pre_tool_use])]

