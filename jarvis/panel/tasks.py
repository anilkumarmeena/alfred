"""Control panel actions: Tasks, their approval requests, and remembered approval rules."""

import asyncio

from jarvis.panel.base import GIT_TIMEOUT, HIDDEN, LOG_LINES, SERVICE_NAMES, _id, _text, action, log  # noqa: F401
from jarvis.panel.schedules import _jobs, _quiet
from jarvis.safety import remembered
from jarvis.state import activity
from jarvis.tasks import client, data_edits, schedules
from jarvis.tasks.store import COMMAND, WAIT_APPROVAL, edits

# ── Tasks ───────────────────────────────────────────────────────


def _waits_on_requests(task: dict) -> bool:
    """Waiting only on approvals, not on a question."""
    return task["status"] == "waiting" and task.get("wait_kind") == WAIT_APPROVAL


def _task(task: dict, jobs: dict[int, str] | None = None) -> dict:
    """Task for list views: HIDDEN fields dropped, plus approval/edit flags and its job's title (from `jobs`)."""
    kept = {k: v for k, v in task.items() if k not in HIDDEN}
    return kept | {"requests_only": _waits_on_requests(task), "pending_edits": len(data_edits.pending(task)),
                   "worktree": bool(task["worktree"]), "job_title": (jobs or {}).get(task.get("job"))}


async def _job_titles(found: list[dict]) -> dict[int, str]:
    return await asyncio.to_thread(schedules.titles, {t["job"] for t in found if t.get("job") is not None})


@action
async def tasks(_: dict) -> dict:
    """Recent tasks, pending approvals, upcoming jobs and do-not-disturb/usage state."""
    found, waiting = await asyncio.gather(client.tasks(), client.approvals())
    titles = await _job_titles(found)
    return {"tasks": [_task(t, titles) for t in found], "approvals": waiting, "schedules": await _jobs(),
            **await _quiet()}


@action
async def task(body: dict) -> dict:
    """Full task: instruction, summary, latest log lines and suggested edits."""
    found = await client.log(_id(body), LOG_LINES)
    described = [{"n": e["n"], "status": e["status"], "what": data_edits.describe(e), "reason": e.get("reason", ""),
                  "needs_yes": e.get("needs_yes", ""), "result": e.get("result", "")} for e in edits(found)]
    return _task(found, await _job_titles([found])) | {"instruction": found["instruction"], "summary": found["summary"],
                                                       "log": found["log"], "edits": described}


@action
async def reply(body: dict) -> dict:
    """Send a task an answer, nudge or follow-up."""
    if not (message := _text(body, "message")):
        raise ValueError("The message is empty.")
    return _task(await client.reply(_id(body), message))


@action
async def retry(body: dict) -> dict:
    """Resume a failed/stopped task with its error as a hint. Command tasks are re-run as a new task instead."""
    found = await client.task(_id(body))
    if found.get("kind") == COMMAND:
        return _task(await client.rerun(found["id"]))
    if found["status"] not in ("failed", "stopped"):
        raise ValueError(f"Task {found['id']} is {found['status']}, not failed or stopped.")
    why = f" Last time it ended with: {found['error']}" if found.get("error") else ""
    return _task(await client.reply(found["id"], f"Try again, picking up where you left off.{why}"))


@action
async def stop(body: dict) -> dict:
    return _task(await client.stop(_id(body)))


@action
async def merge(body: dict) -> dict:
    return _task(await client.merge(_id(body)))


@action
async def discard(body: dict) -> dict:
    return _task(await client.discard(_id(body)))


@action
async def edits_(body: dict) -> dict:
    """Apply or dismiss suggested edits: {"id", "action": "apply" | "dismiss", "edits": [n, ...]}."""
    numbers = body.get("edits")
    if not isinstance(numbers, list) or not numbers:
        raise ValueError("Say which edits, by number.")
    done = await client.edits(_id(body), _text(body, "action"), [int(n) for n in numbers])
    return {"outcome": done["outcome"]}


@action
async def seen(body: dict) -> dict:
    """Mark task updates as seen so JARVIS doesn't announce them."""
    ids = body.get("ids")
    if not isinstance(ids, list):
        raise ValueError("ids must be a list.")
    await client.mark_seen([int(i) for i in ids][:100])
    return {"ok": True}


@action
async def answer(body: dict) -> dict:
    """Answer an approval: {"id", "decision": "approve" | "deny", "remember"?: "once" | "session" | "always",
    "breadth"?: "exact" | "kind"}. The page shows the resulting rule before the user taps."""
    decision = body.get("decision")
    if decision not in ("approve", "deny"):
        raise ValueError("decision must be approve or deny.")
    remember, breadth = body.get("remember") or "once", body.get("breadth") or "exact"
    answered = await client.answer(_id(body), decision, "panel", remember=remember, breadth=breadth)
    return {"state": answered["state"], "outcome": answered["outcome"], "answered": answered.get("answered", False),
            "remembered": answered.get("remembered")}


# ── Remembered approvals ────────────────────────────────────────
# Created by answering requests; here they're only listed and withdrawn (safety.remembered)


def _rules() -> list[dict]:
    return [{k: r[k] for k in ("id", "scope", "session", "covers", "who", "example", "via", "created", "uses", "last_used")}
            for r in remembered.listed()]


@action
async def approval_rules(_: dict) -> list[dict]:
    return await asyncio.to_thread(_rules)


@action
async def approval_rule_forget(body: dict) -> list[dict]:
    gone = await asyncio.to_thread(remembered.forget, _id(body))
    activity.add(f"Withdrew remembered approval {gone['id']}: {gone['covers']}", "warn")
    return await asyncio.to_thread(_rules)
