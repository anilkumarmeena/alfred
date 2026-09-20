"""The persona every brain shares: who JARVIS is and how it speaks and acts, plus the per-turn framing.

The persona's text is markdown (jarvis/prompts/persona.md); this fills in the user's name and what it says about them.
How to use each tool is in that tool's description, not in the persona.

Independent of voice, so the same brain can later serve text channels too.
"""

from datetime import datetime

from jarvis import prompts
from jarvis.core import profile
from jarvis.integrations import accounts
from jarvis.state import conversation, memory, settings
from jarvis.tools import google


def _about() -> str:
    """The persona's lines about the user: their "about you" setting (jarvis.state.settings), then their Google accounts."""
    lines = [l if l.startswith("- ") else f"- {l}" for l in settings.about().splitlines()]
    return "\n".join([*lines, f"- {accounts.describe(google.ACCOUNTS)}"])


def persona() -> str:
    """The persona, for the person JARVIS works for now."""
    return prompts.load("persona", user_name=profile.name(), about=_about())


class Notes:
    """Things said outside a turn (task announcements), which the next turn is told about."""

    def __init__(self) -> None:
        self.notes: list[str] = []
        self.framing: list[str] = []

    def note(self, text: str, unsaid: str = "") -> None:
        """`text` was said; `unsaid` is for the brain alone (which task it was, say), and isn't logged as said."""
        self.notes.append(f"{text} {unsaid}" if unsaid else text)
        memory.log_announcement(text)

    def frame(self, lines: list[str]) -> None:
        """Bracketed lines about how the next turn was heard (jarvis.voice.turn.Pacing.hints), for that turn only."""
        self.framing = list(lines)

    def turn(self, text: str) -> str:
        """The user's words as the brain sees them: stamped with the time, after anything announced since, before
        anything said about how they were heard. They're kept apart too, as the only words task_approve can act on
        (the conversation's `heard`: jarvis.state.conversation)."""
        conversation.current().heard.turn(text)
        head = f"[{datetime.now(profile.tz()):%A %d %B %Y, %I:%M %p}]"
        if self.notes:
            head += "\n[You announced since the last turn: " + " ".join(self.notes) + "]"
            self.notes.clear()
        framing, self.framing = self.framing, []
        return "\n".join([head, text, *framing])


def system_prompt(extra: str = "") -> str:
    """The persona, plus `extra` (the brain's own additions), long-term memory and recent exchanges."""
    parts = [persona() + extra]
    parts.append(memory.prompt_block())
    if recent := memory.read_recent().strip():
        parts.append(f"<recent_conversation>\nThe latest exchanges, possibly from an earlier session "
                     f"(conversation_search finds older ones):\n{recent}\n</recent_conversation>")
    return "\n\n".join(parts)
