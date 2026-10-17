"""Control panel actions: The bell and the conversation history."""

import asyncio

from jarvis.panel.base import GIT_TIMEOUT, HIDDEN, LOG_LINES, SERVICE_NAMES, _id, _text, action, log  # noqa: F401
from jarvis.state import activity, history

# ── Bell ───────────────────────────────────────────────────────


@action
async def activity_(_: dict) -> list[dict]:
    """Recent activity, newest first."""
    return await asyncio.to_thread(activity.recent)


# ── Conversation history (read-only) ────────────────────────────


@action
async def conversations(body: dict) -> dict:
    """Days with conversations, newest first, paged by {"before": "YYYY-MM-DD"}."""
    return await asyncio.to_thread(history.days, _text(body, "before", 10))


@action
async def conversation(body: dict) -> dict:
    """One day's exchanges, newest first: {"date": "YYYY-MM-DD"}."""
    return await asyncio.to_thread(history.day, _text(body, "date", 10))


@action
async def conversation_search(body: dict) -> dict:
    """Exchanges containing every word of {"text"}, newest first."""
    return await asyncio.to_thread(history.search, _text(body, "text", 200))
