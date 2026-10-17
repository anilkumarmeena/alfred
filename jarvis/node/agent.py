"""The machine's side: dial the main computer, sign in, and do what it sends, under the same gates as there.

Nothing listens here: the agent dials out to the main computer's /api/node and dials again after any drop (sleep, a
network change, the main computer restarting), so a machine that wakes is back online within seconds.

The main computer's requests are answered by `handlers`: the online check here, and the work parts' own (see
jarvis.node.remote). The agent keeps the computer awake only while it has work running (`awake`).
"""

import asyncio
import contextlib
import logging
import os
import subprocess
from collections.abc import Awaitable, Callable
from pathlib import Path

import aiohttp

from jarvis.config import ROOT
from jarvis.core import platform
from jarvis.llm import usage
from jarvis.node import bundles, home, link, transfer, update
from jarvis.node.link import Channel, Link, LinkDown, Refused, WebSocketChannel
from jarvis.state import preferences
from jarvis.storage import profile

log = logging.getLogger("jarvis.node.agent")

BACKOFF = (1, 2, 5, 10, 15)  # seconds between dials after a drop; then every 15
HEARTBEAT = 30.0  # WebSocket keepalive, so a dead link is noticed


def ws_url(main: str) -> str:
    """https://host -> wss://host/api/node (http -> ws for a local test)."""
    base = main.rstrip("/")
    if base.startswith("https://"):
        base = "wss://" + base.removeprefix("https://")
    elif base.startswith("http://"):
        base = "ws://" + base.removeprefix("http://")
    return f"{base}/api/node"


class Awake:
    """Keeps the computer from sleeping while work runs (macOS caffeinate; systemd-inhibit on Linux), no longer."""

    def __init__(self) -> None:
        self.holders: set[str] = set()
        self.proc: subprocess.Popen | None = None

    def hold(self, why: str) -> None:
        self.holders.add(why)
        if self.proc is None or self.proc.poll() is not None:
            argv = (["caffeinate", "-i", "-w", str(os.getpid())] if platform.MAC else
                    ["systemd-inhibit", "--what=sleep:idle", "--who=JARVIS", "--why=running work", "sleep", "infinity"])
            try:
                self.proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                             stderr=subprocess.DEVNULL)
            except OSError as e:
                log.warning("can't keep the computer awake: %s", e)

    def release(self, why: str) -> None:
        self.holders.discard(why)
        if not self.holders and self.proc is not None:
            self.proc.terminate()
            self.proc = None


