"""Tests for a machine running its own projects' scheduled commands: on time whether or not the main computer is on,
never twice, within do-not-disturb, and reported back, with one summary for what ran while away."""

import asyncio
from datetime import datetime

from test_in_place import env  # noqa: F401  (env is a fixture)
from test_node_offload import World, world  # noqa: F401  (world is a fixture)
from test_node_projects import registered, there  # noqa: F401  (there is a fixture)

from jarvis.core.database import COMMAND
from jarvis.node import offload
from jarvis.tasks import command_jobs, hold, schedules, scheduler


def job(folder) -> dict:
    return schedules.add("command", "the nightly render", "echo rendered >> out.txt", every_minutes=60,
                         folder=str(folder), project="film")


async def until_true(check, seconds: float = 10) -> None:
    for _ in range(int(seconds * 50)):
        if await check() if asyncio.iscoroutinefunction(check) else check():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("timed out")


async def machine_ticks(w: World, now: float) -> None:
    """The machine's scheduler, ticking until its run has ended and been reported."""
    await w.work.schedule_tick(now)
    await until_true(lambda: not w.work._schedules()["ran"] or all(
        w.work.scheduled.how(w.work.scheduled.get(r["run"])) is not None for r in w.work._schedules()["ran"].values()))
    await w.work.schedule_tick(now + 1)


def test_the_machine_runs_its_projects_scheduled_command_and_this_computer_doesnt(there, tmp_path):
    async def main():
        w = await World(tmp_path).up()
        w.work.scheduled = command_jobs.Runs(tmp_path / "machine" / "scheduled", shell=("/bin/sh", "-c"))
        await registered(w, there)
        made = job(there.resolve())
        assert w.r.offload.owner(made) is not None
        s = scheduler.Scheduler(w.r, notify=lambda *a: asyncio.sleep(0))
        due = made["next_run"] + 1
        s.clock = lambda: datetime.fromtimestamp(due)
        assert await s.tick() == []  # this computer leaves it to the machine, and sends it the job
        assert [j["id"] for j in w.work._schedules()["jobs"]] == [made["id"]]
        await machine_ticks(w, due)
        await until_true(lambda: schedules.get(made["id"])["runs"] == 1)
        await w.down()
        return made, w.r

    made, r = asyncio.run(main())
    kept = schedules.get(made["id"])
    task = r.store.get(kept["last_task"])
    assert (there / "out.txt").read_text() == "rendered\n"
    assert task["kind"] == COMMAND and task["status"] == "done" and task["node"] and task["job"] == made["id"]
    assert "ran on Studio and is done" in kept["last_result"] and kept["next_run"] > made["next_run"]


def test_runs_while_this_computer_was_away_come_back_as_one_summary(there, tmp_path, monkeypatch):
    monkeypatch.setattr(offload, "SUMMARY_WAIT", 0.05)

    async def main():
        w = await World(tmp_path).up()
        w.work.scheduled = command_jobs.Runs(tmp_path / "machine" / "scheduled", shell=("/bin/sh", "-c"))
        await registered(w, there)
        made = job(there.resolve())
        await w.r.offload.sync_schedules()
        heard = []
        w.r.listeners.add(q := asyncio.Queue())
        w.agent.stop()  # the main computer goes away: the machine carries on alone
        await until_true(lambda: w.r.hub.connected(w.node) is None)
        await machine_ticks(w, made["next_run"] + 1)
        assert schedules.get(made["id"])["runs"] == 0  # nothing reached this computer yet
        w.agent.stopping.clear()
        w.running = asyncio.create_task(w.agent.run())  # back: the journal replays
        await until_true(lambda: not q.empty())
        while not q.empty():
            heard.append(q.get_nowait())
        await w.down()
        return made, heard

    made, heard = asyncio.run(main())
    assert schedules.get(made["id"])["runs"] == 1
    said = [e["text"] for e in heard if e["type"] == "alert"]
    assert len(said) == 1 and said[0].startswith("While JARVIS was away, Studio ran 1 scheduled job:")


def test_do_not_disturb_holds_the_machines_jobs_too(there, tmp_path):
    async def main():
        w = await World(tmp_path).up()
        w.work.scheduled = command_jobs.Runs(tmp_path / "machine" / "scheduled", shell=("/bin/sh", "-c"))
        await registered(w, there)
        made = job(there.resolve())
        hold.start("", 180)  # past the job's next run, an hour away
        await w.r.offload.sync_schedules()
        held = await w.work.schedule_tick(made["next_run"] + 1)
        hold.end()
        await w.r.offload.sync_schedules()
        runs = await w.work.schedule_tick(made["next_run"] + 1)
        await w.down()
        return held, runs, made

    held, runs, made = asyncio.run(main())
    assert held == [] and runs == [made["id"]]


def test_a_job_still_running_isnt_started_again(there, tmp_path):
    async def main():
        w = await World(tmp_path).up()
        w.work.scheduled = command_jobs.Runs(tmp_path / "machine" / "scheduled", shell=("/bin/sh", "-c"))
        await registered(w, there)
        made = schedules.add("command", "slow", "sleep 5", every_minutes=60, folder=str(there.resolve()),
                             project="film")
        await w.r.offload.sync_schedules()
        first = await w.work.schedule_tick(made["next_run"] + 1)
        again = await w.work.schedule_tick(made["next_run"] + 7200)
        for ran in w.work._schedules()["ran"].values():
            await w.work.scheduled.stop(ran["run"])
        await w.down()
        return first, again

    first, again = asyncio.run(main())
    assert first and again == []
