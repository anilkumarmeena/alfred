"""Extra access a background task can be granted beyond its kind's defaults. Everything else (the shell, files
anywhere, the computer itself) every task has: auto mode's check clears each action (alfred.tasks.worker).

  artifacts  publish claude.ai pages via the Artifact tool (default: code_change, research; ALFRED_TASK_ARTIFACTS=off
             disables it everywhere)
  accounts   read mail, calendar, Chat and connected tools; read and edit Sheets and Docs, never deleting; change
             things through connected tools, each change after the user's yes (default: research)

Granted via task_start / task_reply, or requested mid-run with request_access (the task waits, then resumes in the same
session).

A read-only task (one carrying on a lookup that ran out of time, alfred.brain.side) has no defaults, only what it was
started with, out of READ_ONLY and held to reads (the worker gives it accounts without edits), and it can never be
given more.
"""

import json

from alfred.config import TASK_ARTIFACTS

# Each completes the question "may it ...?" put to the user
GRANTS = {
    "artifacts": "publish pages to claude.ai with the Artifact tool",
    "accounts": "read mail, calendar, Chat, Sheets, Docs and the tools the user connected, edit Sheets and Docs, and "
                "change things through those tools, each change after the user's yes",
}
_DEFAULTS = {"code_change": {"artifacts"}, "code_question": set(), "research": {"artifacts", "accounts"}}
# What only this computer has (account keys): a task with these never runs on a machine
LOCAL = {"accounts"}
READ_ONLY = {"accounts"}  # what a read-only task may start with
FIXED = "it only reads, carrying on a lookup, so it can't be given more access: start a new task for what needs it"


def _off() -> set[str]:
    """Grants switched off for every task: artifacts when ALFRED_TASK_ARTIFACTS is off."""
    return set() if TASK_ARTIFACTS else {"artifacts"}


def defaults(kind: str) -> set[str]:
    """Access a task of this kind has by default."""
    return _DEFAULTS.get(kind, set()) - _off()


def check(names) -> list[str]:
    """`names` in GRANTS order; ValueError if any is unknown or switched off."""
    names = list(names or [])
    if unknown := [n for n in names if n not in GRANTS]:
        raise ValueError(f"Unknown access {', '.join(map(str, unknown))}: it's one of {', '.join(GRANTS)}.")
    if _off() & set(names):
        raise ValueError("Publishing artifacts is switched off for tasks (ALFRED_TASK_ARTIFACTS).")
    return [n for n in GRANTS if n in names]


def granted(task: dict) -> list[str]:
    """Grants beyond the kind's defaults; ones no longer given (shell, mac and projects, which every task now has) are
    left out."""
    return [n for n in json.loads(task.get("grants") or "[]") if n in GRANTS]


def wanted(task: dict) -> list[str]:
    """Unanswered request_access grants."""
    return [n for n in json.loads(task.get("wants") or "[]") if n in GRANTS]


def has(task: dict) -> set[str]:
    """Defaults (none for a read-only task) plus grants, minus anything switched off."""
    return ((set() if task.get("read_only") else defaults(task["kind"])) | set(granted(task))) - _off()


def missing(task: dict) -> list[str]:
    """Grants the task could still be given: none for a read-only one."""
    out = has(task) | _off()
    return [] if task.get("read_only") else [n for n in GRANTS if n not in out]


def new(task: dict, names) -> list[str]:
    """The checked `names` the task doesn't have yet; ValueError (FIXED) if a read-only task would gain any."""
    have = has(task)
    gained = [n for n in check(names) if n not in have]
    if gained and task.get("read_only"):
        raise ValueError(FIXED)
    return gained


def add(task: dict, names) -> str:
    """Grants with `names` added, as the JSON stored on the row."""
    return json.dumps(check([*granted(task), *names]))


def describe(names) -> str:
    """Grants as prose: 'publish pages ... and read mail ...'."""
    said = [GRANTS[n] for n in names]
    return " and ".join(said) if len(said) < 3 else ", ".join(said[:-1]) + " and " + said[-1]


def listing(names) -> str:
    """Grants as 'name (description)' pairs."""
    return ", ".join(f"{n} ({GRANTS[n]})" for n in names)