class Agent:
    """One machine's agent. `dial()` opens a channel to the main computer (a WebSocket unless a test gives one)."""

    def __init__(self, node: int, own_key: str, main_public: str, dial: Callable[[], Awaitable[Channel]],
                 root: Path = ROOT, restart: Callable[[], None] | None = None) -> None:
        self.node, self.own_key, self.main_public, self.dial, self.root = node, own_key, main_public, dial, root
        self.link: Link | None = None
        self.outbox = transfer.Outbox()
        self.handlers: dict[str, Callable[[dict], Awaitable[dict | None]]] = {"ping": self.ping,
                                                                            "fetch": self.outbox.fetch}
        self.busy: Callable[[], int] = lambda: 0  # runs and jobs going here; set by the work parts
        self.on_link: list[Callable[[Link], Awaitable[None]]] = []  # e.g. replaying the journal
        self.awake = Awake()
        self.commit = ""
        self.welcomed: dict = {}
        self.connected = asyncio.Event()
        self.updater = update.Updater(root, lambda: self.busy() > 0, restart or self._restart)
        self.stopping = asyncio.Event()
        self.rollback = ""  # a rolled-back update's story, told once connected

    @staticmethod
    def _restart() -> None:
        os._exit(update.RESTART)  # the service manager starts the new code

    # ── Connecting ───────────────────────────────────────────────

    async def run(self) -> None:
        """Dial, serve, and dial again after any drop, until stopped."""
        self.commit = await bundles.head(self.root)
        if said := update.started(self.root):  # got this far, so this commit starts
            log.warning(said)
            self.rollback = said
        attempt = 0
        while not self.stopping.is_set():
            try:
                await self.connect_once()
                attempt = 0
            except (LinkDown, Refused, OSError, TimeoutError, aiohttp.ClientError) as e:
                log.info("not connected to the main computer: %s", e)
            if self.stopping.is_set():
                break
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.stopping.wait(), BACKOFF[min(attempt, len(BACKOFF) - 1)])
            attempt += 1

    async def connect_once(self) -> None:
        """One connection: sign in, take the welcome, serve the link until it drops."""
        channel = await self.dial()
        try:
            facts = await asyncio.to_thread(home.facts, self.commit)
            session, self.welcomed = await link.handshake_machine(channel, self.node, self.own_key, self.main_public,
                                                                  facts)
        except BaseException:
            await channel.close()
            raise
        self.link = Link(channel, session, self.own_key, self.main_public, self.handlers, name="main computer")
        served = asyncio.create_task(self.link.serve())
        self.connected.set()
        try:
            await self._welcomed(self.welcomed)
            for hook in self.on_link:
                await hook(self.link)
            if self.rollback:
                await self.report("update", {"said": self.rollback})
                self.rollback = ""
            await served
        finally:
            self.connected.clear()
            served.cancel()
            await self.link.close()

    async def _welcomed(self, welcome: dict) -> None:
        """Take on the user's name and time zone (for prompts here), and update if the main computer moved on."""
        changes = {k: str(welcome[v]) for k, v in (("name", "user"), ("timezone", "timezone")) if welcome.get(v)}
        if changes:
            with contextlib.suppress(Exception):
                await asyncio.to_thread(profile.change, profile.current().id, **changes)
        self.updater.want(str(welcome.get("commit") or ""), self.commit, self._fetch_update)

    async def _fetch_update(self, have: str, into: Path) -> str:
        if self.link is None or not self.link.up:
            raise LinkDown("the main computer went away")
        offer = await self.link.call("update", {"have": have}, timeout=600)
        await transfer.fetch(self.link, offer["bundle"], into, timeout=300)
        return str(offer["commit"])

    async def report(self, kind: str, body: dict) -> None:
        """Tell the main computer something now, if connected (the journal covers what must not be lost)."""
        if self.link is not None and self.link.up:
            with contextlib.suppress(LinkDown):
                await self.link.tell(kind, body)

    def stop(self) -> None:
        self.stopping.set()
        if self.link is not None:
            asyncio.create_task(self.link.close())

    # ── The online check ────────────────────────────────────────

    async def ping(self, body: dict) -> dict:
        """How this machine stands: free slots, load, its commit and protocol, its own Claude account's usage, and
        which of the programs asked about ("programs") it has."""
        running = self.busy()
        try:
            load = round(os.getloadavg()[0], 2)
        except OSError:
            load = None
        asked = [str(p) for p in body.get("programs") or []]
        most = preferences.max_tasks()
        return {"free": max(0, most - running), "max_tasks": most, "running": running, "load": load,
                "cores": os.cpu_count() or 0, "version": self.commit, "protocol": link.PROTOCOL,
                "usage": await asyncio.to_thread(usage.windows), "hold_until": await asyncio.to_thread(usage.hold_until),
                "updating": self.updater.pending, "update_problem": self.updater.problem} | (
                    {"has": await home.have(asked)} if asked else {})


async def websocket_dial(main: str) -> Channel:
    """Open the WebSocket to the main computer."""
    session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None, sock_connect=20))
    try:
        ws = await session.ws_connect(ws_url(main), heartbeat=HEARTBEAT, max_msg_size=link.MAX_MESSAGE)
    except BaseException:
        await session.close()
        raise
    return _Closing(ws, session)


class _Closing(WebSocketChannel):
    """A client WebSocket that closes its session with it."""

    def __init__(self, ws: aiohttp.ClientWebSocketResponse, session: aiohttp.ClientSession) -> None:
        super().__init__(ws)
        self.session = session

    async def close(self) -> None:
        await super().close()
        await self.session.close()
