"""Extra access a background task can be granted beyond its kind's defaults.

  artifacts  publish claude.ai pages via the Artifact tool (default: code_change, research; JARVIS_TASK_ARTIFACTS=off
             disables it everywhere)
  shell      sandboxed shell, any host, writes only to the task's folders (default: code_change)
  mac        host_run and jobs outside the sandbox; risky ones still need the user's yes (default: code_change)
  projects   edit files in any of the user's projects (TASK_READ_DIRS), never secrets or JARVIS's files
  accounts   read-only mail, calendar, Chat, Sheets, Docs and connected tools (default: research)

Granted via task_start / task_reply, or requested mid-run with request_access (the task waits, then resumes in the same
session). Secrets and JARVIS's own data stay out of reach whatever is granted.
"""

import json

from jarvis.core.config import TASK_ARTIFACTS

# Each completes the question "may it ...?" put to the user
GRANTS = {
    "artifacts": "publish pages to claude.ai with the Artifact tool",
    "shell": "run sandboxed shell commands that reach any host",
    "mac": "run commands on the computer itself outside the sandbox",
    "projects": "edit files in other projects",
    "accounts": "read mail, calendar, Chat, Sheets, Docs and the tools the user connected",
}
_DEFAULTS = {"code_change": {"artifacts", "shell", "mac"}, "code_question": set(), "research": {"artifacts", "accounts"}}
# What only this computer has (account keys, the user's other projects): a task with these never runs on a machine
LOCAL = {"accounts", "projects"}


def _off() -> set[str]:
    """Grants switched off for every task: artifacts when JARVIS_TASK_ARTIFACTS is off."""
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
        raise ValueError("Publishing artifacts is switched off for tasks (JARVIS_TASK_ARTIFACTS).")
    return [n for n in GRANTS if n in names]


def granted(task: dict) -> list[str]:
    """Grants beyond the kind's defaults."""
    return json.loads(task.get("grants") or "[]")


def wanted(task: dict) -> list[str]:
    """Unanswered request_access grants."""
    return json.loads(task.get("wants") or "[]")


def has(task: dict) -> set[str]:
    """Defaults plus grants, minus anything switched off."""
    return (defaults(task["kind"]) | set(granted(task))) - _off()


def missing(task: dict) -> list[str]:
    """Grants the task could still be given."""
    out = has(task) | _off()
    return [n for n in GRANTS if n not in out]


def new(task: dict, names) -> list[str]:
    """The checked `names` the task doesn't have yet."""
    have = has(task)
    return [n for n in check(names) if n not in have]


def add(task: dict, names) -> str:
    """Grants with `names` added, as the JSON stored on the row."""
    return json.dumps(check([*granted(task), *names]))


def describe(names) -> str:
    """Grants as prose: 'publish pages ... and run commands on the computer ...'."""
    said = [GRANTS[n] for n in names]
    return " and ".join(said) if len(said) < 3 else ", ".join(said[:-1]) + " and " + said[-1]


def listing(names) -> str:
    """Grants as 'name (description)' pairs."""
    return ", ".join(f"{n} ({GRANTS[n]})" for n in names)
