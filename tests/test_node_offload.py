"""Tests for sending work to another machine (jarvis.node.offload and .work): long commands and Claude tasks run there,
report back in order, come home with their files, and keep the same gates as here."""

import asyncio
import contextlib
import json
from pathlib import Path

import pytest
from test_in_place import env, git  # noqa: F401  (env is a fixture)
from test_node_link import until

from jarvis.brain import sdk
from jarvis.core import vault
from jarvis.node import agent as agents, keys, machines, offload, work
from jarvis.node.journal import Journal
from jarvis.node.link import Refused, memory_pair
from jarvis.tasks import command_jobs, runner
from jarvis.tasks.worker import Outcome

SUMMARY = "Rendered the video."


@pytest.fixture
def world(env, monkeypatch, tmp_path):  # noqa: F811
    """Folders for both computers in tmp_path, a vault for this computer's key, fast polls, fixed summaries."""
    monkeypatch.setattr(vault, "ENV_FILE", tmp_path / "dotenv")
    vault.ensure()
    monkeypatch.setattr(command_jobs, "COMMANDS_DIR", tmp_path / "commands")
    monkeypatch.setattr(offload, "STATE", tmp_path / "main-node")
    monkeypatch.setattr(offload, "POLL", 0.05)
    monkeypatch.setattr(work, "WORKTREES_DIR", tmp_path / "machine" / "worktrees")
    monkeypatch.setattr(work, "DATA_DIR", tmp_path / "machine" / "data")
    monkeypatch.setattr(agents, "BACKOFF", (0.01,))

    async def ask(prompt, system, model, **options):
        if system.startswith(command_jobs.triage_system()):
            return json.dumps({"said": "It broke.", "action": "report"}), 0.0
        return SUMMARY, 0.0

    monkeypatch.setattr(sdk, "ask", ask)
    return env


class World:
    """This computer's runner and a paired, connected machine running `run` for Claude tasks."""

    def __init__(self, tmp: Path, run=None) -> None:
        self.r = runner.Runner()
        self.r.commands = command_jobs.Runs(shell=("/bin/sh", "-c"))
        self.r.hub.spool = tmp / "main-node" / "outbox"
        key = keys.new()
        found = machines.redeem(machines.new_code()["code"], keys.public(key), {"name": "Studio", "cores": 12})
        self.node = machines.confirm(found.id).id
        self.agent = agents.Agent(self.node, key, machines.main_public(), self._dial, restart=lambda: None)
        self.work = work.Work(self.agent, Journal(tmp / "machine" / "journal.db"), run=run or self._run)
        self.work.jobs = command_jobs.Runs(tmp / "machine" / "jobs", shell=("/bin/sh", "-c"))
        self.channels = []
        self.running = None

    async def _dial(self):
        ours, theirs = memory_pair()
        self.channels.append(ours)
        asyncio.create_task(self.r.hub.accept(theirs))
        return ours

    @staticmethod
    async def _run(task, prompt, reporter) -> Outcome:
        return Outcome("Did it.", "", 0.01)

    async def up(self) -> "World":
        self.running = asyncio.create_task(self.agent.run())
        await until(lambda: self.r.hub.connected(self.node) is not None)
        return self

    async def drop(self) -> None:
        """Cut the link (as a sleep would); the agent dials again by itself."""
        await self.channels[-1].close()

    async def down(self) -> None:
        self.agent.stop()
        with contextlib.suppress(Exception):
            await self.running


async def finished(r: runner.Runner, task_id: int, seconds: float = 20) -> dict:
    """Pump the runner until the task has ended and been announced."""
    for _ in range(int(seconds * 20)):
        r.check_commands()
        await r.drain_commands()
        if (task := r.store.get(task_id))["status"] not in ("queued", "running") and task_id not in r.jobs:
            return task
        await asyncio.sleep(0.05)
    raise AssertionError(f"task {task_id} didn't end: {r.store.get(task_id)['activity']}")


