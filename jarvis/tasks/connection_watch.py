"""Keeps a running task's connections current (tasks with the accounts access, jarvis.tasks.worker).

A task's CLI starts its research servers once, so without this a key, address or tool sorting changed mid-run would
wait for its next run. Every WATCH_SECONDS `Watch` compares each server's `mcp_servers.stamps` with the one it runs
under:
  changed   restarted in place (reconnect_mcp_server), so the launcher reads the new settings and keys
  gone      switched off in the session (toggle_mcp_server): disabled, removed, no longer for research, or it can't
            start (its key cleared, say)
  back      switched on again, which starts it afresh
The session's allowed tools are fixed at start, so calls are also checked against the current sorting (`hook`): a
reading tool sorted as changing since is refused (it was let through without asking), as is any tool now blocked or
unsorted. Changing tools the session started with still each wait for a yes (connection_calls). The task hears of a
restart with its next tool result.
A connection added mid-run comes with the task's next run.
"""

import asyncio
import logging

from jarvis.integrations import mcp_servers
from jarvis.integrations.mcp_servers import Server
from jarvis.llm.claude import refusing
from jarvis.llm.sdk import HookMatcher

log = logging.getLogger("jarvis.tasks.connection_watch")

WATCH_SECONDS = 5.0


class Watch:
    """The research servers of one task session."""

    def __init__(self) -> None:
        self.servers: dict[str, Server] = {}  # as now set up; only those switched on
        self.running: dict[str, str] = {}  # name -> stamp it runs under, '' while switched off
        self.news: list[str] = []  # restarts the task hasn't heard of yet
        self.asking: set[str] = set()  # full names of the changing tools the session started with, each asked about

    def use(self, servers: list[Server]) -> None:
        """The servers the session starts with."""
        self.servers = {s.name: s for s in servers}
        self.running = mcp_servers.stamps(servers)
        self.asking = {s.tool(t) for s in servers for t in s.named("write", "own")}

    def _now(self) -> dict[str, tuple[Server, str]]:
        """The session's servers that can run now, with their stamps."""
        usable = [s for s in mcp_servers.for_research() if s.name in self.running and not mcp_servers.missing(s)]
        stamps = mcp_servers.stamps(usable)
        return {s.name: (s, stamps[s.name]) for s in usable}

    async def check(self, client) -> None:
        """Restart, switch off or switch on each server whose stamp changed."""
        now = await asyncio.to_thread(self._now)
        self.servers = {name: server for name, (server, _) in now.items()}
        for name, was in self.running.items():
            stamp = now[name][1] if name in now else ""
            if stamp == was:
                continue
            self.running[name] = stamp  # tried once per change, not every round
            try:
                if not stamp:
                    await client.toggle_mcp_server(name, False)
                elif not was:
                    await client.toggle_mcp_server(name, True)
                else:
                    await client.reconnect_mcp_server(name)
            except Exception as e:  # the CLI's own refusal; the session carries on without it
                log.warning("couldn't bring connection %s up to date: %s", name, e)
                continue
            log.info("connection %s %s", name, "switched off" if not stamp else "restarted with its new settings")
            if stamp:
                self.news.append(now[name][0].label)

    async def run(self, client, every: float = WATCH_SECONDS) -> None:
        """Check every `every` seconds until cancelled."""
        while self.running:
            await asyncio.sleep(every)
            try:
                await self.check(client)
            except Exception:
                log.exception("checking the task's connections failed")

    def refused(self, name: str, _args: dict) -> str:
        """Why a call to one of the session's servers is refused now, or ''."""
        server_name, _, tool = name.removeprefix("mcp__").partition("__")
        if not name.startswith("mcp__") or server_name not in self.running:
            return ""
        if (server := self.servers.get(server_name)) is None:
            return f"The {server_name} connection is switched off now."
        sorted_as = server.tools[tool].access if tool in server.tools else ""
        ok = sorted_as == "read" or (name in self.asking and sorted_as in ("write", "own"))
        return "" if ok else f"{tool} isn't available."

    @property
    def hook(self) -> list[HookMatcher]:
        """PreToolUse: refuse calls the current sorting doesn't allow."""
        return refusing(self.refused)

    @property
    def told(self) -> list[HookMatcher]:
        """PostToolUse: tell the task which connections were restarted since its last tool call."""

        async def tell(data: dict, _tool_use_id, _ctx) -> dict:
            if data.get("agent_id") or not self.news:
                return {}
            names, self.news = ", ".join(dict.fromkeys(self.news)), []
            return {"hookSpecificOutput": {"hookEventName": data["hook_event_name"], "additionalContext": (
                f"{names} restarted with new settings (a changed key or address, say): a call to it that failed "
                "before may work now.")}}

        return [HookMatcher(hooks=[tell])]
