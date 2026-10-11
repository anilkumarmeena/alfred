"""Voice tool for the user's other machines (jarvis.node): which there are, whether they're online, where jobs run."""

from jarvis.node import machines
from jarvis.tasks import client

from . import tool


def _brief(m: dict, placement: dict, kinds: dict) -> dict:
    out = {"name": m["name"], "online": m["online"]}
    if m["status"] != machines.PAIRED:
        out["waiting"] = "paired, but not yet confirmed in Settings › Machines, so it takes no work"
    if m["aliases"]:
        out["also_called"] = m["aliases"]
    if m["online"]:
        out |= {"free_slots": m.get("free"), "load": m.get("load")}
        if not m.get("takes_work"):
            out["takes_no_work"] = "it runs older code; it updates itself once idle, and jobs run here meanwhile"
        elif m.get("updating"):
            out["updating"] = "it updates itself to this computer's code once its current work is done"
    if m.get("update_problem"):
        out["update_problem"] = m["update_problem"]
    out |= {"cores": m["cores"], "ram_gb": m["ram_gb"], "tools": m["tools"]}
    if m["usage"]:
        out["its_claude_usage"] = m["usage"]
    if runs := [kinds[k] for k, v in placement.items() if v == str(m["id"])]:
        out["runs"] = runs
    return out


@tool(
    "machines",
    "The user's other computers that JARVIS sends heavy work to, checked online just now: each one's free slots, "
    "load, tools and its own Claude account's usage, plus where each kind of job runs (Settings › Machines): this "
    "computer, a named machine, or JARVIS's choice. Use it when asked about the machines or where something runs. "
    "Jobs follow that setting; when the user says where a particular job should run (\"run this one here\", \"do it "
    "on the studio machine\"), pass that in the job's machine. Research, and tasks that read their mail or "
    "accounts, always run on this computer. When a chosen machine is offline or its Claude account is used up, the "
    "job runs here instead, and JARVIS says so. Pairing a machine happens only in Settings › Machines, never by "
    "voice.",
    reads=True,
)
async def machines_list() -> dict:
    found = await client.machines()
    placement, kinds = found["placement"], found["kinds"]
    return {"machines": [_brief(m, placement, kinds) for m in found["machines"]] or "none paired yet",
            "where_jobs_run": {label: machines.where(placement[kind]) for kind, label in kinds.items()}
            | {"Research": "this computer, always"}}
