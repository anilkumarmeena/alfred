"""A stand-in for the `claude` CLI behind ClaudeSDKClient, for tests of the voice brain that drives it
(jarvis.brain.cli). Each turn plays a script of what the CLI would stream: messages, made with the helpers here,
`Hold`s where a tool keeps running until the test lets it end, and exceptions the stream breaks off with. The fake
keeps what it was sent, how often it was interrupted, the models it was switched to and whether it was closed.

The `voice` fixture (tests/conftest.py) puts a CliBrain on fake CLIs: each one the brain starts, at first or again
after closing one, plays the turns still left."""

import asyncio
from dataclasses import dataclass, field

from claude_agent_sdk import AssistantMessage, ResultMessage, StreamEvent, ToolResultBlock, ToolUseBlock, UserMessage


def text(words: str, parent: str | None = None) -> StreamEvent:
    """Words of the reply as they stream; with `parent`, a subagent's, which aren't spoken."""
    return StreamEvent(uuid="e", session_id="s", parent_tool_use_id=parent,
                       event={"type": "content_block_delta", "delta": {"type": "text_delta", "text": words}})


def next_turn() -> StreamEvent:
    """A new model turn starting, as after a tool call."""
    return StreamEvent(uuid="e", session_id="s", event={"type": "message_start"})


def tool_call(tool_id: str, name: str, args: dict | None = None) -> AssistantMessage:
    return AssistantMessage(content=[ToolUseBlock(id=tool_id, name=name, input=args or {})], model="m")


def tool_done(tool_id: str, output: str = "") -> UserMessage:
    return UserMessage(content=[ToolResultBlock(tool_use_id=tool_id, content=output)])


def result(error: str = "") -> ResultMessage:
    """The turn's end; with `error`, a failed one, saying that."""
    return ResultMessage(subtype="error_during_execution" if error else "success", duration_ms=1, duration_api_ms=1,
                         is_error=bool(error), num_turns=1, session_id="s", result=error or None)


class Hold:
    """A point in the script the reply waits at until the test calls `release()`, or the CLI is interrupted (which
    kills whatever was running). `reached` is set once the reply gets there."""

    def __init__(self) -> None:
        self.reached, self.released = asyncio.Event(), asyncio.Event()

    def release(self) -> None:
        self.released.set()


@dataclass
class FakeCli:
    """The CLI: each query starts the next of `turns` (shared by every FakeCli a brain starts), which
    receive_response plays from where it last stopped, up to its result. An interrupt ends the turn there, with a
    result, unless `interrupt_fails` says what it raises instead; `drain_hangs` keeps the stream open after one."""

    turns: list[list] = field(default_factory=list)
    interrupt_fails: Exception | None = None
    drain_hangs: bool = False
    sent: list = field(default_factory=list)  # each query's prompt; photos' messages as a list of their dicts
    models: list[str] = field(default_factory=list)
    interrupts: int = 0
    connected: bool = False
    closed: bool = False
    _script: list = field(default_factory=list)
    _stop: asyncio.Event | None = None

    @property
    def prompts(self) -> list[str]:
        return [p if isinstance(p, str) else p[-1]["message"]["content"][-1]["text"] for p in self.sent]

    async def connect(self) -> None:
        self.connected = True

    async def disconnect(self) -> None:
        self.closed = True

    async def set_model(self, model: str) -> None:
        self.models.append(model)

    async def query(self, prompt) -> None:
        self.sent.append(prompt if isinstance(prompt, str) else [m async for m in prompt])
        self._script = list(self.turns.pop(0)) if self.turns else [text("Okay."), result()]
        self._stop = asyncio.Event()

    async def interrupt(self) -> None:
        self.interrupts += 1
        if self.interrupt_fails:
            raise self.interrupt_fails
        assert self._stop is not None, "interrupted before any query"
        self._stop.set()

    async def receive_response(self):
        assert self._stop is not None, "read before any query"
        while self._script:
            if self._stop.is_set():
                if self.drain_hangs:
                    await asyncio.Event().wait()
                self._script = []
                yield result("interrupted")
                return
            step = self._script[0]
            if isinstance(step, Hold):
                step.reached.set()
                stopped = asyncio.ensure_future(self._stop.wait())
                released = asyncio.ensure_future(step.released.wait())
                try:
                    await asyncio.wait([stopped, released], return_when=asyncio.FIRST_COMPLETED)
                finally:
                    stopped.cancel(), released.cancel()
                if self._stop.is_set():
                    continue
                self._script.pop(0)
            elif isinstance(step, BaseException):
                self._script.pop(0)
                raise step
            else:
                self._script.pop(0)
                yield step
                if isinstance(step, ResultMessage):
                    return


class Voice:
    """A CliBrain on fake CLIs, each made with `cli` (FakeCli's settings) and playing the turns left in `turns`,
    with what memory would log of each exchange kept in `logged`: (user, assistant, interrupted)."""

    def __init__(self, monkeypatch, turns: list[list], **cli) -> None:
        from jarvis.brain import cli as brain_cli
        from jarvis.state import memory

        self.turns, self.clis, self.logged = turns, [], []

        def started(options) -> FakeCli:
            self.clis.append(FakeCli(self.turns, **cli))
            return self.clis[-1]

        monkeypatch.setattr(brain_cli, "_options", lambda gate, jobs: type("Options", (), {"model": "claude-opus-5-5"}))
        monkeypatch.setattr(brain_cli, "ClaudeSDKClient", started)
        monkeypatch.setattr(memory, "log_exchange", lambda user, assistant, approved=(), interrupted=False:
                            self.logged.append((user, assistant, interrupted)))
        self.brain = brain_cli.CliBrain()

    @property
    def cli(self) -> FakeCli:
        """The CLI the brain has now, or had last."""
        return self.clis[-1]

    async def say(self, words: str) -> str:
        """The user says `words`: the brain's whole spoken reply."""
        return "".join([chunk async for chunk in self.brain.respond(words)])

    async def talk_over(self, words: str, hold: Hold) -> str:
        """The user says `words`, then speaks again once the reply reaches `hold`, which cuts it off as LiveKit
        does (cancelling it): what was spoken of it by then."""
        spoken: list[str] = []

        async def listen() -> None:
            async for chunk in self.brain.respond(words):
                spoken.append(chunk)

        reply = asyncio.create_task(listen())
        await asyncio.wait_for(hold.reached.wait(), 2)
        reply.cancel()
        try:
            await reply
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("the reply ended before it was talked over")
        return "".join(spoken)
