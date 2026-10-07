"""Access a background task can be granted beyond what its kind has by default (jarvis.tasks.worker).

  artifacts  publish pages to claude.ai with Claude Code's Artifact tool (code_change and research have it, unless
             JARVIS_TASK_ARTIFACTS is off, which turns it off for every task)
  shell      shell commands in the sandbox, reaching any host, writing only the task's own folders (code_change has it)
  mac        commands on the Mac itself, outside the sandbox: host_run and jobs, risky ones still after the user's
             yes (code_change has it)
  projects   edit files in any of the user's projects (TASK_READ_DIRS), never secrets or JARVIS's own files
  accounts   the user's mail, calendar, Chat, Sheets, Docs, GitHub and GitLab, read-only (research has it)

JARVIS grants them when it starts a task (task_start) or replies to one (task_reply), when the user's request needs
them. A task that finds it lacks one asks for it with request_access and waits, as for a question; once granted it
carries on in the same session, with the added tools. Whatever is granted, secrets and JARVIS's own data files stay
out of reach, and risky commands on the Mac still wait for the user's yes.
"""

import json

from jarvis.core.config import TASK_ARTIFACTS

# Each as it ends "may it ...?", which JARVIS asks the user
GRANTS = {
    "artifacts": "publish pages to claude.ai with the Artifact tool",
    "shell": "run sandboxed shell commands that reach any host",
    "mac": "run commands on the Mac itself outside the sandbox",
    "projects": "edit files in other projects",
    "accounts": "read mail, calendar, Chat, Sheets, Docs, GitHub and GitLab",
}
_DEFAULTS = {"code_change": {"artifacts", "shell", "mac"}, "code_question": set(), "research": {"artifacts", "accounts"}}


def _off() -> set[str]:
    """What no task gets, whatever it's granted: artifacts, when JARVIS_TASK_ARTIFACTS is off."""
    return set() if TASK_ARTIFACTS else {"artifacts"}


def defaults(kind: str) -> set[str]:
    """What a task of this kind has without being granted anything."""
    return _DEFAULTS.get(kind, set()) - _off()


def check(names) -> list[str]:
    """The grants asked for, in GRANTS' order; raises ValueError for one that isn't known, or is switched off."""
    names = list(names or [])
    if unknown := [n for n in names if n not in GRANTS]:
        raise ValueError(f"Unknown access {', '.join(map(str, unknown))}: it's one of {', '.join(GRANTS)}.")
    if _off() & set(names):
        raise ValueError("Publishing artifacts is switched off for tasks (JARVIS_TASK_ARTIFACTS).")
    return [n for n in GRANTS if n in names]


def granted(task: dict) -> list[str]:
    """What the task was granted beyond its kind's defaults."""
    return json.loads(task.get("grants") or "[]")


def wanted(task: dict) -> list[str]:
    """What the task asked for with request_access, still unanswered."""
    return json.loads(task.get("wants") or "[]")


def has(task: dict) -> set[str]:
    """Everything the task can do: its kind's defaults and what it was granted."""
    return (defaults(task["kind"]) | set(granted(task))) - _off()


def missing(task: dict) -> list[str]:
    """What it could still be granted."""
    out = has(task) | _off()
    return [n for n in GRANTS if n not in out]


def new(task: dict, names) -> list[str]:
    """Of `names` (checked), those the task doesn't have yet."""
    have = has(task)
    return [n for n in check(names) if n not in have]


def add(task: dict, names) -> str:
    """The task's grants with `names` added, as kept in its row."""
    return json.dumps(check([*granted(task), *names]))


def describe(names) -> str:
    """'publish pages to claude.ai with the Artifact tool and run commands on the Mac itself, ...'"""
    said = [GRANTS[n] for n in names]
    return " and ".join(said) if len(said) < 3 else ", ".join(said[:-1]) + " and " + said[-1]


def listing(names) -> str:
    """'artifacts (publish pages to claude.ai ...), mac (run commands on the Mac ...)'"""
    return ", ".join(f"{n} ({GRANTS[n]})" for n in names)
