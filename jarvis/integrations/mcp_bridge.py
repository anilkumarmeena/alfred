"""A bridge from a Claude session's stdio to an HTTP MCP server (MCP's streamable HTTP transport), run by the launcher
(jarvis.integrations.mcp_launch) for an HTTP server with a key in a header or a sign-in, so neither is ever on the
CLI's command line or in its environment.

Each JSON-RPC message the CLI writes, one per line, is POSTed to the server's URL with its headers filled in from the
vault (`{VAR}` in a header's value takes the secret its secret_env names) and, for a signed-in server, its access
token (jarvis.integrations.mcp_oauth), refreshed once if the server refuses it. The answer, plain JSON or a stream of
server-sent events, goes back to the CLI a line per message. The session id the server gives at the start goes with
every later request, as does the protocol version agreed then. Requests are sent side by side, so a slow call doesn't
hold up the rest. The server's own stream of messages outside replies (a GET) isn't opened: tools don't need it.
"""

import asyncio
import json
import logging
import re
import sys
from collections.abc import Awaitable, Callable

import httpx

from jarvis.core import vault
from jarvis.integrations import mcp_oauth
from jarvis.integrations.mcp_servers import _FIELD, Server

log = logging.getLogger("jarvis.integrations.mcp_bridge")

LINE_LIMIT = 64 * 1024 * 1024  # a message the CLI writes: a file's content can be long
ERROR = -32000


def headers(server: Server) -> dict[str, str]:
    """Its headers with their secrets filled in. Raises vault.VaultError if one isn't set."""
    out = {}
    for name, value in server.headers.items():
        filled = _FIELD.sub(lambda m: vault.get(server.secret_env[m.group(1)]).strip(), value)
        out[name] = re.sub(r"^(Bearer\s+)+", "Bearer ", filled, flags=re.IGNORECASE)  # a key pasted with its Bearer
    return out


class Bridge:
    def __init__(self, server: Server, write: Callable[[dict], Awaitable[None]],
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.server, self.write = server, write
        self.fixed = headers(server)
        self.session_id = ""
        self.version = ""
        self.client = httpx.AsyncClient(transport=transport, timeout=httpx.Timeout(30, read=None),
                                        follow_redirects=True)

    async def _headers(self, refresh: bool = False) -> dict[str, str]:
        out = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"} | self.fixed
        if self.server.sign_in:
            out["Authorization"] = f"Bearer {await mcp_oauth.token(self.server, refresh, self.client)}"
        if self.session_id:
            out["Mcp-Session-Id"] = self.session_id
        if self.version:
            out["MCP-Protocol-Version"] = self.version
        return out

    async def _fail(self, message: dict | list, why: str) -> None:
        """Each request in the message answered with an error, so the CLI isn't left waiting."""
        for m in message if isinstance(message, list) else [message]:
            if isinstance(m, dict) and "id" in m and "method" in m:
                await self.write({"jsonrpc": "2.0", "id": m["id"], "error": {"code": ERROR, "message": why}})

    async def _passed(self, found, initialize: bool) -> None:
        for m in found if isinstance(found, list) else [found]:
            if not isinstance(m, dict):
                continue
            if initialize and isinstance(m.get("result"), dict) and m["result"].get("protocolVersion"):
                self.version = str(m["result"]["protocolVersion"])
            await self.write(m)

    async def send(self, message: dict | list) -> None:
        """One message from the CLI to the server, and whatever it answers back to the CLI."""
        initialize = isinstance(message, dict) and message.get("method") == "initialize"
        label = self.server.label
        try:
            for attempt in (0, 1):
                async with self.client.stream("POST", self.server.url, json=message,
                                              headers=await self._headers(refresh=attempt == 1)) as r:
                    if r.status_code == 401 and self.server.sign_in and attempt == 0:
                        continue  # its token refreshed, once
                    if initialize and (sid := r.headers.get("mcp-session-id")):
                        self.session_id = sid
                    if r.status_code == 401:
                        await self._fail(message, f"{label} refused JARVIS's {'sign-in' if self.server.sign_in else 'key'}"
                                                  "; it may need setting again.")
                    elif r.status_code >= 400:
                        await self._fail(message, f"{label} answered {r.status_code}.")
                    elif r.headers.get("content-type", "").startswith("text/event-stream"):
                        await self._events(r, initialize)
                    elif r.status_code != 202:
                        body = await r.aread()
                        if body.strip():
                            await self._passed(json.loads(body), initialize)
                    return
        except (httpx.HTTPError, ValueError, mcp_oauth.SignInError, vault.VaultError) as e:
            log.warning("%s: %s", self.server.name, type(e).__name__)
            await self._fail(message, str(e) if isinstance(e, mcp_oauth.SignInError) else
                             f"{label} couldn't be reached ({type(e).__name__}).")

    async def _events(self, r: httpx.Response, initialize: bool) -> None:
        data: list[str] = []
        async for line in r.aiter_lines():
            if line.startswith("data:"):
                data.append(line[5:].removeprefix(" "))
            elif not line.strip() and data:
                try:
                    await self._passed(json.loads("\n".join(data)), initialize)
                except ValueError:
                    pass
                data = []
        if data:
            try:
                await self._passed(json.loads("\n".join(data)), initialize)
            except ValueError:
                pass

    async def close(self) -> None:
        if self.session_id:
            try:
                await self.client.delete(self.server.url, headers=await self._headers(), timeout=5)
            except (httpx.HTTPError, mcp_oauth.SignInError, vault.VaultError):
                pass
        await self.client.aclose()


async def _stdin() -> asyncio.StreamReader:
    reader = asyncio.StreamReader(limit=LINE_LIMIT)
    await asyncio.get_running_loop().connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)
    return reader


async def serve(server: Server, reader: asyncio.StreamReader | None = None,
                out=None, transport: httpx.AsyncBaseTransport | None = None) -> int:
    """Bridges stdin to the server and its answers to stdout until stdin ends. Raises vault.VaultError if a secret
    isn't set."""
    reader = reader or await _stdin()
    out = out or sys.stdout
    lock = asyncio.Lock()

    async def write(message: dict) -> None:
        async with lock:
            out.write(json.dumps(message, ensure_ascii=False) + "\n")
            out.flush()

    bridge = Bridge(server, write, transport)
    running: set[asyncio.Task] = set()
    try:
        while line := await reader.readline():
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if isinstance(message, dict) and message.get("method") == "initialize":
                await bridge.send(message)  # first, so what follows carries its session
                continue
            task = asyncio.create_task(bridge.send(message))
            running.add(task)
            task.add_done_callback(running.discard)
        if running:
            await asyncio.wait(running, timeout=30)
    finally:
        await bridge.close()
    return 0
