"""Shared persona (jarvis/prompts/persona.md, filled with the user's name and details) and per-turn framing.

Tool usage lives in each tool's description, not here. Voice-independent.
"""

from datetime import datetime

from jarvis import prompts
from jarvis.core import profile
from jarvis.integrations import accounts
from jarvis.state import conversation, memory, settings
from jarvis.tools import google


def _about() -> str:
    """Persona lines about the user: the "about you" setting, then their Google accounts."""
    lines = [l if l.startswith("- ") else f"- {l}" for l in settings.about().splitlines()]
    return "\n".join([*lines, f"- {accounts.describe(google.ACCOUNTS)}"])


def persona() -> str:
    """The persona for the current user."""
    return prompts.load("persona", user_name=profile.name(), about=_about())


def private(text: str) -> str:
    """`text` marked as the brain's alone: the persona says never to say it, and jarvis.voice.unspoken drops it."""
    return f"<private>{text}</private>"


class Notes:
    """Things said outside a turn (task announcements), reported to the next turn, and private notes about them."""

    def __init__(self) -> None:
        self.notes: list[str] = []
        self.unsaid: list[str] = []
        self.framing: list[str] = []

    def note(self, text: str, unsaid: str = "") -> None:
        """Record `text` as said (if any); `unsaid` (e.g. which task) goes to the brain only and isn't logged."""
        if text:
            self.notes.append(text)
            memory.log_announcement(text)
        if unsaid:
            self.unsaid.append(unsaid)

    def frame(self, lines: list[str]) -> None:
        """Bracketed lines about how the next turn was heard, for that turn only."""
        self.framing = list(lines)

    def turn(self, text: str) -> str:
        """The user's words as the brain sees them: timestamp, announcements since, words, framing.

        Also recorded in the conversation's `heard`, the only words task_approve may act on.
        """
        conversation.current().heard.turn(text)
        framing, self.framing = self.framing, []
        return "\n".join([self.head(), text, *framing])

    def head(self) -> str:
        """Timestamp, announcements since the last turn and private notes on them, which start every message to the
        brain."""
        head = f"[{datetime.now(profile.tz()):%A %d %B %Y, %I:%M %p}]"
        if self.notes:
            head += "\n[You announced since the last turn: " + " ".join(self.notes) + "]"
        if self.unsaid:
            head += "\n" + private(" ".join(self.unsaid))
        self.notes, self.unsaid = [], []
        return head


def system_prompt(extra: str = "") -> str:
    """Persona + `extra` + long-term memory + recent exchanges."""
    parts = [persona() + extra]
    parts.append(memory.prompt_block())
    if recent := memory.read_recent().strip():
        parts.append(f"<recent_conversation>\nThe latest exchanges, possibly from an earlier session "
                     f"(conversation_search finds older ones):\n{recent}\n</recent_conversation>")
    return "\n\n".join(parts)
