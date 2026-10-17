"""A task's calls to a connection's changing tools (`ConnectionGate`): each waits for the user's yes.

Reading tools are allowed outright (mcp_servers.session). A write or own-inbox tool on one of the task's connections is
refused at first and becomes an approval request (jarvis.tasks.approvals), announced like a risky command: JARVIS reads
out what it would do (mcp_words.action, built from the tool's `says` phrase and the call's own arguments, never the
model's words) and the phone's Approve and Deny buttons answer it. A yes lets the identical call, with exactly the same
arguments, through once. Orders and remembered rules never cover these calls. Unlisted and blocked tools are refused.
"""

import json
import logging

from jarvis.integrations import mcp_words
from jarvis.integrations.mcp_servers import Server
from jarvis.llm.sdk import PermissionResultAllow, PermissionResultDeny
from jarvis.storage import profile
from jarvis.tasks.host_tools import answer

log = logging.getLogger("jarvis.tasks.connection_calls")

WHY = "reaches other people or changes something there"
NEVER = "a change through a connection is asked about each time"  # why a yes to one can't be remembered


def call_item(server: Server, tool: str, args: dict) -> dict:
    """Approval request item for a call: the full tool name, what it does, the connection and the exact arguments.

    `command` and `folder` hold what the approval card and push show; the arguments make the match exact."""
    return {"tool": server.tool(tool), "command": mcp_words.action(server, tool, args), "folder": server.label,
            "why": WHY, "connection": server.label, "args": json.loads(json.dumps(args)), "remember": {"never": NEVER}}


class ConnectionGate:
    """Decides calls to the task's connections' changing tools; `tools` are their full names, for TaskGate."""

    def __init__(self, task_id: int, servers: list[Server], reporter) -> None:
        self.task_id, self.reporter = task_id, reporter
        self.servers = {s.name: s for s in servers}
        self.tools = {s.tool(t) for s in servers for t in s.named("write", "own")}

    async def __call__(self, name: str, args: dict, ctx) -> PermissionResultAllow | PermissionResultDeny:
        server_name, _, tool = name.removeprefix("mcp__").partition("__")
        server = self.servers.get(server_name)
        if server is None or name not in self.tools:
            return PermissionResultDeny(message=f"{tool or name} isn't available.")
        item = call_item(server, tool, args)
        if await answer(self.reporter.cleared(self.task_id, item)):  # a yes to exactly this call, not yet used
            log.warning("task %s, as agreed: On %s, %s", self.task_id, server.label, item["command"])
            return PermissionResultAllow()
        who = profile.name()
        if getattr(ctx, "agent_id", None):
            return PermissionResultDeny(message=f"Not done: it {WHY}, so it needs {who}'s go-ahead, and only the "
                                                "task's own agent can ask for that. Report back what you'd post instead.")
        approval = await answer(self.reporter.request_approval(self.task_id, item))
        return PermissionResultDeny(message=(
            f"Not done yet: it {WHY}, so it needs {who}'s go-ahead. JARVIS has asked them (request {approval['id']}), "
            "reading out what it would do. Don't ask them yourself. Carry on with anything that doesn't depend on it; "
            "if nothing's left, end your turn with one short line. When they answer you'll get a message; if it's a "
            "yes, make the identical call again, with exactly the same arguments."))
