"""A voice conversation's own state, kept on one object rather than in module globals: the profile it runs as, the
user's own words this turn (`heard`), a rule offered for saving (`offer`), the speaker check the voice_enroll tool
reaches, and the background lookups background_lookup hands work to.

The voice agent begins one per conversation (`begin`), and the code that conversation runs (its brain, tools and gates)
finds it with current(). A voice process holds one conversation at a time, so the one begun is also the process's,
for code that runs outside its context (a callback LiveKit makes from elsewhere). Outside any conversation (the task
runner, tests), current() is one of the process's own.
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from jarvis.core import profile
from jarvis.safety.approvals import Heard, RuleOffer

if TYPE_CHECKING:
    from jarvis.brain.side import Jobs
    from jarvis.voice.speaker import SpeakerCheck


@dataclass(eq=False)
class Conversation:
    profile: profile.Profile | None = None  # who it's with; None: whoever the process runs as
    heard: Heard = field(default_factory=Heard)
    offer: RuleOffer = field(default_factory=RuleOffer)
    speaker: SpeakerCheck | None = None
    lookups: Jobs | None = None


_current: ContextVar[Conversation | None] = ContextVar("jarvis_conversation", default=None)
_process = Conversation()


def current() -> Conversation:
    return _current.get() or _process


def begin(who: profile.Profile | None = None) -> Conversation:
    """A new conversation, with `who` (the profile the process runs as by default), current from here on in this
    context and the tasks it starts, and the process's. Its profile is the one its code runs as too."""
    global _process
    _process = Conversation(who or profile.current())
    _current.set(_process)
    profile.use(_process.profile)
    return _process


def end(ended: Conversation) -> None:
    """The conversation is over: the process's goes back to one of its own."""
    global _process
    if _process is ended:
        _process = Conversation()