def logged(r: runner.Runner, task_id: int, kind: str) -> list[str]:
    return [e["text"] for e in r.store.history(task_id, 100, steps=True) if e["kind"] == kind]


# ── Long commands ───────────────────────────────────────────────


def test_a_long_command_runs_on_the_machine_and_its_files_come_back(world, tmp_path):
    video = world["video"]
    (video / "clip.txt").write_text("frames\n")

    async def main():
        w = await World(tmp_path).up()
        machines.set_placement("commands", str(w.node))
        task = w.r.start_command("cat clip.txt > out.txt && echo rendered", "the render", str(video), placeable=True)
        shown = await w.r.where(task["id"])
        assert shown["node"] == w.node and shown["where"] == "It's running on Studio."
        assert shown["machine"] == "Studio"
        done = await finished(w.r, task["id"])
        output = w.r.commands.output(task["id"], 1000)
        await w.down()
        return done, output

    done, output = asyncio.run(main())
    assert (video / "out.txt").read_text() == "frames\n"
    assert done["status"] == "done" and SUMMARY in done["spoken"] and done["spoken"].endswith("It ran on Studio.")
    assert "rendered" in output
    assert not list((tmp_path / "machine" / "worktrees").glob("node-test-t*"))  # its copy was removed


def test_a_command_the_machine_cant_take_runs_here_and_says_why(world, tmp_path):
    async def main():
        w = World(tmp_path)  # never connects
        machines.set_placement("commands", str(w.node))
        task = w.r.start_command("echo here > where.txt", "a job", str(world["video"]), placeable=True)
        shown = await w.r.where(task["id"])
        done = await finished(w.r, task["id"])
        return shown, done

    shown, done = asyncio.run(main())
    assert shown["where"] == "Studio isn't online, so it's running on this computer instead."
    assert done["status"] == "done" and not done["node"] and (world["video"] / "where.txt").exists()


def test_a_risky_command_without_the_users_yes_is_turned_down_there(world, tmp_path):
    async def main():
        w = await World(tmp_path).up()
        machines.set_placement("commands", str(w.node))
        task = w.r.start_command("rm -rf build", "clean up", str(world["video"]), placeable=True)
        await w.r.where(task["id"])
        done = await finished(w.r, task["id"])
        await w.down()
        return done

    done = asyncio.run(main())
    assert done["status"] == "failed" and "Studio turned it down" in done["spoken"] and "deletes" in done["spoken"]


def test_a_command_outside_any_project_stays_here(world, tmp_path):
    loose = tmp_path / "loose"
    loose.mkdir()

    async def main():
        w = await World(tmp_path).up()
        machines.set_placement("commands", str(w.node))
        task = w.r.start_command("echo hi", "loose job", str(loose), placeable=True)
        done = await finished(w.r, task["id"])
        await w.down()
        return done

    assert asyncio.run(main())["node"] is None


def test_stopping_a_remote_command_stops_it_there(world, tmp_path):
    async def main():
        w = await World(tmp_path).up()
        machines.set_placement("commands", str(w.node))
        task = w.r.start_command("sleep 30", "a long wait", str(world["video"]), placeable=True)
        await w.r.where(task["id"])
        await until(lambda: w.work._running_jobs() == [task["id"]])
        stopped = await w.r.halt(task["id"])
        await until(lambda: not w.work._running_jobs())
        await w.down()
        return stopped

    assert asyncio.run(main())["status"] == "stopped"


def test_a_command_told_where_by_the_user_goes_there(world, tmp_path):
    async def main():
        w = await World(tmp_path).up()
        assert machines.placement()["commands"] == machines.AUTO
        task = w.r.start_command("echo hi", "a job", str(world["video"]), placeable=True, machine="the studio")
        shown = await w.r.where(task["id"])
        with pytest.raises(ValueError, match="no machine called laptop"):
            w.r.start_command("echo hi", "a job", str(world["video"]), placeable=True, machine="laptop")
        here = w.r.start_command("echo hi", "a job", str(world["video"]), placeable=True, machine="here")
        await finished(w.r, task["id"])
        await w.down()
        return shown, await w.r.where(here["id"])

    there, here = asyncio.run(main())
    assert there["node"] is not None and here["node"] is None


