"""Single-use model sessions with one started ahead for the next call, for checks that guard permission (a spoken
yes or no, hidden instructions in a tool result): CLI startup takes longer than such a check may, but no check may see
another's words, so each session answers once and is closed.

Unlike jarvis.llm.quick (one session, many timing judgements), a call here takes the warm session if it's on the
right model (else starts its own), uses it for one exchange and closes it. An unused warm session closes after
`idle` seconds.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable

from jarvis.llm.quick import exchange
from jarvis.llm.roles import Role

log = logging.getLogger("jarvis.llm.standby")


class Standby:
    def __init__(self, label: str, prompt: str, idle: float,
                 connect: Callable[[Role | str], Awaitable[object]] | None = None) -> None:
        """`connect(model)` starts a session (default: a short tool-less one with system prompt `prompt`)."""
        self.label, self.prompt, self.idle = label, prompt, idle
        self._connect = connect or self._start
        self._ready: tuple[Role | str, asyncio.Task] | None = None  # (model, starting or started session)
        self._background: set[asyncio.Task] = set()  # closing sessions and the idle timer

    async def _start(self, model: Role | str):
        from claude_agent_sdk import ClaudeSDKClient

        from jarvis.llm.claude import short_options

        client = ClaudeSDKClient(short_options(self.prompt, model))
        await client.connect()  # on failure the SDK cleans up
        return client

    async def ask(self, prompt: str, model: Role | str, warm_next: bool = False) -> str:
        """One exchange on a session of its own (the warm one if ready); returns the reply text. With `warm_next`,
        another is started for the next call. Raises RuntimeError on failure."""
        client = await self._take(model, warm_next)
        try:
            return await exchange(client, prompt)
        finally:
            self._close(client)

    def warm(self, model: Role | str) -> None:
        """Start a session on `model` for the next call unless one is ready. No-op outside a running event loop."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        if self._usable(model):
            return
        self._drop()
        task = loop.create_task(self._connect(model))
        task.add_done_callback(self._started)
        self._ready = (model, task)
        self._keep(loop.create_task(self._expire(task)))

    async def close(self) -> None:
        """Close an unused warm session (at a conversation's end)."""
        self._drop()

    async def _take(self, model: Role | str, warm_next: bool):
        task = self._usable(model)
        if task:
            self._ready = None
        if warm_next:
            self.warm(model)
        if task is None:
            return await self._connect(model)
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:  # gave up during startup: close it once started
            task.add_done_callback(lambda t: None if _failed(t) else self._close(t.result()))
            raise

    def _usable(self, model: Role | str) -> asyncio.Task | None:
        """The warm session if it's on `model`, in this loop, and not failed."""
        if self._ready and self._ready[0] == model and self._ready[1].get_loop() is asyncio.get_running_loop() \
                and not _failed(self._ready[1]):
            return self._ready[1]
        return None

    async def _expire(self, task: asyncio.Task) -> None:
        await asyncio.sleep(self.idle)
        if self._ready and self._ready[1] is task:
            self._drop()

    def _drop(self) -> None:
        if self._ready is None:
            return
        task, self._ready = self._ready[1], None
        try:
            if task.get_loop() is not asyncio.get_running_loop():
                return  # belongs to a dead event loop
        except RuntimeError:
            return
        if not task.done():
            task.cancel()  # still starting: the SDK cleans up
        elif not _failed(task):
            self._close(task.result())

    def _started(self, task: asyncio.Task) -> None:
        if not task.cancelled() and (e := task.exception()) is not None:
            log.warning("starting a session for the %s failed: %s", self.label, type(e).__name__)

    def _close(self, client) -> None:
        async def disconnect() -> None:
            try:
                await client.disconnect()
            except Exception as e:
                log.debug("closing a %s session failed: %s", self.label, type(e).__name__)

        self._keep(asyncio.get_running_loop().create_task(disconnect()))

    def _keep(self, task: asyncio.Task) -> None:
        self._background.add(task)
        task.add_done_callback(self._background.discard)


def _failed(task: asyncio.Task) -> bool:
    return task.done() and (task.cancelled() or task.exception() is not None)
