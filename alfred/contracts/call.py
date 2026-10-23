"""What a voice call carries that others reach through the conversation (alfred.state.conversation): its lookups,
its tool calls carrying on in the background, and its speaker check. The voice process fills them in with the real
ones: alfred.brain.side.Jobs, alfred.brain.kept.Kept and alfred.voice.speaker.SpeakerCheck.

Each lookup and kept call has a Helper: how the task list shows it beside tasks (alfred.tasks.helpers), running and
then, for HELPER_LINGER seconds, with how it ended."""

import asyncio
import secrets
import time
from collections.abc import Awaitable
from dataclasses import dataclass, field
from typing import Any, Protocol

HELPER_LINGER = 60  # seconds a finished helper stays listed with its outcome
# How a helper ended, as the task list shows it; "" while it runs
DONE, FAILED, TIMED_OUT, STOPPED = "done", "failed", "timed_out", "stopped"


@dataclass(eq=False)
class Helper:
    """A lookup or a tool call carrying on beside the conversation, as the task list shows it."""
    kind: str  # background_lookup, file_lookup, computer_lookup, or tool_call
    about: str  # its topic, or for a tool call the tool and its short arguments
    id: str = field(default_factory=lambda: secrets.token_hex(4))  # unique across calls: the panel stops one by it
    started: float = field(default_factory=time.time)
    steps: list[str] = field(default_factory=list)  # what it has done so far; the last is its activity
    outcome: str = ""
    note: str = ""  # said with the outcome ("carries on as task 12")
    ended: float | None = None

    def end(self, outcome: str, note: str = "") -> None:
        if not self.outcome:  # the first ending holds: a stop isn't a failure
            self.outcome, self.note, self.ended = outcome, note, time.time()

    def shown(self, now: float) -> bool:
        """Running, or ended within HELPER_LINGER seconds."""
        return self.ended is None or now - self.ended < HELPER_LINGER

    def view(self) -> dict:
        return {"id": self.id, "kind": self.kind, "about": self.about, "status": self.outcome or "running",
                "activity": self.note or (self.steps[-1] if self.steps else None), "started": self.started,
                "ended": self.ended}


def shown(helpers: list[Helper]) -> list[Helper]:
    """Those still to list, oldest first."""
    now = time.time()
    return sorted((h for h in helpers if h.shown(now)), key=lambda h: h.started)


class Lookups(Protocol):
    helpers: dict[asyncio.Task, Helper]  # job -> how the task list shows it, running or lately ended

    @property
    def running(self) -> dict[asyncio.Task, str]: ...  # job still going -> what it looks up

    def new_turn(self) -> None: ...

    def listed(self) -> list[Helper]: ...

    def start(self, brief: str, about: str) -> str: ...

    async def ask(self, brief: str, about: str, lookup: str = ...) -> str: ...

    async def stop(self, job: asyncio.Task) -> None: ...


class BackgroundCall(Protocol):
    tool: str  # the Alfred tool called
    args: dict
    helper: Helper | None  # set once it carries on outside its turn


class BackgroundCalls(Protocol):
    def going(self) -> list[Any]: ...  # BackgroundCall each

    def listed(self) -> list[Helper]: ...

    async def stop(self, calls: list[Any], first: Awaitable[Any] | None = None) -> Any: ...


class SpeakerCheck(Protocol):
    def start_enrollment(self, name: str = ...) -> str: ...

    def cancel_enrollment(self) -> bool: ...