# ── Claude tasks ────────────────────────────────────────────────


def test_an_in_place_task_runs_in_a_copy_there_and_its_edits_come_back(world, tmp_path):
    video = world["video"]
    (video / "notes.txt").write_text("the user's own, uncommitted\n")
    seen = {}

    async def run(task, prompt, reporter) -> Outcome:
        folder = Path(task["worktree"])
        seen["folder"], seen["notes"] = folder, (folder / "notes.txt").read_text()
        seen["git"] = git(folder, "rev-parse", "HEAD")
        (folder / "app.py").write_text("print('changed there')\n")
        (folder / "new.py").write_text("x = 1\n")
        reporter.session(task["id"], "s-1")
        reporter.progress(task["id"], "half way")
        reporter.activity(task["id"], "editing app.py")
        return Outcome("Changed it.", "", 0.02)

    async def main():
        w = await World(tmp_path, run).up()
        machines.set_placement("tasks", str(w.node))
        task = w.r.start("code_change", "change the app", "Change app.py", "video-agent")
        shown = await w.r.where(task["id"])
        done = await finished(w.r, task["id"])
        await w.down()
        return shown, done, w.r

    shown, done, r = asyncio.run(main())
    assert shown["where"] == "It's running on Studio."
    assert seen["folder"] != video and seen["notes"] == "the user's own, uncommitted\n"
    assert seen["git"] == git(video, "rev-parse", "HEAD")  # history came as a bundle
    assert (video / "app.py").read_text() == "print('changed there')\n" and (video / "new.py").exists()
    assert done["status"] == "done" and done["session_id"] == "s-1" and done["cost"] == pytest.approx(0.02)
    assert "half way" in logged(r, done["id"], "progress") and "editing app.py" in logged(r, done["id"], "step")
    assert done["spoken"].endswith("It ran on Studio.")
    assert "uncommitted" in done["changes"]


def test_a_worktree_tasks_changes_are_committed_to_its_branch_here(world, tmp_path):
    async def run(task, prompt, reporter) -> Outcome:
        (Path(task["worktree"]) / "feature.py").write_text("y = 2\n")
        return Outcome("Added it.", "", 0.01)

    async def main():
        w = await World(tmp_path, run).up()
        machines.set_placement("tasks", str(w.node))
        task = w.r.start("code_change", "add a feature", "Add feature.py", "other")
        done = await finished(w.r, task["id"])
        await w.down()
        return done

    done = asyncio.run(main())
    assert done["status"] == "done" and "1 file changed" in done["changes"]
    assert git(world["other"], "show", f"{done['branch']}:feature.py") == "y = 2"


def test_a_risky_command_there_waits_for_a_yes_given_here(world, tmp_path):
    answered = asyncio.Event()
    heard = {}

    async def run(task, prompt, reporter) -> Outcome:
        item = {"tool": "host_run", "command": "rm -rf build", "folder": task["worktree"], "why": "is harmless",
                "remember": {}}
        heard["cleared"] = await reporter.cleared(task["id"], item)
        heard["approval"] = await reporter.request_approval(task["id"], item)
        await answered.wait()
        heard["nudges"] = await reporter.take_nudges(task["id"])
        return Outcome("Done.", "", 0.01)

    async def main():
        w = await World(tmp_path, run).up()
        machines.set_placement("tasks", str(w.node))
        task = w.r.start("code_change", "clean", "Clean the build", "video-agent")
        await until(lambda: w.r.store.approvals(task["id"]), 10)
        pending = w.r.store.approvals(task["id"])[0]
        w.r.answer(pending["id"], "approve", "panel")
        answered.set()
        done = await finished(w.r, task["id"])
        await w.down()
        return pending, done

    pending, done = asyncio.run(main())
    command = pending["commands"][0]
    assert heard["cleared"] is False and heard["approval"]["id"] == pending["id"]
    assert command["machine"] == "Studio" and command["why"] != "is harmless"  # described here, not by the machine
    assert "approved request" in heard["nudges"][0] and done["status"] == "done"


