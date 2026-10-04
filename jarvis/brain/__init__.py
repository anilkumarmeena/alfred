"""The brain: Claude with tools and memory, streaming its reply as text.

cli.py runs the local `claude` CLI on its own login, speaking as the persona in persona.py.
"""

from collections.abc import AsyncIterator
from typing import Protocol


class VoiceBrain(Protocol):
    """What a voice call (jarvis.voice.session) needs of its brain."""

    next_model: str | None  # a lighter model for the next turn alone (jarvis.brain.route)

    async def start(self) -> None: ...

    async def end(self) -> None:
        """The conversation is over."""

    def respond(self, text: str) -> AsyncIterator[str]:
        """Stream the spoken reply to one user utterance."""

    def note(self, text: str, unsaid: str = "") -> None:
        """`text` was said outside a turn; `unsaid` is for the brain alone."""

    def frame(self, lines: list[str]) -> None:
        """Lines about how the next turn was heard, for that turn only."""

    def question_open(self) -> bool:
        """Whether its shell has a question of its own about a risky command open, which a task's request mustn't be
        put to the user over."""


def make_brain() -> VoiceBrain:
    from jarvis.brain.cli import CliBrain

    return CliBrain()
