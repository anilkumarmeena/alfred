"""Short judgements on a fast model during a voice session, from a CLI session kept warm so each one waits only for the
model: whether the user has finished their thought (jarvis.voice.turn), and whether now is the moment to bring
something up (jarvis.voice.floor).

Unlike the yes-or-no check (jarvis.safety.consent), which starts a session per check so nothing carries from one
reply to the next, these judge only timing, never whether anything may happen, so one session serves many calls:
starting a CLI takes longer than a judgement may. It's replaced after USES calls, so its history stays short, and
whenever a call fails or runs out of time mid-answer. The system prompt says each message stands alone.

A caller that gives up (its timeout, or LiveKit cancelling a turn's wait because the user went on) never leaves the
session mid-answer: the call already sent is read to its end in the background, and one still queued is skipped.
"""

import asyncio
import json
import logging
import re
import time
from collections.abc import Awaitable, Callable

from jarvis.core.config import FAST_MODEL

log = logging.getLogger("jarvis.brain.quick")

USES = 30  # calls on one session before it's replaced
LIMIT_SECONDS = 20  # a call still unanswered after this has hung: its session is dropped
_background: set[asyncio.Task] = set()


class Quick:
    """One warm session on `model` with `prompt` as its system prompt, one call at a time."""

    def __init__(self, label: str, prompt: str, model: str = FAST_MODEL,
                 connect: Callable[[], Awaitable[object]] | None = None) -> None:
        self.label, self.prompt, self.model = label, prompt, model
        self._connect = connect or self._start
        self._session: asyncio.Task | None = None
        self._used = 0
        self._lock = asyncio.Lock()

    async def _start(self):
        from claude_agent_sdk import ClaudeSDKClient

        from jarvis.brain.sdk import short_options

        client = ClaudeSDKClient(short_options(self.prompt, self.model))
        await client.connect()  # on failure the SDK closes what it started
        return client

    def warm(self) -> None:
        """Start the session now if none is starting or ready, so the next call doesn't wait for it. Nothing happens
        outside a running event loop."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        if self._session is None or _failed(self._session) or self._session.get_loop() is not loop:
            self._session = loop.create_task(self._connect())
            self._session.add_done_callback(self._started)

    def _started(self, task: asyncio.Task) -> None:
        if not task.cancelled() and (e := task.exception()) is not None:
            log.warning("starting the %s session failed: %s", self.label, type(e).__name__)

    async def ask(self, text: str, timeout: float) -> str | None:
        """The model's reply to `text`, or None if it failed or took longer than `timeout` seconds."""
        started = time.monotonic()
        gone = asyncio.Event()  # set when the caller stops waiting: a call not yet sent is skipped
        call = asyncio.ensure_future(self._ask(text, gone))
        _keep(call)
        try:
            return await asyncio.wait_for(asyncio.shield(call), timeout)
        except TimeoutError:
            log.info("%s: no answer within %.1f s", self.label, timeout)
            return None
        except Exception as e:  # the type alone: what the user said stays out of the log
            log.warning("%s failed after %.1f s: %s", self.label, time.monotonic() - started, type(e).__name__)
            return None
        finally:
            gone.set()

    async def _ask(self, text: str, gone: asyncio.Event) -> str | None:
        async with self._lock:
            if gone.is_set():
                return None
            self.warm()
            client = await asyncio.shield(self._session)
            try:
                async with asyncio.timeout(LIMIT_SECONDS):
                    reply = await _exchange(client, text)
            except BaseException:
                self._drop()  # it may still be answering: the next call gets a fresh one
                raise
            self._used += 1
            if self._used >= USES:
                self._drop()
            self.warm()
            return reply

    def _drop(self) -> None:
        task, self._session, self._used = self._session, None, 0
        if task is not None:
            task.add_done_callback(lambda t: None if _failed(t) else _close(t.result()))

    async def close(self) -> None:
        """The conversation is over: the session goes."""
        task, self._session = self._session, None
        if task is None:
            return
        if not task.done():
            task.cancel()  # still starting: the SDK closes what it started
        elif not _failed(task):
            _close(task.result())


async def _exchange(client, text: str) -> str:
    from claude_agent_sdk import RateLimitEvent, ResultMessage

    from jarvis.brain import usage

    await client.query(text)
    async for m in client.receive_response():
        if isinstance(m, RateLimitEvent):
            usage.record(m.rate_limit_info)
        elif isinstance(m, ResultMessage):
            if m.is_error:
                raise RuntimeError(f"the model call failed: {m.subtype}")
            return m.result or ""
    raise RuntimeError("the model call ended without a reply")


def untagged(text: str) -> str:
    """Spoken words for a prompt, on one line, with no < or > to close the tags they're quoted in."""
    return " ".join(text.replace("<", " ").replace(">", " ").split())


def found(reply: str | None) -> dict:
    """The one JSON object a reply holds, or {} if it holds none."""
    if not reply or not (m := re.search(r"\{.*?\}", reply, re.DOTALL)):
        return {}
    try:
        value = json.loads(m.group(0))
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _failed(task: asyncio.Task) -> bool:
    return task.done() and (task.cancelled() or task.exception() is not None)


def _close(client) -> None:
    async def disconnect() -> None:
        try:
            await client.disconnect()
        except Exception as e:
            log.debug("closing a quick session failed: %s", type(e).__name__)

    _keep(asyncio.get_running_loop().create_task(disconnect()))


def _keep(task: asyncio.Task) -> None:
    _background.add(task)
    task.add_done_callback(_background.discard)
