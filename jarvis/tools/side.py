"""background_lookup and file_lookup: hand slow reading to a side job (jarvis.brain.side)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from jarvis.state import conversation
from jarvis.tools import tool

if TYPE_CHECKING:
    from jarvis.brain.side import Jobs

def _jobs() -> Jobs:
    if (lookups := conversation.current().lookups) is None:
        raise RuntimeError("Background lookups work only in a voice conversation; do it here instead.")
    return lookups


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
    stays=True,
    screened=False,
)
async def background_lookup(brief: str, about: str) -> str:
    return _jobs().start(brief, about)


@tool(
    "file_lookup",
    "Find something in files on the computer, since you can't search files yourself: what a config says, where "
    "something is defined, a project's version, a line in a log, note or conversation log. A fast helper searches "
    "and answers in a sentence or two, with the file. Give it the folder to look in (projects_list has each "
    "project's folder); with none it searches JARVIS's own folder, and it knows where JARVIS keeps its data. A quick "
    "find comes back in this turn; one that takes longer than a few seconds carries on in the background and its "
    "answer is announced when ready. It only reads; file_read reads one document or picture you can name, and how "
    "a feature works across files or what's wrong with some code is a code_question task.",
    {
        "brief": {"type": "string", "description": "What to find and in which folder; it doesn't see this "
                                                   "conversation."},
        "about": {"type": "string", "description": "What it's about, in a few words, as you'd say it: \"the "
                                                   "deploy port\", \"the wake word setting\"."},
    },
    ["brief", "about"],
    reads=True,
    stays=True,
    screened=False,
)
async def file_lookup(brief: str, about: str) -> str:
    return await _jobs().ask(brief, about)
