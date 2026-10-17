"""What a voice call carries that others reach through the conversation (jarvis.state.conversation): its lookups,
its tool calls carrying on in the background, and its speaker check. The voice process fills them in with the real
ones: jarvis.brain.side.Jobs, jarvis.brain.kept.Kept and jarvis.voice.speaker.SpeakerCheck."""

import asyncio
from collections.abc import Awaitable
from typing import Any, Protocol


class Lookups(Protocol):
    running: dict[asyncio.Task, str]  # job -> what it looks up

    def new_turn(self) -> None: ...

    def start(self, brief: str, about: str) -> str: ...

    async def ask(self, brief: str, about: str, lookup: str = ...) -> str: ...

    async def stop(self, job: asyncio.Task) -> None: ...


class BackgroundCall(Protocol):
    tool: str  # the JARVIS tool called
    args: dict


class BackgroundCalls(Protocol):
    def going(self) -> list[Any]: ...  # BackgroundCall each

    async def stop(self, calls: list[Any], first: Awaitable[Any] | None = None) -> Any: ...


class SpeakerCheck(Protocol):
    def start_enrollment(self, name: str = ...) -> str: ...

    def cancel_enrollment(self) -> bool: ...
