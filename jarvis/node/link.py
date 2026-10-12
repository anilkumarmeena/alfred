"""The link between the main computer and a machine: mutual sign-in, then signed requests both ways.

A machine dials out to the main computer (a WebSocket to /api/node, passed on to the task runner), so nothing
listens on the machine. Both sides then prove who they are with their Ed25519 keys (`handshake_*`), and every request
either side sends afterwards is signed over the session and its own number, which only goes up, so nothing can be
injected or replayed. Replies travel back on the same channel.

  Channel   send / receive JSON messages (a WebSocket, or an in-memory pair in tests)
  Link      call (request, wait for its reply), tell (one-way), serve (dispatch to handlers)
"""

import asyncio
import contextlib
import itertools
import json
import logging
import secrets
from collections.abc import Awaitable, Callable
from typing import Protocol

import aiohttp
from aiohttp import web

from jarvis.node import keys

log = logging.getLogger("jarvis.node.link")

PROTOCOL = 1  # bumped when messages change incompatibly; a machine on another one takes no new work
CONTEXT = "jarvis-link/1"
HANDSHAKE_SECONDS = 10.0
MAX_MESSAGE = 8 * 1024 * 1024  # bytes per message; files go in chunks well under it


class LinkDown(ConnectionError):
    """The other side is unreachable, or went away mid-request."""


class Refused(RuntimeError):
    """The other side answered a request with an error."""


class Channel(Protocol):
    async def send(self, message: dict) -> None: ...
    async def receive(self) -> dict: ...  # LinkDown once closed
    async def close(self) -> None: ...


class WebSocketChannel:
    """A Channel over an aiohttp WebSocket, server or client side."""

    def __init__(self, ws: web.WebSocketResponse | aiohttp.ClientWebSocketResponse) -> None:
        self.ws = ws

    async def send(self, message: dict) -> None:
        try:
            await self.ws.send_str(json.dumps(message))
        except (ConnectionError, RuntimeError, aiohttp.ClientError) as e:
            raise LinkDown(str(e) or "the connection closed") from e

    async def receive(self) -> dict:
        msg = await self.ws.receive()
        if msg.type != aiohttp.WSMsgType.TEXT:
            raise LinkDown("the connection closed")
        try:
            found = json.loads(msg.data)
        except ValueError:
            raise LinkDown("a message wasn't JSON") from None
        if not isinstance(found, dict):
            raise LinkDown("a message wasn't an object")
        return found

    async def close(self) -> None:
        with contextlib.suppress(Exception):
            await self.ws.close()


class MemoryChannel:
    """One end of an in-memory channel pair (tests, and the machine's own loopback)."""

    def __init__(self, inbox: asyncio.Queue, outbox: asyncio.Queue) -> None:
        self.inbox, self.outbox, self.closed = inbox, outbox, False

    async def send(self, message: dict) -> None:
        if self.closed:
            raise LinkDown("the connection closed")
        await self.outbox.put(json.loads(json.dumps(message)))  # as a real wire would: copied, JSON only

    async def receive(self) -> dict:
        if self.closed:
            raise LinkDown("the connection closed")
        message = await self.inbox.get()
        if message is None:
            self.closed = True
            raise LinkDown("the connection closed")
        return message

    async def close(self) -> None:
        if not self.closed:
            self.closed = True
            await self.outbox.put(None)
            await self.inbox.put(None)


def memory_pair() -> tuple[MemoryChannel, MemoryChannel]:
    a, b = asyncio.Queue(), asyncio.Queue()
    return MemoryChannel(a, b), MemoryChannel(b, a)


def canonical(body: object) -> str:
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _signed(session: str, number: int, kind: str, body: object) -> bytes:
    return f"{CONTEXT}|request|{session}|{number}|{kind}|{canonical(body)}".encode()


# ── Sign-in ─────────────────────────────────────────────────────


def _proof(role: str, node: int, theirs: str, ours: str) -> bytes:
    return f"{CONTEXT}|{role}|{node}|{theirs}|{ours}".encode()


async def _expect(channel: Channel, kind: str) -> dict:
    message = await asyncio.wait_for(channel.receive(), HANDSHAKE_SECONDS)
    if message.get("kind") == "refused":
        raise Refused(str(message.get("error") or "refused"))
    if message.get("kind") != kind:
        raise Refused(f"expected {kind}, got {message.get('kind')}")
    return message


async def handshake_machine(channel: Channel, node: int, own_key: str, main_public: str, hello: dict) -> tuple[str, dict]:
    """The machine's side: say hello (with `hello` facts), check the main computer's proof, prove itself.

    Returns (session, the welcome's body). Refused if either side's proof fails."""
    mine = secrets.token_hex(16)
    await channel.send({"kind": "hello", "node": node, "nonce": mine, "protocol": PROTOCOL} | {"facts": hello})
    challenge = await _expect(channel, "challenge")
    theirs = str(challenge.get("nonce") or "")
    if len(theirs) < 32 or not keys.verify(main_public, _proof("main", node, mine, theirs), str(challenge.get("sig"))):
        await channel.close()
        raise Refused("the main computer couldn't prove who it is; pair again if its key changed")
    await channel.send({"kind": "proof", "sig": keys.sign(own_key, _proof("node", node, mine, theirs))})
    welcome = await _expect(channel, "welcome")
    return mine + theirs, welcome.get("body") or {}


