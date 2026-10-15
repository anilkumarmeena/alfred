"""How much work one spoken turn may do itself, so the user is never left waiting to talk.

A PreToolUse hook on the voice brain (jarvis.brain.cli). Past TURN_TOOLS tool calls or TURN_SECONDS in a turn, further
calls are refused with a note telling the brain to hand the rest off: to a lookup helper (jarvis.brain.side), which
answers while the talk goes on, or a background task. A shell line chaining several programs is refused at once, since
that's a check for computer_lookup. Hand-offs themselves always run.
"""

import time
from collections.abc import Callable

from claude_agent_sdk import HookMatcher

from jarvis.brain.sdk import refusing
from jarvis.core.config import TURN_SECONDS, TURN_TOOLS
from jarvis.safety.commands import programs

# Calls that hand work off rather than do it, stop background work the user asked to stop, and loading deferred tools
# or a skill's instructions
HAND_OFFS = {"mcp__jarvis__background_lookup", "mcp__jarvis__file_lookup", "mcp__jarvis__computer_lookup",
             "mcp__jarvis__task_start", "mcp__shell__start_job", "mcp__jarvis__background_stop",
             "mcp__jarvis__task_stop", "mcp__shell__stop_job", "ToolSearch", "Skill"}
CHAIN = 2  # most programs one shell line may run in a turn
# Filters and plumbing around a command's output, not work of their own
_PLUMBING = {"cd", "pushd", "popd", "echo", "printf", "true", "false", "cat", "head", "tail", "grep", "egrep", "fgrep",
             "sort", "uniq", "wc", "cut", "tr", "awk", "sed", "column", "tee", "jq"}
HAND_OFF = ("Hand what's left to computer_lookup (anything about this computer), file_lookup, background_lookup or "
            "task_start now, say in a few words that it's under way, and answer with what you already have.")


def chained(command: str) -> list[str]:
    """The programs a shell line runs that do work of their own, when there are more than CHAIN of them; else []."""
    working = [p for p in programs(command) if p not in _PLUMBING]
    return working if len(working) > CHAIN else []


class Budget:
    """One conversation's per-turn budget. `new_turn()` starts each turn, the user's or a follow-up."""

    def __init__(self, tools: int = TURN_TOOLS, seconds: float = TURN_SECONDS,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.tools, self.seconds, self.clock = tools, seconds, clock
        self.began = clock()
        self.used = 0

    def new_turn(self) -> None:
        self.began, self.used = self.clock(), 0

    def refused(self, name: str, args: dict) -> str:
        """Why the call `name` mustn't run in this turn, or ''. A call let through counts against the turn."""
        if name in HAND_OFFS:
            return ""
        if name == "Bash" and (working := chained(str(args.get("command") or ""))):
            return (f"Not run: that line runs {len(working)} programs, a check of several steps that would keep the "
                    f"conversation waiting. Hand it to computer_lookup instead, say in a few words that you're "
                    f"checking, and carry on.")
        if self.used >= self.tools:
            return f"Not run: this reply has already made {self.used} calls of its own. {HAND_OFF}"
        if self.clock() - self.began >= self.seconds:
            return f"Not run: this reply has already taken {self.seconds:g} seconds. {HAND_OFF}"
        self.used += 1
        return ""

    @property
    def hook(self) -> list[HookMatcher]:
        return refusing(self.refused)
