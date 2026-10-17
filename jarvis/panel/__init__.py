"""Control panel actions: POST /api/panel/ACTION (jarvis.server checks the passcode first).

Each action takes the JSON body and returns what the page shows. Tasks, approvals and the machines' live state go
through the task runner (tasks.client); everything else (schedules, to-dos, projects, memory, vocabulary, skills,
accounts, devices, voiceprints, history, pairing codes) is changed directly. Machines are paired only here, never by
voice, and only by the owner's login: a code made here, run on the machine, and its fingerprint confirmed here. Keys for connections and JARVIS
itself are entered only here, write-only, so they're never spoken or typed to the brain.

Deliberately not possible here, since anyone with a login can reach the page: showing secrets, adding MCP servers or
changing their command, tool access or settings other than the server they reach (and moving that to another host
forgets their keys, so a login can't send them elsewhere), changing a project's folder. Those need voice with a yes, or the computer's
command line. Google accounts are added and signed in here (jarvis.integrations.google_signin): a sign-in needs the
Google account's own password and consent, so a login alone gains nothing.
"""

from jarvis.panel import (  # noqa: F401  (each registers its actions)
    access,
    accounts,
    activity,
    devices,
    machines,
    memory,
    preferences,
    projects,
    schedules,
    skills,
    system,
    tasks,
    todos,
)
from jarvis.panel.base import ACTIONS


async def call(name: str, body: dict) -> object:
    """Run action `name`. Raises KeyError if unknown; ValueError or runner client errors with a user-facing message."""
    return await ACTIONS[name](body)
