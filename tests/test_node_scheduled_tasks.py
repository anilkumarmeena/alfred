"""Tests for scheduled Claude tasks of projects that live on another machine: that machine starts each when due, by
the main computer when it's there and alone while it's away, never twice; a run alone is taken up as the task it is
when the main computer is back, said in one summary, and a risky command in it still waits for the user's yes."""

import asyncio
import json
from datetime import datetime
from pathlib import Path

from test_in_place import env, git  # noqa: F401  (env is a fixture)
from test_node_offload import World, finished, world  # noqa: F401  (world is a fixture)
from test_node_projects import registered, there  # noqa: F401  (there is a fixture)
from test_node_schedules import until_true

from jarvis.node import offload
from jarvis.tasks import schedules, scheduler
from jarvis.tasks.worker import Outcome


def nightly() -> dict:
    return schedules.add("task", "nightly tidy", "Tidy the scenes", every_minutes=60, task_kind="code_change",
                         project="film")


def with_scheduler(w: World, due: float) -> scheduler.Scheduler:
    """This computer's scheduler, as serve() gives the runner one, its clock at `due`."""
    async def quiet(*_):
        pass

    s = w.r.scheduler = scheduler.Scheduler(w.r, notify=quiet)
    s.clock = lambda: datetime.fromtimestamp(due)
    return s


async def back(w: World) -> None:
    """The main computer is on again: the machine dials in, and its journal replays."""
    w.agent.stopping.clear()
    w.running = asyncio.create_task(w.agent.run())
    await until_true(lambda: w.r.hub.connected(w.node) is not None)


def job_tasks(r, job_id: int) -> list[dict]:
    return [t for t in r.store.recent(50) if t["job"] == job_id]


def test_with_this_computer_on_the_machine_hands_its_task_over_and_it_runs_once(there, tmp_path):
    seen = []

    async def run(task, prompt, reporter) -> Outcome:
        seen.append(task["id"])
        return Outcome("Tidied three scenes.\nNOTEPAD: scenes 1-3 tidied", "", 0.01)

    async def main():
        w = await World(tmp_path, run).up()
        await registered(w, there)
        made = nightly()
        due = made["next_run"] + 1
        assert await with_scheduler(w, due).tick() == []  # this computer leaves it to the machine, and sends it over
        sent = w.work._schedules()["jobs"][0]
        assert sent["kind"] == "task" and "Tidy the scenes" in sent["prompt"] and sent["folder"] == str(there.resolve())
        assert await w.work.schedule_tick(due) == [made["id"]]
        assert await w.work.schedule_tick(due + 5) == []  # once
        task = await finished(w.r, schedules.get(made["id"])["last_task"])
        again = await w.r.offload._due(w.node, {"job": made["id"], "due": f"{made['id']}-{int(made['next_run'])}",
                                                "at": due})
        await w.down()
        return made, task, again, w.r

    made, task, again, r = asyncio.run(main())
    kept = schedules.get(made["id"])
    assert seen == [task["id"]] and again["task"] == task["id"]  # asked again, the same task: never a second run
    assert task["status"] == "done" and task["node"] and task["job"] == made["id"] and len(job_tasks(r, made["id"])) == 1
    assert kept["runs"] == 1 and kept["next_run"] > made["next_run"]
    assert json.loads(kept["notepad"])["text"] == "scenes 1-3 tidied"


