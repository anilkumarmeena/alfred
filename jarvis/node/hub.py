"""The main computer's end of its machines' links, held by the task runner.

  accept   a machine dials in (the web server passes /api/node on to the runner): sign-in, welcome, then its link is
           served until it drops. A newer connection from the same machine replaces an older one.
  check    the online check: a ping answered within PING_SECONDS, never an old "online" flag
  update   a machine on another commit asks for the code: a bundle of this computer's checked-out commit

What a machine sends back (task steps, approval requests, command output) arrives through `handlers`, which the parts
that send it work register (see jarvis.node.remote).
"""

import asyncio
import logging
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

from jarvis.config import DATA_DIR, ROOT
from jarvis.llm import usage
from jarvis.node import bundles, keys, link, machines
from jarvis.node.link import Channel, Link, LinkDown, Refused
from jarvis.node.transfer import Outbox
from jarvis.storage import profile

log = logging.getLogger("jarvis.node.hub")

PING_SECONDS = 5.0


@dataclass
class Conn:
    """A machine's live link."""
    node: int
    link: Link
    facts: dict
    since: float = field(default_factory=time.time)


def view(m: machines.Machine, check: dict | None, commit: str) -> dict:
    """A machine as the panel and the brain see it: who it is, whether it answered just now, and how it stands."""
    out = {"id": m.id, "name": m.name, "aliases": m.aliases, "status": m.status, "fingerprint": m.fingerprint,
           "host": m.host, "os": m.os, "cores": m.cores, "ram_gb": m.ram_gb, "tools": m.tools, "version": m.version,
           "up_to_date": bool(commit) and m.version == commit, "last_seen": m.last_seen or None,
           "online": bool(check), "usage": usage.said(kept=m.usage) if m.usage else ""}
    if check:
        out |= {k: check.get(k) for k in ("free", "max_tasks", "running", "load", "updating", "update_problem")}
        out["takes_work"] = bool(check.get("same_protocol"))
    return out


