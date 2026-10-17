"""Control panel actions: Other machines (jarvis.node): pairing, confirming, where jobs run."""

import asyncio

from jarvis.auth import logins
from jarvis.node import keys, machines
from jarvis.panel.base import GIT_TIMEOUT, HIDDEN, LOG_LINES, SERVICE_NAMES, _id, _text, action, log  # noqa: F401
from jarvis.panel.projects import _aliases
from jarvis.state import activity
from jarvis.tasks import client

# ── Machines (jarvis.node): pairing, confirming, where jobs run ─
# Pairing itself is finished on the machine (make node-pair), with a code made here. Only the owner's login pairs,
# confirms or forgets a machine, since a machine runs the owner's work.

PAIRING = "pair, confirm or forget machines"


async def _machines(body: dict) -> dict:
    """The machines as the page shows them, and whether this login may pair them."""
    return await client.machines() | {"owner": await asyncio.to_thread(logins.owner),
                                      "can_pair": await asyncio.to_thread(logins.is_owner, str(body.get("login", "")))}


def _owner_only(body: dict) -> None:
    logins.owner_only(str(body.get("login", "")), PAIRING)


@action
async def machines_(body: dict) -> dict:
    """Paired machines, each checked online just now, and where each kind of job runs."""
    return await _machines(body)


@action
async def machine_code(body: dict) -> dict:
    """A one-time pairing code, with this computer's fingerprint for the machine's screen to match."""
    await asyncio.to_thread(_owner_only, body)
    made = await asyncio.to_thread(machines.new_code)
    return made | {"main_fingerprint": keys.fingerprint(await asyncio.to_thread(machines.main_public))}


@action
async def machine_confirm(body: dict) -> dict:
    """The user checked a pending machine's fingerprint: it may connect."""
    await asyncio.to_thread(_owner_only, body)
    found = await asyncio.to_thread(machines.confirm, _id(body))
    await asyncio.to_thread(activity.add, f"Paired {found.name} in the control panel.", "ok")
    return await _machines(body)


@action
async def machine_save(body: dict) -> dict:
    """Rename a machine: {"id", "name", "aliases"}."""
    await asyncio.to_thread(machines.rename, _id(body), _text(body, "name", machines.NAME_MOST), _aliases(body))
    return await _machines(body)


@action
async def machine_remove(body: dict) -> dict:
    """Forget a machine (the page confirms first); it must pair again to come back."""
    await asyncio.to_thread(_owner_only, body)
    gone = await client.machine_remove(_id(body))
    await asyncio.to_thread(activity.add, f"Removed {gone['removed']} in the control panel.", "warn")
    return await _machines(body)


@action
async def placement_save(body: dict) -> dict:
    """Where a kind of job runs: {"kind": "commands" | "tasks", "value": "here" | "auto" | machine id}."""
    await asyncio.to_thread(machines.set_placement, _text(body, "kind", 20), _text(body, "value", 20))
    return await _machines(body)
