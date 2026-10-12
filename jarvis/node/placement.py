"""Where a job runs: this computer or one of the user's machines, by their setting, their word, and an online check.

  asked    where the user said this one should run ("here", or a machine's name), read by the brain from their words
  setting  Settings › Machines › Where jobs run, per kind: this computer, a machine, or let JARVIS choose
  choice   with "let JARVIS choose", PLACEMENT_MODEL weighs this computer and every machine that is online and able
           to take it: free slots, load, programs, and how much of each Claude account's limit is left

A machine is able to take a job when it answered a ping just now, runs the same link protocol, has the programs a
command runs (asked in that ping), and, for a Claude task, has a free slot and a Claude account that isn't used up.
Otherwise the job runs here and JARVIS says why. When this computer can't take it (its Claude account is used up,
or it lacks a program the command runs), it goes to the able machine with the most room instead. If the model can't
be asked, the computer with the most room takes it, this one on a tie.
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
from jarvis.node import home, machines
from jarvis.safety import commands

log = logging.getLogger("jarvis.node.placement")

COMMANDS, TASKS = "commands", "tasks"
CHOICE_SECONDS = 20.0
CLOSE = 0.2  # share of a limit taken to be left when Claude warns it's getting close without saying how close


@dataclass(frozen=True)
class Place:
    node: int | None = None
    name: str = "this computer"
    said: str = ""  # why it isn't where the user would expect, said with the start; "" when there's nothing to add


HERE = Place()


def usage_left(kept: dict | None = None) -> float:
    """Share of the tightest Claude limit still left, 0 to 1: this computer's account, or `kept` (a machine's). Claude
    reports only as limits get close, so nothing reported counts as all of it."""
    if usage.out(kept=kept):
        return 0.0
    known = [w for k, w in usage.windows(kept=kept).items() if k not in usage.OWN]
    used = [float(w["utilization"]) for w in known if w.get("utilization") is not None]
    if used:
        return round(max(0.0, 1 - max(used)), 2)
    return CLOSE if any(w.get("status") == "allowed_warning" for w in known) else 1.0


def problem(check: dict | None, kind: str, needs: list[str] = ()) -> str:
    """Why a machine can't take this kind of job now ("isn't online"), or "" if it can. `needs`: programs it runs."""
    if not check:
        return "isn't online"
    if not check.get("same_protocol"):
        return "is updating to this computer's code"
    if needs and (has := check.get("has")) is not None and (lacking := [p for p in needs if p not in has]):
        return f"doesn't have {', '.join(lacking)}"
    if kind == TASKS:
        if (check.get("free") or 0) < 1:
            return "is busy with as many tasks as it runs at once"
        if usage.out(kept=check.get("usage") or {}):
            return "has used up its Claude account's limit"
    return ""


@dataclass(frozen=True)
class Standing:
    """How much one computer has to spare for a job, as the choice weighs it."""
    cores: int
    load: float | None
    free: int
    left: float  # usage_left of its Claude account

    def room(self, kind: str) -> tuple:
        """Larger is better: for a Claude task, a free slot and Claude usage first; then idle cores."""
        idle = max(0.0, self.cores - (self.load if self.load is not None else self.cores / 2))
        return (self.free > 0, round(self.left, 1), idle) if kind == TASKS else (idle, self.cores)


def _standing(m: machines.Machine, check: dict) -> Standing:
    return Standing(int(check.get("cores") or m.cores or 0), check.get("load"), int(check.get("free") or 0),
                    usage_left(check.get("usage") or {}))


def _here(running: int, slots: int) -> Standing:
    try:
        load = round(os.getloadavg()[0], 2)
    except OSError:
        load = None
    return Standing(os.cpu_count() or 0, load, max(0, slots - running), usage_left())


def _there(found: machines.Machine, said: str = "") -> Place:
    return Place(found.id, found.name, said)


def _fallback(found: machines.Machine, why: str) -> Place:
    return Place(said=f"{found.name} {why}, so it's running on this computer instead.")


def _cant_here(kind: str, lacking: list[str]) -> str:
    """Why this computer can't take the job, or ""."""
    if lacking:
        return f"This computer doesn't have {', '.join(lacking)}"
    if kind == TASKS and usage.out():
        return "This computer's Claude account has hit its limit"
    return ""