class Hub:
    """The machines' links, and what they ask of this computer."""

    def __init__(self, root: Path = ROOT, spool: Path = DATA_DIR / "node" / "outbox") -> None:
        self.root, self.spool = root, spool  # spool: files offered to machines, removed once fetched
        self.conns: dict[int, Conn] = {}
        self.outbox = Outbox()
        self.handlers: dict[str, Callable[[int, dict], Awaitable[dict | None]]] = {"update": self._update}
        self.on_connect: list[Callable[[int], Awaitable[None]]] = []  # e.g. sending what a machine missed meanwhile
        self.on_lost: list[Callable[[int], None]] = []  # a machine's link dropped
        self.lost_at: dict[int, float] = {}  # when each machine's link last dropped
        self._catching_up: set[asyncio.Task] = set()  # on_connect hooks running, held so they aren't collected

    # ── Links ────────────────────────────────────────────────────

    async def accept(self, channel: Channel) -> None:
        """Sign a machine in and serve its link until it drops. Refusals are logged, never raised."""
        try:
            key = await asyncio.to_thread(machines.main_key)
            node, session, facts = await link.handshake_main(channel, key, machines.public_of)
        except (Refused, LinkDown, TimeoutError) as e:
            log.warning("a machine's connection was refused: %s", e)
            await channel.close()
            return
        found = machines.need(node)
        if not found.paired:
            await link.refuse(channel, "this machine is waiting to be confirmed: check its fingerprint in Settings › "
                                       "Machines on the main computer")
            return
        handlers = {kind: self._bound(node, handler) for kind, handler in self.handlers.items()}
        handlers["fetch"] = self.outbox.fetch
        conn = Conn(node, Link(channel, session, key, found.public_key, handlers, name=found.name), facts)
        await asyncio.to_thread(machines.saw, node, facts)
        try:
            await channel.send({"kind": "welcome", "body": await self.welcome(found)})
        except LinkDown:
            return
        if old := self.conns.get(node):
            await old.link.close()
        self.conns[node] = conn
        self.lost_at.pop(node, None)
        log.info("%s connected (protocol %s, commit %s)", found.name, facts.get("protocol"), str(facts.get("version"))[:7])
        served = asyncio.create_task(conn.link.serve())
        for hook in self.on_connect:
            hooked = asyncio.create_task(self._quietly(hook(node), f"catching up with {found.name}"))
            self._catching_up.add(hooked)
            hooked.add_done_callback(self._catching_up.discard)
        try:
            await served
        finally:
            if self.conns.get(node) is conn:
                del self.conns[node]
                self.lost_at[node] = time.time()
                await asyncio.to_thread(machines.saw, node, {})
                log.info("%s went away", found.name)
                for hook in self.on_lost:
                    hook(node)

    @staticmethod
    async def _quietly(coro: Awaitable, what: str) -> None:
        try:
            await coro
        except Exception:
            log.exception("%s failed", what)

    def _bound(self, node: int, handler: Callable[[int, dict], Awaitable[dict | None]]):
        async def call(body: dict) -> dict | None:
            return await handler(node, body)
        return call

    async def welcome(self, found: machines.Machine) -> dict:
        """What a machine is told on connecting: who it works for, their time zone, and the code to run."""
        mine = profile.current()
        return {"node": found.id, "name": found.name, "user": mine.name, "timezone": mine.timezone,
                "commit": await bundles.head(self.root), "protocol": link.PROTOCOL}

    def connected(self, node: int) -> Link | None:
        conn = self.conns.get(node)
        return conn.link if conn and conn.link.up else None

    async def accept_websocket(self, ws) -> None:
        """A machine linking in through the runner's socket (jarvis.tasks.api)."""
        await self.accept(link.WebSocketChannel(ws))

    async def close(self) -> None:
        for conn in list(self.conns.values()):
            await conn.link.close()

    # ── Online check ─────────────────────────────────────────────

    async def check(self, node: int, timeout: float = PING_SECONDS, needs: list[str] = ()) -> dict | None:
        """The machine's answer to a ping, plus "online": True; None if it doesn't answer in time. With `needs`
        (programs a command runs), the answer says which of them it has ("has")."""
        if (up := self.connected(node)) is None:
            return None
        try:
            got = await up.call("ping", {"programs": list(needs)} if needs else {}, timeout)
        except (LinkDown, Refused, TimeoutError):
            return None
        await asyncio.to_thread(machines.saw, node, got)
        return got | {"online": True, "same_protocol": got.get("protocol") == link.PROTOCOL}

    async def check_all(self, timeout: float = PING_SECONDS, needs: list[str] = ()) -> dict[int, dict | None]:
        """Every paired machine's online check, at once."""
        found = await asyncio.to_thread(machines.paired)
        answers = await asyncio.gather(*(self.check(m.id, timeout, needs) for m in found))
        return {m.id: a for m, a in zip(found, answers)}

    # ── What the panel and the brain see ─────────────────────────

    async def listing(self) -> dict:
        """Every machine with its live online check, this computer, and where each kind of job runs."""
        checks = await self.check_all()
        commit = await bundles.head(self.root)
        shown = [view(m, checks.get(m.id), commit) for m in await asyncio.to_thread(machines.load)]
        return {"machines": shown, "placement": await asyncio.to_thread(machines.placement), "kinds": machines.KINDS,
                "commit": commit, "fingerprint": keys.fingerprint(await asyncio.to_thread(machines.main_public))}

    async def remove(self, node: int) -> dict:
        """Forget a machine and drop its link."""
        gone = await asyncio.to_thread(machines.remove, node)
        if conn := self.conns.pop(node, None):
            await conn.link.close()
        return {"removed": gone.name}

    # ── Updates ──────────────────────────────────────────────────

    async def _update(self, node: int, body: dict) -> dict:
        """A machine on another commit asks for this one: offer a bundle of it (only what it lacks, when it can)."""
        commit = await bundles.head(self.root)
        if not commit:
            raise Refused("this computer's JARVIS isn't a git checkout, so it can't send its code")
        bundle = await bundles.make_update(self.root, str(body.get("have") or ""),
                                           self.spool / f"update-{secrets.token_hex(6)}.bundle")
        log.info("sending machine %s the code at %s", node, commit[:7])
        return {"commit": commit, "bundle": self.outbox.offer(bundle, remove=True)}
