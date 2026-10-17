"""Short timing judgements on a fast model from a warm CLI session (model routing, when to speak up).

Unlike jarvis.safety.consent (one session per check), these judge only timing, never permission, so one session
serves many calls. It's replaced after USES calls and after any failure or timeout. Each message stands alone.

A caller that gives up never leaves the session mid-answer: a sent call is drained in the background, a queued one
is skipped.
"""

import asyncio
import json
import logging
import re
import time
from collections.abc import Awaitable, Callable

from jarvis.llm.roles import Role

log = logging.getLogger("jarvis.llm.quick")

USES = 30  # calls on one session before it's replaced
LIMIT_SECONDS = 20  # unanswered after this: hung, drop the session
_background: set[asyncio.Task] = set()


class Quick:
    """One warm session on `model` with system prompt `prompt`; one call at a time."""

    def __init__(self, label: str, prompt: str, model: Role | str = "fast",
                 connect: Callable[[], Awaitable[object]] | None = None) -> None:
        self.label, self.prompt, self.model = label, prompt, model
        self._connect = connect or self._start
        self._session: asyncio.Task | None = None
        self._used = 0
        self._lock = asyncio.Lock()

    async def _start(self):
        from claude_agent_sdk import ClaudeSDKClient

        from jarvis.llm.claude import short_options

        client = ClaudeSDKClient(short_options(self.prompt, self.model))
        await client.connect()  # SDK cleans up on failure
        return client

    def warm(self) -> None:
        """Start a session if none is starting or ready. No-op outside a running loop."""
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
        """The reply to `text`, or None on failure or after `timeout` seconds."""
        started = time.monotonic()
        gone = asyncio.Event()  # caller stopped waiting: skip if not yet sent
        call = asyncio.ensure_future(self._ask(text, gone))
        _keep(call)
        try:
            return await asyncio.wait_for(asyncio.shield(call), timeout)
        except TimeoutError:
            log.info("%s: no answer within %.1f s", self.label, timeout)
            return None
        except Exception as e:  # type only: keep user words out of the log
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
                    reply = await exchange(client, text)
            except BaseException:
                self._drop()  # may still be answering; next call gets a fresh one
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
        """Close the session."""
        task, self._session = self._session, None
        if task is None:
            return
        if not task.done():
            task.cancel()  # still starting; SDK cleans up
        elif not _failed(task):
            _close(task.result())


async def exchange(client, text: str) -> str:
    """Send `text` on a session and return the reply, recording rate-limit events; RuntimeError on an error."""
    from claude_agent_sdk import RateLimitEvent, ResultMessage

    from jarvis.llm import claude

    await client.query(text)
    async for m in client.receive_response():
        if isinstance(m, RateLimitEvent):
            claude.record(m.rate_limit_info, client.options)  # under the login it ran on
        elif isinstance(m, ResultMessage):
            if m.is_error:
                raise RuntimeError(f"the model call failed: {m.subtype}")
            return m.result or ""
    raise RuntimeError("the model call ended without a reply")


def untagged(text: str) -> str:
    """Spoken words on one line with < and > removed, safe to quote inside tags."""
    return " ".join(text.replace("<", " ").replace(">", " ").split())


def found(reply: str | None) -> dict:
    """The first JSON object in a reply, or {}."""
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