async def choose(hub, kind: str, job: dict, asked: str = "", running_here: int = 0, slots_here: int = 1) -> Place:
    """Where `job` (a description for the choice) of `kind` runs. ValueError if `asked` names no machine."""
    if asked.strip().lower() == machines.HERE:
        return HERE
    needs = commands.programs(str(job.get("command") or "")) if kind == COMMANDS else []
    if asked.strip():
        found = machines.find(asked)
        return _there(found) if not (why := problem(await hub.check(found.id, needs=needs), kind, needs)) \
            else _fallback(found, why)
    found_here = await home.have(needs) if needs else []
    cant = _cant_here(kind, [p for p in needs if found_here is not None and p not in found_here])
    setting = machines.placement()[kind]
    if setting == machines.HERE:
        return await _cover(hub, kind, needs, cant) if cant else HERE
    if setting != machines.AUTO:
        found = machines.need(int(setting))
        if not (why := problem(await hub.check(found.id, needs=needs), kind, needs)):
            return _there(found)
        return await _cover(hub, kind, needs, cant) if cant else _fallback(found, why)
    if cant:
        return await _cover(hub, kind, needs, cant)
    able = await _able(hub, kind, needs)
    return await _pick(kind, job, needs, able, running_here, slots_here) if able else HERE


async def _able(hub, kind: str, needs: list[str]) -> dict[int, tuple[machines.Machine, dict]]:
    """The paired machines able to take the job now, with their online checks."""
    checks = await hub.check_all(needs=needs)
    return {m.id: (m, checks[m.id]) for m in machines.paired() if not problem(checks.get(m.id), kind, needs)}


async def _cover(hub, kind: str, needs: list[str], cant: str) -> Place:
    """This computer can't take the job (`cant` says why): the able machine with the most room, else here."""
    return _best(kind, await _able(hub, kind, needs), None, f"{cant}, so it's running on {{name}}.")


def _best(kind: str, able: dict, here: Standing | None, said: str = "") -> Place:
    """The computer with the most room for the job: an able machine, or this one (`here`, when it can take it), which
    wins a tie. `said` is what to say, with {name} for the machine's."""
    if not able:
        return HERE
    m, check = max(able.values(), key=lambda mc: _standing(*mc).room(kind))
    if here is not None and here.room(kind) >= _standing(m, check).room(kind):
        return HERE
    return _there(m, said.format(name=m.name))


def _shown(s: Standing) -> dict:
    return {"cores": s.cores, "load": s.load, "free_slots": s.free, "claude_usage_left_percent": round(s.left * 100)}


async def _pick(kind: str, job: dict, needs: list[str], able: dict, running: int, slots: int) -> Place:
    """Let the model choose among this computer and the machines able to take it; if it can't be asked, the one with
    the most room."""
    from jarvis.brain import sdk

    here = _here(running, slots)
    there = {str(i): {"name": m.name, "ram_gb": m.ram_gb, "programs": m.tools} | _shown(_standing(m, check))
             | {"claude_usage": usage.said(kept=check.get("usage") or {})} for i, (m, check) in able.items()}
    material = {"job": job | ({"runs_programs": needs} if needs else {}),
                "this_computer": {"ram_gb": round(psutil.virtual_memory().total / 1e9, 1)} | _shown(here)
                | {"claude_tasks_running": running, "claude_usage": usage.said()},
                "machines": there}
    schema = {"type": "object", "properties": {"where": {"enum": ["here", *there]}, "why": {"type": "string"}},
              "required": ["where"]}
    try:
        found, _ = await sdk.ask_json(json.dumps(material, ensure_ascii=False), schema,
                                      prompts.load("placement", user_name=profile.name()), PLACEMENT_MODEL,
                                      timeout=CHOICE_SECONDS)
    except Exception as e:
        log.warning("choosing where %s runs failed (%s); the computer with the most room takes it", job.get("title"),
                    type(e).__name__)
        found = None
    if not found:
        return _best(kind, able, here)
    if found["where"] == "here":
        return HERE
    m, _ = able[int(found["where"])]
    return _there(m)
