"""The fallback that asks, for a task's session (`AutoGate`), and the rules every task keeps.

A task runs in Claude Code's auto mode (alfred.tasks.worker), whose own check clears each action; can_use_tool is
reached only by ask rules (a connection's changes) and when auto mode stops to ask.
"""

import json
import logging
from pathlib import Path

from alfred.llm.sdk import PermissionResultAllow, PermissionResultDeny
from alfred.safety import commands
from alfred.storage import profile
from alfred.tasks.connection_calls import ConnectionGate
from alfred.tasks.host_tools import answer, approval_item

log = logging.getLogger("alfred.tasks.gate")

ARTIFACT = "Artifact"

_FILE_ARG = {"Read": "file_path", "Glob": "path", "Grep": "path", "Edit": "file_path", "Write": "file_path", "NotebookEdit": "notebook_path"}
_WRITES = {"Edit", "Write", "NotebookEdit"}
# In-place tasks share the user's checkout: deny git commands that push, move branches or drop their changes
_IN_PLACE_DENIED = [f"Bash(git {c}:*)" for c in commands.IN_PLACE_GIT if c != "commit"]

Decision = PermissionResultAllow | PermissionResultDeny

ASKED = "needs your yes before auto mode lets it run"  # why, when the command itself doesn't say
SHOWN_ARGS = 1500  # chars of a tool's arguments shown in its request


class AutoGate:
    """can_use_tool for a task in auto mode (alfred.llm.claude.auto_mode). Auto mode's own check clears or blocks each
    action, so this is reached only by ask rules (a connection's changes, decided by `connections`) and when auto mode
    stops to ask: the user answers on their phone, as for a risky host command, and a yes lets the identical call
    through once."""

    def __init__(self, task_id: int, cwd: Path, reporter, connections: ConnectionGate | None = None) -> None:
        self.task_id, self.cwd, self.reporter, self.connections = task_id, cwd, reporter, connections

    async def __call__(self, name: str, args: dict, ctx) -> Decision:
        if self.connections and name in self.connections.tools:
            return await answer(self.connections(name, args, ctx))
        item = self.item(name, args)
        if await answer(self.reporter.cleared(self.task_id, item)):
            log.warning("task %s runs, as agreed: %s %s", self.task_id, name, item["command"][:200])
            return PermissionResultAllow()
        if getattr(ctx, "agent_id", None):
            return PermissionResultDeny(message=f"Not done: it needs {profile.name()}'s go-ahead, and only the task's own "
                                                "agent can ask for that. Report back what you wanted to do instead.")
        approval = await answer(self.reporter.request_approval(self.task_id, item))
        return PermissionResultDeny(message=(
            f"Not done yet: it needs {profile.name()}'s go-ahead. Alfred has asked them (request {approval['id']}). Carry "
            "on with anything that doesn't depend on it; if nothing's left, end your turn with one short line. When they "
            f"answer you'll get a message; if it's a yes, make the identical {name} call again, once."))

    def item(self, name: str, args: dict) -> dict:
        """The approval request: for Bash its command and what makes it risky, else the tool and its arguments."""
        if name == "Bash" and (command := str(args.get("command") or "").strip()):
            return approval_item(name, {"command": command}, self.cwd, commands.describe(command, cwd=self.cwd) or ASKED)
        shown = json.dumps(args, sort_keys=True, ensure_ascii=False)
        return {"tool": name, "command": f"{name} {shown[:SHOWN_ARGS]}", "folder": str(self.cwd), "why": ASKED,
                "remember": {"pieces": [], "never": "auto mode asks about each one"}}