def test_a_task_in_a_copy_there_never_commits_as_only_its_files_come_back(world, tmp_path):
    heard = []

    async def run(task, prompt, reporter) -> Outcome:
        heard.append(await reporter.commit_ordered(task["id"]))
        return Outcome("Done.", "", 0.01)

    async def main():
        w = await World(tmp_path, run).up()
        machines.set_placement("tasks", str(w.node))
        task = w.r.start("code_change", "fix typo", "Fix the typo", "video-agent", order="Fix the typo and commit it.")
        await finished(w.r, task["id"])
        await w.down()

    asyncio.run(main())
    assert heard == [False]


def test_reports_made_while_the_link_was_down_arrive_in_order_afterwards(world, tmp_path):
    cut, back = asyncio.Event(), asyncio.Event()
    w_ref = {}

    async def run(task, prompt, reporter) -> Outcome:
        await w_ref["w"].drop()
        cut.set()
        for n in range(3):
            reporter.progress(task["id"], f"step {n}")
        await back.wait()
        return Outcome("Done.", "", 0.01)

    async def main():
        w = w_ref["w"] = await World(tmp_path, run).up()
        machines.set_placement("tasks", str(w.node))
        task = w.r.start("code_change", "work", "Do it", "video-agent")
        await cut.wait()
        await until(lambda: w.r.hub.connected(w.node) is not None and w.agent.connected.is_set())
        back.set()
        done = await finished(w.r, task["id"])
        await w.down()
        return done, w.r

    done, r = asyncio.run(main())
    assert logged(r, done["id"], "progress")[-3:] == ["step 0", "step 1", "step 2"] and done["status"] == "done"


def test_stopping_a_remote_task_stops_its_run_there_and_keeps_its_work(world, tmp_path):
    started = asyncio.Event()

    async def run(task, prompt, reporter) -> Outcome:
        (Path(task["worktree"]) / "partial.py").write_text("z = 3\n")
        started.set()
        await asyncio.sleep(60)
        return Outcome("never", "", 0)

    async def main():
        w = await World(tmp_path, run).up()
        machines.set_placement("tasks", str(w.node))
        task = w.r.start("code_change", "slow", "Take ages", "video-agent")
        await started.wait()
        w.r.stop(task["id"])
        done = await finished(w.r, task["id"])
        await until(lambda: not w.work.runs)
        await w.down()
        return done

    done = asyncio.run(main())
    assert done["status"] == "stopped" and (world["video"] / "partial.py").exists()


def test_a_task_whose_machine_went_away_before_it_ran_runs_here(world, tmp_path, monkeypatch):
    ran_here = []

    async def run(task, prompt, reporter) -> Outcome:
        ran_here.append(task["id"])
        return Outcome("Done here.", "", 0.01)

    monkeypatch.setattr(runner, "run", run)

    async def main():
        w = World(tmp_path)
        machines.set_placement("tasks", str(w.node))
        task = w.r.start("code_question", "ask", "What is it?", "video-agent")
        shown = await w.r.where(task["id"])
        done = await finished(w.r, task["id"])
        return shown, done

    shown, done = asyncio.run(main())
    assert "isn't online" in shown["where"] and ran_here == [done["id"]] and done["node"] is None


