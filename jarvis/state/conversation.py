"""Per-conversation voice state: profile, this turn's words (`heard`), pending rule offer, speaker check, lookups and
the brain's tool calls carrying on in the background (`kept`).

begin() makes it current in this context and also process-wide, since a voice process holds one conversation and
LiveKit callbacks may run outside the context. Outside a conversation (task runner, tests) current() returns a
process default.
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass, field

from jarvis.contracts.call import BackgroundCalls, Lookups, SpeakerCheck
from jarvis.safety.approvals import Heard, RuleOffer
from jarvis.storage import profile


@dataclass(eq=False)
class Conversation:
    profile: profile.Profile | None = None  # None = the process's profile
    heard: Heard = field(default_factory=Heard)
    offer: RuleOffer = field(default_factory=RuleOffer)
    speaker: SpeakerCheck | None = None
    lookups: Lookups | None = None
    kept: BackgroundCalls | None = None


_current: ContextVar[Conversation | None] = ContextVar("jarvis_conversation", default=None)
_process = Conversation()


def current() -> Conversation:
    return _current.get() or _process


def begin(who: profile.Profile | None = None) -> Conversation:
    """Start a conversation with `who` (default: current profile), current for this context and process-wide.

    Also switches the context's profile to it."""
    global _process
    _process = Conversation(who or profile.current())
    _current.set(_process)
    profile.use(_process.profile)
    return _process


def end(ended: Conversation) -> None:
    """Reset the process default if `ended` is still it."""
    global _process
    if _process is ended:
        _process = Conversation()