def test_while_this_computer_is_away_the_machine_runs_it_alone_and_it_is_taken_up_on_return(there, tmp_path,
                                                                                           monkeypatch):
    monkeypatch.setattr(offload, "SUMMARY_WAIT", 0.05)
    (there / "app.py").write_text("print('mine, uncommitted')\n")
    seen = []

    async def run(task, prompt, reporter) -> Outcome:
        seen.append(dict(task))
        reporter.progress(task["id"], "tidying the scenes")
        (Path(task["worktree"]) / "tidy.txt").write_text("tidied\n")
        return Outcome("Tidied three scenes.\nNOTEPAD: scenes 1-3 tidied", "", 0.01)

    async def main():
        w = await World(tmp_path, run).up()
        await registered(w, there)
        made = nightly()
        due = made["next_run"] + 1
        await with_scheduler(w, due).tick()
        w.r.listeners.add(heard := asyncio.Queue())
        w.agent.stop()  # the main computer goes away
        await until_true(lambda: w.r.hub.connected(w.node) is None)
        assert await w.work.schedule_tick(due) == [made["id"]]
        await until_true(lambda: not w.work.runs)  # it ran to its end, alone
        assert await w.work.schedule_tick(due + 5) == [] and schedules.get(made["id"])["runs"] == 0
        await back(w)
        await until_true(lambda: schedules.get(made["id"])["runs"] == 1)
        task = await finished(w.r, schedules.get(made["id"])["last_task"])
        await until_true(lambda: not heard.empty() and any(e.get("type") == "alert" for e in list(heard._queue)))
        await w.down()
        return made, task, [e for e in list(heard._queue) if e.get("type") == "alert"], w.r

    made, task, alerts, r = asyncio.run(main())
    assert seen[0]["id"] < 0 and seen[0]["worktree"] == str(there.resolve())  # a stand-in id while it was alone
    assert (there / "tidy.txt").exists() and git(there, "for-each-ref", "refs/jarvis/")  # own work saved first
    assert task["status"] == "done" and task["node"] and task["job"] == made["id"]
    assert task["worktree"] == str(there.resolve()) and "untracked" in task["changes"]
    logged = {e["kind"]: e["text"] for e in r.store.history(task["id"], 50, steps=True)}
    assert "while JARVIS was away" in logged["machine"] and logged["progress"] == "tidying the scenes"
    assert len(alerts) == 1 and alerts[0]["text"].startswith("While JARVIS was away, Studio ran 1 scheduled job:")
    assert len(job_tasks(r, made["id"])) == 1 and json.loads(schedules.get(made["id"])["notepad"])["text"]


def test_a_risky_command_in_a_run_alone_waits_for_the_users_yes(there, tmp_path):
    item = {"tool": "host_run", "command": "rm -rf build", "why": "cleans", "remember": {}}

    async def run(task, prompt, reporter) -> Outcome:
        if await reporter.cleared(task["id"], item | {"folder": task["worktree"]}):
            return Outcome("Cleaned the build.", "", 0.01)
        await reporter.request_approval(task["id"], item | {"folder": task["worktree"]})
        return Outcome("Waiting for a yes to clean the build.", "", 0.01)

    async def main():
        w = await World(tmp_path, run).up()
        await registered(w, there)
        made = nightly()
        due = made["next_run"] + 1
        await with_scheduler(w, due).tick()
        w.agent.stop()
        await until_true(lambda: w.r.hub.connected(w.node) is None)
        await w.work.schedule_tick(due)
        await until_true(lambda: not w.work.runs)
        await back(w)
        await until_true(lambda: schedules.get(made["id"])["runs"] == 1)
        task_id = schedules.get(made["id"])["last_task"]
        waiting = await finished(w.r, task_id)
        pending = w.r.store.approvals(task_id)
        w.r.answer(pending[0]["id"], "approve", "panel")
        done = await finished(w.r, task_id)
        await w.down()
        return waiting, pending, done

    waiting, pending, done = asyncio.run(main())
    assert waiting["status"] == "waiting" and pending[0]["commands"][0]["machine"] == "Studio"  # asked here, on return
    assert done["status"] == "done" and "Cleaned" in done["summary"]


def test_a_handover_whose_answer_was_lost_is_never_run_alone_but_asked_again(there, tmp_path):
    seen = []

    async def run(task, prompt, reporter) -> Outcome:
        seen.append(task["id"])
        return Outcome("Tidied.", "", 0.01)

    async def main():
        w = World(tmp_path, run)
        due_there = w.r.offload._due

        async def lost(node, body):  # this computer starts it, but the answer never reaches the machine
            got = await due_there(node, body)
            await until_true(lambda: seen)  # its run has begun there
            w.agent.stop()
            await asyncio.sleep(0.1)
            return got

        w.r.hub.handlers["schedule.due"] = lost
        await w.up()
        await registered(w, there)
        made = nightly()
        due = made["next_run"] + 1
        await with_scheduler(w, due).tick()
        assert await w.work.schedule_tick(due) == [made["id"]]
        await until_true(lambda: w.r.hub.connected(w.node) is None)
        assert await w.work.schedule_tick(due + 5) == [] and not w.work.runs  # not begun alone: it may be running
        w.r.hub.handlers["schedule.due"] = due_there
        await back(w)
        await w.work.schedule_tick(due + 10)  # asked again: the same task
        task = await finished(w.r, schedules.get(made["id"])["last_task"])
        await w.down()
        return made, task, w.work._schedules()["ran"][str(made["id"])], w.r

    made, task, ran, r = asyncio.run(main())
    assert ran["claude"] == "main" and ran["task"] == task["id"]
    assert seen == [task["id"]] and len(job_tasks(r, made["id"])) == 1