async def handshake_main(channel: Channel, main_key: str, public_of: Callable[[int], str | None]) -> tuple[int, str, dict]:
    """The main computer's side: read hello, prove itself, check the machine's proof.

    `public_of(node)` is the paired machine's public key, or None. Returns (node, session, hello facts); the caller
    sends the welcome. Refused (after telling the machine why) on any mismatch."""
    hello = await _expect(channel, "hello")
    try:
        node = int(hello["node"])
    except (KeyError, TypeError, ValueError):
        raise Refused("hello without a machine number") from None
    theirs = str(hello.get("nonce") or "")
    if len(theirs) < 32 or not (public := public_of(node)):
        await refuse(channel, "this machine isn't paired with this computer; pair it again from Settings › Machines")
        raise Refused(f"machine {node} isn't paired")
    mine = secrets.token_hex(16)
    await channel.send({"kind": "challenge", "nonce": mine, "sig": keys.sign(main_key, _proof("main", node, theirs, mine))})
    proof = await _expect(channel, "proof")
    if not keys.verify(public, _proof("node", node, theirs, mine), str(proof.get("sig"))):
        await refuse(channel, "the machine's key doesn't match the one it paired with")
        raise Refused(f"machine {node} failed its proof")
    facts = hello.get("facts") if isinstance(hello.get("facts"), dict) else {}
    return node, theirs + mine, facts | {"protocol": hello.get("protocol")}


async def refuse(channel: Channel, why: str) -> None:
    with contextlib.suppress(Exception):
        await channel.send({"kind": "refused", "error": why})
    await channel.close()


# ── Requests after sign-in ───────────────────────────────────────

Handler = Callable[[dict], Awaitable[dict | None]]


class Link:
    """Signed requests and replies over a signed-in channel. `handlers` answer the other side's requests by kind."""

    def __init__(self, channel: Channel, session: str, own_key: str, peer_public: str,
                 handlers: dict[str, Handler] | None = None, name: str = "link") -> None:
        self.channel, self.session, self.own_key, self.peer_public = channel, session, own_key, peer_public
        self.handlers = handlers or {}
        self.name = name
        self._numbers = itertools.count(1)
        self._last_seen = 0  # highest request number received: anything not above it is refused as a replay
        self._waiting: dict[int, asyncio.Future] = {}
        self._running: set[asyncio.Task] = set()
        self.closed = asyncio.Event()

    @property
    def up(self) -> bool:
        return not self.closed.is_set()

    async def call(self, kind: str, body: dict | None = None, timeout: float = 30.0) -> dict:
        """Send a request and wait for its reply's body. LinkDown if the link is down or drops; Refused for an error
        reply; TimeoutError after `timeout` seconds."""
        if not self.up:
            raise LinkDown(f"{self.name} is down")
        number = next(self._numbers)
        reply = self._waiting[number] = asyncio.get_running_loop().create_future()
        try:
            await self._send(number, kind, body or {})
            return await asyncio.wait_for(asyncio.shield(reply), timeout)
        finally:
            self._waiting.pop(number, None)

    async def tell(self, kind: str, body: dict | None = None) -> None:
        """Send a request whose reply nobody waits for. LinkDown if the link is down."""
        if not self.up:
            raise LinkDown(f"{self.name} is down")
        await self._send(next(self._numbers), kind, body or {}, reply=False)

    async def _send(self, number: int, kind: str, body: dict, reply: bool = True) -> None:
        sig = keys.sign(self.own_key, _signed(self.session, number, kind, body))
        await self.channel.send({"id": number, "kind": kind, "body": body, "sig": sig} | ({} if reply else {"quiet": 1}))

    async def serve(self) -> None:
        """Read until the channel closes, answering requests and settling replies. Ends quietly on LinkDown."""
        try:
            while True:
                message = await self.channel.receive()
                if "re" in message:
                    self._settle(message)
                else:
                    self._request(message)
        except LinkDown:
            pass
        finally:
            self.closed.set()
            for waiting in self._waiting.values():
                if not waiting.done():
                    waiting.set_exception(LinkDown(f"{self.name} went away"))
            for task in list(self._running):
                task.cancel()

    async def close(self) -> None:
        await self.channel.close()
        self.closed.set()

    def _settle(self, message: dict) -> None:
        waiting = self._waiting.get(message.get("re"))  # type: ignore[arg-type]
        if waiting is None or waiting.done():
            return
        if "error" in message:
            waiting.set_exception(Refused(str(message["error"])))
        else:
            waiting.set_result(message.get("body") or {})

    def _request(self, message: dict) -> None:
        number, kind, body = message.get("id"), str(message.get("kind") or ""), message.get("body") or {}
        if not isinstance(number, int) or number <= self._last_seen or not isinstance(body, dict) \
                or not keys.verify(self.peer_public, _signed(self.session, number, kind, body), str(message.get("sig"))):
            log.warning("%s: dropped a request that wasn't signed for this session (%s)", self.name, kind)
            return
        self._last_seen = number
        task = asyncio.create_task(self._answer(number, kind, body, not message.get("quiet")))
        self._running.add(task)
        task.add_done_callback(self._running.discard)

    async def _answer(self, number: int, kind: str, body: dict, reply: bool) -> None:
        handler = self.handlers.get(kind)
        try:
            if handler is None:
                raise Refused(f"no {kind} here")
            out = {"re": number, "body": await handler(body) or {}}
        except asyncio.CancelledError:
            raise
        except (Refused, ValueError, KeyError, TypeError, OSError, RuntimeError) as e:
            out = {"re": number, "error": str(e) or type(e).__name__}
        except Exception as e:
            log.exception("%s: %s failed", self.name, kind)
            out = {"re": number, "error": f"{kind} failed: {type(e).__name__}"}
        if reply:
            with contextlib.suppress(LinkDown):
                await self.channel.send(out)
