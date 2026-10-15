"""Fake `claude` CLI (ClaudeSDKClient) for testing the voice brain, jarvis.brain.cli.

Each turn is a script of steps the CLI streams:
  message    built with the helpers below; a ResultMessage ends the turn
  Hold       blocks until the test calls release() or the CLI is interrupted
  exception  raised mid-stream
FakeCli records prompts sent, interrupts, model switches and close. Every CLI the brain starts shares one turn queue.
"""

import asyncio
from dataclasses import dataclass, field

from claude_agent_sdk import AssistantMessage, ResultMessage, StreamEvent, ToolResultBlock, ToolUseBlock, UserMessage


def text(words: str, parent: str | None = None) -> StreamEvent:
    """Streamed reply text; with `parent`, a subagent's (not spoken)."""
    return StreamEvent(uuid="e", session_id="s", parent_tool_use_id=parent,
                       event={"type": "content_block_delta", "delta": {"type": "text_delta", "text": words}})


def next_turn() -> StreamEvent:
    """Start of a new model turn, as after a tool call."""
    return StreamEvent(uuid="e", session_id="s", event={"type": "message_start"})


def tool_call(tool_id: str, name: str, args: dict | None = None) -> AssistantMessage:
    return AssistantMessage(content=[ToolUseBlock(id=tool_id, name=name, input=args or {})], model="m")


def tool_done(tool_id: str, output: str = "") -> UserMessage:
    return UserMessage(content=[ToolResultBlock(tool_use_id=tool_id, content=output)])


def result(error: str = "") -> ResultMessage:
    """End of turn; failed with `error` if given."""
    return ResultMessage(subtype="error_during_execution" if error else "success", duration_ms=1, duration_api_ms=1,
                         is_error=bool(error), num_turns=1, session_id="s", result=error or None)


class Hold:
    """Script step that blocks until `release()` or an interrupt; `reached` is set when the stream gets here."""

    def __init__(self) -> None:
        self.reached, self.released = asyncio.Event(), asyncio.Event()

    def release(self) -> None:
        self.released.set()


@dataclass
class FakeCli:
    """Each query pops the next of `turns`; receive_response resumes it up to its result.

    An interrupt ends the turn with a result, unless `interrupt_fails` is raised instead; `drain_hangs` keeps the
    stream open after an interrupt.
    """

    turns: list[list] = field(default_factory=list)
    interrupt_fails: Exception | None = None
    drain_hangs: bool = False
    sent: list = field(default_factory=list)  # str prompts, or a list of message dicts for photos
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
    """CliBrain on FakeClis built with `cli`; `logged` holds memory's (user, assistant, interrupted) per exchange."""

    def __init__(self, monkeypatch, turns: list[list], **cli) -> None:
        from jarvis.brain import cli as brain_cli
        from jarvis.state import memory

        self.turns, self.clis, self.logged = turns, [], []

        def started(options) -> FakeCli:
            self.clis.append(FakeCli(self.turns, **cli))
            return self.clis[-1]

        monkeypatch.setattr(brain_cli, "_options", lambda gate, jobs, kept=None, budget=None: type("Options", (), {"model": "claude-opus-5-5"}))
        monkeypatch.setattr(brain_cli, "ClaudeSDKClient", started)
        monkeypatch.setattr(memory, "log_exchange", lambda user, assistant, approved=(), interrupted=False:
                            self.logged.append((user, assistant, interrupted)))
        self.brain = brain_cli.CliBrain()

    @property
    def cli(self) -> FakeCli:
        """Current or most recent CLI."""
        return self.clis[-1]

    async def say(self, words: str) -> str:
        """Full spoken reply to `words`."""
        return "".join([chunk async for chunk in self.brain.respond(words)])

    async def talk_over(self, words: str, hold: Hold) -> str:
        """Say `words`, then barge in (cancel, as LiveKit does) when the reply reaches `hold`; return what was said."""
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
