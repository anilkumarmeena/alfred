"""Control panel actions: Scheduled jobs and quiet hours."""

import asyncio

from jarvis.llm import usage
from jarvis.panel.base import GIT_TIMEOUT, HIDDEN, LOG_LINES, SERVICE_NAMES, _id, _text, action, log  # noqa: F401
from jarvis.tasks import client, hold, schedules

# ── Scheduled jobs ──────────────────────────────────────────────
# Created by voice only; here they're listed, inspected, paused, resumed or cancelled

JOB_FIELDS = ("id", "kind", "title", "text", "status", "next_run", "last_run", "until", "runs", "last_result",
              "task_kind", "project", "only_changes", "folder")
RUNS = 10  # latest runs shown per job


def _job(job: dict) -> dict:
    note = schedules.notepad(job)
    return {k: job[k] for k in JOB_FIELDS} | {"when": schedules.describe(job), "check": schedules.check_text(job),
                                              "does": schedules.work_text(job),
                                              "notepad": note and {"at": note["at"], "text": note["text"]}}


async def _jobs() -> list[dict]:
    """Active and paused jobs, soonest first."""
    return [_job(j) for j in await asyncio.to_thread(schedules.items)]


def _run(task: dict, job: dict) -> dict:
    """One run of a job, result text without the job-title prefix."""
    said = (task.get("spoken") or task.get("error") or task.get("activity") or "").removeprefix(f"{job['title']}: ")
    return {"id": task["id"], "status": task["status"], "created": task["created"], "updated": task["updated"],
            "hidden": bool(task.get("hidden")), "result": said, "fix": bool(task.get("fixes"))}


@action
async def schedule(body: dict) -> dict:
    """Full job plus its latest task runs. Reminders and model-free watches start no tasks; see last_run/last_result."""
    job = await asyncio.to_thread(schedules.find, _id(body))
    runs, problem = [], ""
    if job["task_kind"] or job["kind"] == "command":
        try:
            runs = [_run(t, job) for t in (await client.runs(job["id"]))[:RUNS]]
        except client.RunnerUnavailable as e:
            problem = str(e)
    return _job(job) | {"recent": runs, "runs_problem": problem}


@action
async def schedule_pause(body: dict) -> list[dict]:
    await asyncio.to_thread(schedules.change, _id(body), paused=True)
    return await _jobs()


@action
async def schedule_resume(body: dict) -> list[dict]:
    await asyncio.to_thread(schedules.change, _id(body), paused=False)
    return await _jobs()


@action
async def schedule_cancel(body: dict) -> list[dict]:
    """Cancel a job; an in-progress run continues until its task is stopped."""
    await asyncio.to_thread(schedules.cancel, _id(body))
    return await _jobs()


# Do not disturb (tasks.hold) pauses jobs, notices and notifications; scheduled runs also wait on usage limits

def _quiet_now() -> dict:
    h = hold.status()
    wait = usage.hold_until()
    return {"hold": h and h | {"said": hold.describe(h)},
            "usage": {"said": usage.said(), "waiting": f"Scheduled tasks wait for its reset {usage.when(wait)}."
                      if wait else ""}}


async def _quiet() -> dict:
    return await asyncio.to_thread(_quiet_now)


@action
async def hold_status(_: dict) -> dict:
    """Do-not-disturb state without contacting the runner."""
    return await _quiet()


@action
async def hold_on(body: dict) -> dict:
    """Start do not disturb: {"minutes": n}, {"until": "15:00"}, or neither for indefinitely."""
    minutes = body.get("minutes") or 0
    if not isinstance(minutes, int) or isinstance(minutes, bool):
        raise ValueError("minutes must be a whole number.")
    await asyncio.to_thread(hold.start, _text(body, "until", 40), minutes)
    return await _quiet()


@action
async def hold_off(_: dict) -> dict:
    await asyncio.to_thread(hold.end)
    return await _quiet()
