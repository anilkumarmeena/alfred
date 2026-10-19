"""A few words said the moment a voice turn's first tool call starts, when the model said nothing before it.

The persona asks for a lead-in before working, but on the MacBook Air 49 of 61 tool turns had none and sat silent for
13 s (median) until the answer. Picked by what the tool does, never the same twice running, and short: the model's own
answer follows once the tool is done.
"""

import random

PHRASES = {
    "tasks": ("Let me look.", "Checking on that.", "Let me see where that's got to."),
    "start": ("On it.", "Setting that up.", "Getting that going."),
    "look": ("Let me check.", "Looking now.", "One moment."),
    "other": ("One moment.", "Let me see.", "Just a second."),
}
_TASKS = ("task_list", "task_detail", "task_log", "background_status")
_START = ("task_start", "task_reply", "task_merge", "start_job", "schedule_add", "schedule_change", "todo_add",
          "project_register")
_LOOK = ("lookup", "search", "read", "list", "calendar", "gmail", "drive", "sheets", "docs", "slides", "chat",
         "Bash", "ToolSearch", "WebFetch", "WebSearch")


def kind(tool: str) -> str:
    """The kind of work `tool` (an SDK tool name, e.g. mcp__alfred__task_list) does, for its phrase."""
    name = tool.rsplit("__", 1)[-1]
    if name in _TASKS:
        return "tasks"
    if name in _START:
        return "start"
    if any(word in name for word in _LOOK):
        return "look"
    return "other"


def pick(tool: str, last: str | None = None, rng: random.Random | None = None) -> str:
    """A phrase for `tool`'s kind, not `last` (the one said before)."""
    choices = [p for p in PHRASES[kind(tool)] if p != last]
    return (rng or random).choice(choices)