def test_research_and_accounts_never_leave_this_computer(world, tmp_path):
    async def main():
        w = World(tmp_path)
        machines.set_placement("tasks", str(w.node))
        with pytest.raises(ValueError, match="runs on this computer"):
            w.r.start("code_question", "mail", "Check mail", "video-agent", grants=["accounts"], machine="Studio")
        task = w.r.start("research", "look", "Look it up")
        assert task["id"] not in w.r.placing
        w.r.stop(task["id"])

    asyncio.run(main())


def test_after_a_restart_here_a_run_still_going_there_is_taken_up(world, tmp_path):
    release = asyncio.Event()

    async def run(task, prompt, reporter) -> Outcome:
        reporter.session(task["id"], "s-9")
        await release.wait()
        return Outcome("Finished while you were away.", "", 0.01)

    async def main():
        w = await World(tmp_path, run).up()
        machines.set_placement("tasks", str(w.node))
        task = w.r.start("code_change", "long", "Long work", "video-agent")
        await until(lambda: w.work.runs and w.r.store.get(task["id"])["session_id"] == "s-9")
        await w.r.shutdown()  # this computer restarts; the machine carries on
        again = runner.Runner()
        again.commands = w.r.commands
        w.r = again
        again.start_background()
        await until(lambda: again.hub.connected(w.node) is not None)
        release.set()
        done = await finished(again, task["id"])
        await w.down()
        return done

    done = asyncio.run(main())
    assert done["status"] == "done" and done["summary"] == "Finished while you were away."


def test_a_run_cut_off_by_a_restart_there_carries_on_in_its_session(world, tmp_path):
    prompts = []
    gate = asyncio.Event()

    async def run(task, prompt, reporter) -> Outcome:
        prompts.append((prompt, task.get("session_id")))
        if len(prompts) == 1:
            reporter.session(task["id"], "s-5")
            gate.set()
            await asyncio.sleep(60)
        return Outcome("Carried on.", "", 0.01)

    async def main():
        w = await World(tmp_path, run).up()
        machines.set_placement("tasks", str(w.node))
        task = w.r.start("code_change", "long", "Long work", "video-agent")
        await gate.wait()
        w.work.runs[task["id"]][1].cancel()  # the machine shuts down mid-run
        await until(lambda: not w.work.runs)
        restarted = work.Work(w.agent, w.work.journal, run=run)
        assert restarted.resume() == [task["id"]]
        done = await finished(w.r, task["id"])
        await w.down()
        return done

    done = asyncio.run(main())
    assert prompts[1] == (work.RESUMED, "s-5") and done["status"] == "done" and done["summary"] == "Carried on."


def test_a_machine_lost_with_work_on_it_is_a_notice_until_it_comes_back(world, tmp_path):
    from datetime import datetime

    from jarvis.tasks import notices

    lost = [{"node": 1, "name": "Studio", "minutes": 45, "since": 1000.0, "tasks": [7, 8]}]
    check = notices.task_notices(runner.Runner().store, lost=lambda now: lost)
    found = asyncio.run(check(datetime.now()))
    assert [(n.key, n.ongoing, n.task_id) for n in found] == [("machine:1:lost:1000", True, 7)]
    assert found[0].text.startswith("Studio has been unreachable for 45 minutes, with task 7, 8 on it.")


def test_a_machine_may_only_report_on_its_own_tasks(world, tmp_path):
    async def main():
        w = World(tmp_path)
        task = w.r.store.create("code_change", "t", "i", "video-agent")
        with pytest.raises(Refused):
            await w.r.offload._report(w.node, {"seq": 1, "kind": "progress", "task": task["id"], "run": "x",
                                               "body": {"text": "hi"}})
        w.r.store.set(task["id"], node=w.node)
        with pytest.raises(Refused, match="secrets"):
            await w.r.offload._cleared(w.node, {"task": task["id"], "item": {
                "tool": "host_run", "command": "cat ~/.ssh/id_ed25519", "folder": "/x"}})

    asyncio.run(main())
