"""The voice brain: Claude with Alfred's tools and memory, streaming its spoken reply (cli.py), plus its background lookups, turn budget and persona."""

from collections.abc import AsyncIterator, Callable
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from alfred.brain.kept import Kept


class _SpeakNow(str):
    """The type of SPEAK_NOW."""


# A chunk of a reply marking where a tool call starts: what was said before it is spoken now, not held until more
# words follow (the voice ends its speech there). It is an empty string, so joining a reply's chunks drops it.
SPEAK_NOW = _SpeakNow()


class VoiceBrain(Protocol):
    """What a voice call (alfred.voice.session) needs of its brain."""

    next_model: str | None  # a lighter model for the next turn alone (alfred.brain.route)
    kept: "Kept"  # its tool calls carrying on outside their turn

    async def start(self) -> None: ...

    async def end(self) -> None:
        """The conversation is over."""

    def respond(self, text: str) -> AsyncIterator[str]:
        """Stream the spoken reply to one user utterance, with SPEAK_NOW where a tool call starts."""

    def on_background(self, finished: Callable[[str, str, str], object]) -> None:
        """Take results of slow tool calls that left their turn: `finished(summary, prompt, words)`, where `prompt`
        is for follow_up (alfred.brain.kept)."""

    def follow_up(self, prompt: str, words: str = "") -> AsyncIterator[str]:
        """Stream a turn the brain starts itself, on a kept call's result; `words` are the user's of the turn that
        made the call."""

    def note(self, text: str, unsaid: str = "") -> None:
        """`text` was said outside a turn; `unsaid` is for the brain alone."""

    def frame(self, lines: list[str]) -> None:
        """Lines about how the next turn was heard, for that turn only."""

    def question_open(self) -> bool:
        """Whether a risky-command question is open; a task's request must not be put to the user over it."""


def make_brain() -> VoiceBrain:
    from alfred.brain.cli import CliBrain

    return CliBrain()
