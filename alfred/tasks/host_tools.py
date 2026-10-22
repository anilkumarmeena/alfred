"""Helpers task sessions share: a Reporter's answers, the approval request for a command, a task's temp folder, and
short lists and tool results.
"""

import inspect
from collections.abc import Awaitable
from pathlib import Path
from typing import TypeVar

from alfred.config import TASK_CACHE_DIR
from alfred.safety import commands, remembered


def task_temp(task_id: int) -> Path:
    """Per-task temp folder in the cache dir (a GitLab connection's downloads, alfred.tasks.repo_files)."""
    return TASK_CACHE_DIR / "tmp" / f"task-{task_id}"


T = TypeVar("T")


async def answer(value: T | Awaitable[T]) -> T:
    """A Reporter's answer, whether given now (the runner) or awaited (over the link, on another machine)."""
    return await value if inspect.isawaitable(value) else value  # type: ignore[return-value]


def few(names: list[str]) -> str:
    """First ten names, plus a count of the rest."""
    return ", ".join(names[:10]) + (f" and {len(names) - 10} more" if len(names) > 10 else "")


def _resolve(path: str, cwd: Path) -> Path:
    p = Path(path).expanduser()
    return (p if p.is_absolute() else cwd / p).resolve()


def text(body: str, error: bool = False) -> dict:
    """An in-process tool's result: `body` as text, marked as an error if `error`."""
    return {"content": [{"type": "text", "text": body}]} | ({"is_error": True} if error else {})


def approval_item(tool: str, args: dict, cwd: Path, why: str, own: commands.Own | None = None) -> dict:
    """Approval request for a host command: exact command, tool, folder, risk reason, rememberable pieces and the
    fingerprints of the scripts it runs (a yes covers them as they are now)."""
    command, folder = args.get("command", "").strip(), _resolve(args.get("folder") or str(cwd), cwd)
    item = {"tool": tool, "command": command, "folder": str(folder), "why": why,
            "remember": remembered.pieces(command, own, folder)}
    return item | ({"scripts": files} if (files := commands.seen(command, folder).files) else {})
