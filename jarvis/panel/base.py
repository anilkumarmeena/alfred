"""The control panel's action registry and the helpers every action uses."""

import logging
from collections.abc import Awaitable, Callable

log = logging.getLogger("jarvis.panel")

LOG_LINES = 40  # latest task log lines (runner's maximum)
SERVICE_NAMES = {"livekit": "LiveKit", "agent": "Voice agent", "web": "Web", "tasks": "Task runner"}
GIT_TIMEOUT = 10
HIDDEN = ("instruction", "summary", "nudges", "edits", "session_id", "log")  # omitted from task lists

Action = Callable[[dict], Awaitable[object]]
ACTIONS: dict[str, Action] = {}


def action(fn: Action) -> Action:
    """Register an action under the function's name (trailing _ stripped)."""
    ACTIONS[fn.__name__.rstrip("_")] = fn
    return fn


def _id(body: dict, key: str = "id") -> int:
    try:
        return int(body[key])
    except (KeyError, TypeError, ValueError):
        raise ValueError(f"Say which one: {key} is missing.") from None


def _text(body: dict, key: str, most: int = 4000) -> str:
    value = body.get(key, "")
    if not isinstance(value, str):
        raise ValueError(f"{key} must be text.")
    return value.strip()[:most]


