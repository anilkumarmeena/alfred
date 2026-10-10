"""background_lookup: slow reading handed to a side job, so the conversation carries on (jarvis.brain.side)."""

from jarvis.state import conversation
from jarvis.tools import tool


@tool(
    "background_lookup",
    "Look something up in the background while the conversation carries on, and have the answer announced when it's "
    "ready: several web searches, a long page or document to read, a comparison, a dig through mail. Use it instead "
    "of doing that work in this turn whenever it will plainly take more than a few seconds, so the user isn't left "
    "waiting; one quick search or check is still fine in the turn. It only reads; anything that changes something, "
    "or several steps of real work, is a task (task_start). The job doesn't see this conversation, so the brief must "
    "say everything it needs: what to find, for whom, and any names, dates or places. After calling it, say in a few "
    "words that you're on it and carry on.",
    {
        "brief": {"type": "string", "description": "Everything the job needs to know: what to find and why."},
        "about": {"type": "string", "description": "What it's about, in a few words, as you'd say it: \"flights to "
                                                   "Lisbon\", \"the Kokoro release notes\"."},
    },
    ["brief", "about"],
)
async def background_lookup(brief: str, about: str) -> str:
    if (lookups := conversation.current().lookups) is None:
        raise RuntimeError("Background lookups work only in a voice conversation; do it here instead.")
    return lookups.start(brief, about)
