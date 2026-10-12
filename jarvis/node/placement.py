"""Where a job runs: this computer or one of the user's machines, by their setting, their word, and an online check.

  asked    where the user said this one should run ("here", or a machine's name), read by the brain from their words
  setting  Settings › Machines › Where jobs run, per kind: this computer, a machine, or let JARVIS choose
  choice   with "let JARVIS choose", PLACEMENT_MODEL weighs the machines that are online and able to take it

A machine is able to take a job when it answered a ping just now, runs the same link protocol, has a free slot (for
a Claude task), and its Claude account isn't used up (for a Claude task). Otherwise the job runs here and JARVIS says
why; when it's this computer's account that's used up, a Claude task goes to a machine that can take it instead.
"""

import json
import logging
import os
from dataclasses import dataclass

import psutil

from jarvis import prompts
from jarvis.brain import usage
from jarvis.core import profile
from jarvis.core.config import PLACEMENT_MODEL
from jarvis.node import machines

log = logging.getLogger("jarvis.node.placement")

COMMANDS, TASKS = "commands", "tasks"
CHOICE_SECONDS = 20.0


@dataclass(frozen=True)
class Place:
    node: int | None = None
    name: str = "this computer"
    said: str = ""  # why it isn't where the user would expect, said with the start; "" when there's nothing to add


HERE = Place()


def problem(check: dict | None, kind: str) -> str:
    """Why a machine can't take this kind of job now ("isn't online"), or "" if it can."""
    if not check:
        return "isn't online"
    if not check.get("same_protocol"):
        return "is updating to this computer's code"
    if kind == TASKS:
        if (check.get("free") or 0) < 1:
            return "is busy with as many tasks as it runs at once"
        if usage.out(kept=check.get("usage") or {}):
            return "has used up its Claude account's limit"
    return ""


def _there(found: machines.Machine, said: str = "") -> Place:
    return Place(found.id, found.name, said)


def _fallback(found: machines.Machine, why: str) -> Place:
    return Place(said=f"{found.name} {why}, so it's running on this computer instead.")


async def choose(hub, kind: str, job: dict, asked: str = "", running_here: int = 0, slots_here: int = 1) -> Place:
    """Where `job` (a description for the choice) of `kind` runs. ValueError if `asked` names no machine."""
    if asked.strip().lower() == machines.HERE:
        return HERE
    if asked.strip():
        found = machines.find(asked)
        return _there(found) if not (why := problem(await hub.check(found.id), kind)) else _fallback(found, why)
    setting = machines.placement()[kind]
    if setting == machines.HERE:
        if kind == TASKS and usage.out():
            return await _cover(hub, kind)
        return HERE
    if setting != machines.AUTO:
        found = machines.need(int(setting))
        if not (why := problem(await hub.check(found.id), kind)):
            return _there(found)
        return await _cover(hub, kind) if kind == TASKS and usage.out() else _fallback(found, why)
    checks = await hub.check_all()
    able = {m.id: (m, checks[m.id]) for m in machines.paired() if not problem(checks.get(m.id), kind)}
    if not able:
        return HERE
    if kind == TASKS and usage.out():
        m, _ = next(iter(able.values()))
        return _there(m, "This computer's Claude account has hit its limit, so it's running there.")
    return await _pick(kind, job, able, running_here, slots_here)


async def _cover(hub, kind: str) -> Place:
    """This computer's Claude account is used up: a machine that can take the task, else here."""
    checks = await hub.check_all()
    for m in machines.paired():
        if not problem(checks.get(m.id), kind):
            return _there(m, f"This computer's Claude account has hit its limit, so it's running on {m.name}.")
    return HERE


def _here_now(running: int, slots: int) -> dict:
    try:
        load = round(os.getloadavg()[0], 2)
    except OSError:
        load = None
    return {"cores": os.cpu_count(), "ram_gb": round(psutil.virtual_memory().total / 1e9, 1), "load": load,
            "claude_tasks_running": running, "free_slots": max(0, slots - running), "claude_usage": usage.said()}


async def _pick(kind: str, job: dict, able: dict, running: int, slots: int) -> Place:
    """Let the model choose among this computer and the machines able to take it; here if the call fails."""
    from jarvis.brain import sdk

    there = {str(i): {"name": m.name, "cores": m.cores, "ram_gb": m.ram_gb, "tools": m.tools,
                      "load": check.get("load"), "free_slots": check.get("free"),
                      "claude_usage": usage.said(kept=check.get("usage") or {})} for i, (m, check) in able.items()}
    material = {"job": job, "this_computer": _here_now(running, slots), "machines": there}
    schema = {"type": "object", "properties": {"where": {"enum": ["here", *there]}, "why": {"type": "string"}},
              "required": ["where"]}
    try:
        found, _ = await sdk.ask_json(json.dumps(material, ensure_ascii=False), schema,
                                      prompts.load("placement", user_name=profile.name()), PLACEMENT_MODEL,
                                      timeout=CHOICE_SECONDS)
    except Exception as e:
        log.warning("choosing where %s runs failed (%s); it runs here", job.get("title"), type(e).__name__)
        return HERE
    if not found or found["where"] == "here":
        return HERE
    m, _ = able[int(found["where"])]
    return _there(m)
