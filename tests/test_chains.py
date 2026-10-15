"""Tests for work that waits for other work (jarvis.tasks.chains): tasks that start by themselves once others finish
or are merged, merges that go ahead by themselves, holding what can't start as planned, and surviving a restart."""

import asyncio
import json
from pathlib import Path

import pytest
from test_in_place import add_new_file, branches, env  # noqa: F401  (env is a fixture)
from test_nudges import until

from jarvis.core.database import WAIT_AFTER
from jarvis.tasks import git_ops, runner
from jarvis.tasks.worker import Outcome
from jarvis.tools import tasks as task_tools


def gated(monkeypatch, edit=None) -> tuple[dict[str, asyncio.Event], list[str]]:
    """Fake worker.run: each task runs until its title's gate opens (made on first use), then succeeds, or fails if
    its title starts with "bad". Returns the gates and the titles in the order they began."""
    gates: dict[str, asyncio.Event] = {}
    began: list[str] = []

    async def run(task: dict, prompt: str, reporter) -> Outcome:
        began.append(task["title"])
        reporter.session(task["id"], f"s{task['id']}")
        await gates.setdefault(task["title"], asyncio.Event()).wait()
        if edit and task["worktree"]:
            edit(task)
        if task["title"].startswith("bad"):
            return Outcome("", "it broke", 0.01)
        return Outcome(f"Did {task['title']}.", "", 0.01)

    monkeypatch.setattr(runner, "run", run)
    return gates, began


def finish(gates: dict[str, asyncio.Event], title: str) -> None:
    gates.setdefault(title, asyncio.Event()).set()


def status(r: runner.Runner, task: dict) -> str:
    return r.store.get(task["id"])["status"]


async def ended(r: runner.Runner, *tasks: dict) -> None:
    await until(lambda: all(status(r, t) not in ("queued", "running") or r.store.get(t["id"])["waits_on"]
                            for t in tasks) and not r.jobs)


def heard(events: asyncio.Queue) -> list[str]:
    said = []
    while not events.empty():
        said.append(events.get_nowait()["text"])
    return said


def listening(r: runner.Runner) -> asyncio.Queue:
    events: asyncio.Queue = asyncio.Queue()
    r.listeners.add(events)
    return events


# ── Starting by itself ──────────────────────────────────────────


def test_a_task_waits_without_running_then_starts_by_itself_when_the_one_before_finishes(env, monkeypatch):
    gates, began = gated(monkeypatch)

    async def main():
        r = runner.Runner()
        events = listening(r)
        first = r.start("research", "first", "Look up A.")
        second = r.start("research", "second", "Look up B.", after=[first["id"]])
        assert second["status"] == "queued" and second["id"] not in r.jobs
        assert second["activity"] == f"waiting for task {first['id']} to finish, then it starts by itself"
        assert r.view(second)["waits_for"] == [f"task {first['id']} to finish"]
        await until(lambda: began == ["first"])
        await asyncio.sleep(0.05)
        assert began == ["first"]  # nothing runs it meanwhile
        finish(gates, "first")
        await until(lambda: began == ["first", "second"])
        finish(gates, "second")
        await ended(r, first, second)
        return r, heard(events), first, second

    r, said, first, second = asyncio.run(main())
    assert status(r, second) == "done" and r.store.get(second["id"])["waits_on"] is None
    start = f"Task {second['id']}, second, starts now that task {first['id']} has finished."
    assert said.index(start) > said.index(next(s for s in said if "first" in s and "done" in s.lower()))
    assert start in [e["text"] for e in r.store.history(second["id"]) if e["kind"] == "waited"]


def test_a_chain_of_three_runs_in_order(env, monkeypatch):
    gates, began = gated(monkeypatch)

    async def main():
        r = runner.Runner()
        one = r.start("research", "one", "Step one.")
        two = r.start("research", "two", "Step two.", after=[one["id"]])
        three = r.start("research", "three", "Step three.", after=[two["id"]])
        for title in ("one", "two", "three"):
            await until(lambda: began[-1:] == [title])
            finish(gates, title)
        await ended(r, one, two, three)
        return r, (one, two, three)

    r, tasks = asyncio.run(main())
    assert began == ["one", "two", "three"] and [status(r, t) for t in tasks] == ["done"] * 3


def test_several_tasks_waiting_for_one_all_start_when_it_finishes(env, monkeypatch):
    gates, began = gated(monkeypatch)

    async def main():
        r = runner.Runner()
        first = r.start("research", "first", "Look up A.")
        rest = [r.start("research", name, f"Look up {name}.", after=[first["id"]]) for name in ("left", "right")]
        await until(lambda: began == ["first"])
        finish(gates, "first")
        await until(lambda: sorted(began[1:]) == ["left", "right"])
        for name in ("left", "right"):
            finish(gates, name)
        await ended(r, first, *rest)
        return r, rest

    r, rest = asyncio.run(main())
    assert [status(r, t) for t in rest] == ["done", "done"]


def test_a_task_waiting_for_several_starts_only_once_all_have_finished(env, monkeypatch):
    gates, began = gated(monkeypatch)

    async def main():
        r = runner.Runner()
        a = r.start("research", "a", "Look up A.")
        b = r.start("research", "b", "Look up B.")
        last = r.start("research", "last", "Put them together.", after=[a["id"], b["id"]])
        await until(lambda: sorted(began) == ["a", "b"])
        finish(gates, "a")
        await until(lambda: status(r, a) == "done")
        await asyncio.sleep(0.05)
        assert "last" not in began and r.view(r.store.get(last["id"]))["waits_for"] == [f"task {b['id']} to finish"]
        assert r.store.get(last["id"])["activity"] == f"waiting for task {b['id']} to finish, then it starts by itself"
        finish(gates, "b")
        await until(lambda: "last" in began)
        finish(gates, "last")
        await ended(r, a, b, last)
        return r, last, (a, b)

    r, last, (a, b) = asyncio.run(main())
    assert status(r, last) == "done"
    assert f"starts now that task {a['id']} has finished and task {b['id']} has finished" in \
        " ".join(e["text"] for e in r.store.history(last["id"]))


def test_waiting_for_a_task_already_finished_starts_at_once(env, monkeypatch):
    gates, began = gated(monkeypatch)
    finish(gates, "first")
    finish(gates, "second")

    async def main():
        r = runner.Runner()
        first = r.start("research", "first", "Look up A.")
        await ended(r, first)
        second = r.start("research", "second", "Look up B.", after=[first["id"]])
        assert second["status"] in ("queued", "running") and second["id"] in r.jobs
        await ended(r, second)
        return r, second

    r, second = asyncio.run(main())
    assert status(r, second) == "done"


def test_a_message_to_a_waiting_task_goes_with_it_when_it_starts(env, monkeypatch):
    gates, began = gated(monkeypatch)
    prompts = []

    async def main():
        r = runner.Runner()
        first = r.start("research", "first", "Look up A.")
        second = r.start("research", "second", "Look up B.", after=[first["id"]])
        r.reply(second["id"], "Only this year's.")
        assert status(r, second) == "queued" and second["id"] not in r.jobs  # still waits
        finish(gates, "first")
        await until(lambda: "second" in began)
        prompts.append(r.store.get(second["id"])["instruction"])
        finish(gates, "second")
        await ended(r, first, second)

    asyncio.run(main())
    assert "Only this year's." in prompts[0]


# ── Refusing what can't be waited for ───────────────────────────


def test_waiting_for_a_task_that_already_failed_or_doesnt_exist_is_refused(env, monkeypatch):
    gates, began = gated(monkeypatch)
    finish(gates, "bad one")

    async def main():
        r = runner.Runner()
        bad = r.start("research", "bad one", "Break.")
        await ended(r, bad)
        with pytest.raises(ValueError, match="failed, so nothing can wait for it"):
            r.start("research", "after it", "Go on.", after=[bad["id"]])
        with pytest.raises(ValueError, match="There's no task 999"):
            r.start("research", "after nothing", "Go on.", after=[999])
        with pytest.raises(ValueError, match="no branch of its own"):
            r.start("research", "after a merge", "Go on.", after_merge=[bad["id"]])
        return r

    r = asyncio.run(main())
    assert [t["title"] for t in r.store.active()] == []  # nothing half made


# ── Holding what can't start as planned ─────────────────────────


@pytest.mark.parametrize("how", ["fails", "stopped"])
def test_when_what_it_waits_for_fails_or_is_stopped_the_task_is_held_and_the_user_asked(env, monkeypatch, how):
    gates, began = gated(monkeypatch)

    async def main():
        r = runner.Runner()
        events = listening(r)
        first = r.start("research", "bad first" if how == "fails" else "first", "Look up A.")
        second = r.start("research", "second", "Look up B.", after=[first["id"]])
        third = r.start("research", "third", "Look up C.", after=[second["id"]])
        await until(lambda: len(began) == 1)
        if how == "fails":
            finish(gates, "bad first")
        else:
            r.stop(first["id"])
        await until(lambda: status(r, second) == "waiting")
        await asyncio.sleep(0.05)
        return r, heard(events), first, second, third

    r, said, first, second, third = asyncio.run(main())
    assert len(began) == 1  # neither started
    held = r.store.get(second["id"])
    word = "failed" if how == "fails" else "was stopped"
    assert held["wait_kind"] == WAIT_AFTER and held["seen"] == 0
    assert f"task {first['id']}" in held["question"] and word in held["question"] and "anyway" in held["question"]
    assert any(f"Task {second['id']}, second, hasn't started" in s and f"Task {third['id']} waits for it in turn" in s
               for s in said)
    assert status(r, third) == "queued"  # still waits on the held one, which the user decides on


def test_a_held_task_starts_on_the_users_reply_and_the_chain_carries_on(env, monkeypatch):
    gates, began = gated(monkeypatch)
    finish(gates, "bad first")

    async def main():
        r = runner.Runner()
        first = r.start("research", "bad first", "Look up A.")
        second = r.start("research", "second", "Look up B.", after=[first["id"]])
        third = r.start("research", "third", "Look up C.", after=[second["id"]])
        await until(lambda: status(r, second) == "waiting")
        r.reply(second["id"], "Start it anyway.")
        await until(lambda: "second" in began)
        finish(gates, "second")
        await until(lambda: "third" in began)
        finish(gates, "third")
        await ended(r, second, third)
        return r, second, third

    r, second, third = asyncio.run(main())
    assert status(r, second) == "done" and status(r, third) == "done"
    assert r.store.get(second["id"])["waits_on"] is None


def test_stopping_a_held_or_waiting_task_holds_what_waits_for_it(env, monkeypatch):
    gates, began = gated(monkeypatch)

    async def main():
        r = runner.Runner()
        first = r.start("research", "first", "Look up A.")
        second = r.start("research", "second", "Look up B.", after=[first["id"]])
        third = r.start("research", "third", "Look up C.", after=[second["id"]])
        stopped = r.stop(second["id"])
        finish(gates, "first")
        await ended(r, first)
        await asyncio.sleep(0.05)
        return r, stopped, third

    r, stopped, third = asyncio.run(main())
    assert stopped["status"] == "stopped" and began == ["first"]
    assert status(r, third) == "waiting" and r.store.get(third["id"])["wait_kind"] == WAIT_AFTER


def test_a_held_task_still_starts_by_itself_if_what_it_waited_for_comes_right(env, monkeypatch):
    gates, began = gated(monkeypatch)

    async def main():
        r = runner.Runner()
        first = r.start("research", "first", "Look up A.")
        second = r.start("research", "second", "Look up B.", after=[first["id"]])
        await until(lambda: began == ["first"])
        r.stop(first["id"])
        await until(lambda: status(r, second) == "waiting")
        r.reply(first["id"], "Carry on.")  # the user picks the first back up instead
        await until(lambda: began == ["first", "first"])
        finish(gates, "first")
        await until(lambda: "second" in began)
        finish(gates, "second")
        await ended(r, first, second)
        return r, second

    r, second = asyncio.run(main())
    assert status(r, second) == "done"


# ── Merges ──────────────────────────────────────────────────────


def new_file(task: dict) -> None:
    add_new_file(Path(task["worktree"]), task)


def test_a_task_waiting_for_a_merge_starts_once_the_branch_is_merged(env, monkeypatch):
    gates, began = gated(monkeypatch, new_file)

    async def main():
        r = runner.Runner()
        change = r.start("code_change", "the change", "Add new.py", "other")
        after = r.start("research", "after it", "Check it.", after_merge=[change["id"]])
        finish(gates, "the change")
        await until(lambda: status(r, change) == "done")
        await asyncio.sleep(0.05)
        assert began == ["the change"]  # finished isn't merged
        await r.merge(change["id"])
        await until(lambda: "after it" in began)
        finish(gates, "after it")
        await ended(r, after)
        return r, after

    r, after = asyncio.run(main())
    assert status(r, after) == "done" and (env["other"] / "new.py").exists()


def test_a_failed_merge_holds_what_waits_for_it(env, monkeypatch):
    gates, began = gated(monkeypatch, new_file)

    async def main():
        r = runner.Runner()
        change = r.start("code_change", "the change", "Add new.py", "other")
        after = r.start("research", "after it", "Check it.", after_merge=[change["id"]])
        finish(gates, "the change")
        await until(lambda: status(r, change) == "done")
        (env["other"] / "app.py").write_text("print('dirty')\n")  # uncommitted: the merge refuses
        with pytest.raises(ValueError, match="uncommitted changes"):
            await r.merge(change["id"])
        return r, after

    r, after = asyncio.run(main())
    held = r.store.get(after["id"])
    assert held["status"] == "waiting" and "merge failed" in held["question"]


def test_stopping_a_merge_under_way_holds_what_waits_for_it(env, monkeypatch):
    gates, began = gated(monkeypatch, new_file)
    squashing = asyncio.Event()
    real = git_ops._git_try

    async def git_slowly(cwd, *args, timeout=60):
        out = await real(cwd, *args, timeout=timeout)
        if args[0] == "merge":
            squashing.set()
            await asyncio.sleep(0.2)  # the stop lands mid-squash, which is then undone
        return out

    monkeypatch.setattr(git_ops, "_git_try", git_slowly)

    async def main():
        r = runner.Runner()
        change = r.start("code_change", "the change", "Add new.py", "other")
        after = r.start("research", "after it", "Check it.", after_merge=[change["id"]])
        finish(gates, "the change")
        await until(lambda: status(r, change) == "done")
        merging = asyncio.create_task(r.merge(change["id"]))
        await squashing.wait()
        await r.halt(change["id"])
        with pytest.raises(ValueError, match="its merge was stopped"):
            await merging
        return r, change, after

    r, change, after = asyncio.run(main())
    held = r.store.get(after["id"])
    assert not r.store.merged(change["id"]) and held["status"] == "waiting"
    assert held["question"].startswith(f"It was to start once other work finished, but task {change['id']}'s merge "
                                       "was stopped.")


def test_a_merge_set_for_later_happens_by_itself_and_then_what_waits_for_it_starts(env, monkeypatch):
    gates, began = gated(monkeypatch, new_file)

    async def main():
        r = runner.Runner()
        events = listening(r)
        change = r.start("code_change", "the change", "Add new.py", "other")
        later = r.merge_later(change["id"], after=[change["id"]])
        assert r.view(later)["merge_waits_for"] == [f"task {change['id']} to finish"]
        rebuild = r.start("research", "rebuild", "Rebuild.", after_merge=[change["id"]])
        watch = r.start("research", "watch", "Rebuild the watch.", after_merge=[change["id"]])
        finish(gates, "the change")
        await until(lambda: sorted(began[1:]) == ["rebuild", "watch"])
        finish(gates, "rebuild")
        finish(gates, "watch")
        await ended(r, rebuild, watch)
        await until(lambda: not r._bg or all(t.done() for t in r._bg))
        return r, heard(events), change, (rebuild, watch)

    r, said, change, rest = asyncio.run(main())
    assert branches(env["other"]) == ["main"] and (env["other"] / "new.py").exists()
    assert r.store.merged(change["id"]) and r.store.get(change["id"])["merge_waits_on"] is None
    assert any("merged into main" in s for s in said)
    assert [status(r, t) for t in rest] == ["done", "done"]


def test_a_merge_waiting_for_a_task_that_fails_is_called_off_and_holds_what_waits_for_it(env, monkeypatch):
    gates, began = gated(monkeypatch, new_file)

    async def main():
        r = runner.Runner()
        change = r.start("code_change", "the change", "Add new.py", "other")
        bad = r.start("research", "bad check", "Check first.")
        r.merge_later(change["id"], after=[change["id"], bad["id"]])
        after = r.start("research", "after it", "Go on.", after_merge=[change["id"]])
        finish(gates, "bad check")
        await until(lambda: status(r, after) == "waiting")
        finish(gates, "the change")
        await ended(r, change)
        return r, change, after

    r, change, after = asyncio.run(main())
    assert r.store.get(change["id"])["merge_waits_on"] is None and not r.store.merged(change["id"])
    assert any("won't be merged by itself" in e["text"] for e in r.store.history(change["id"])
               if e["kind"] == "merge called off")
    assert "merge was called off" in r.store.get(after["id"])["question"]


def test_stopping_a_finished_task_calls_off_its_waiting_merge(env, monkeypatch):
    gates, began = gated(monkeypatch, new_file)

    async def main():
        r = runner.Runner()
        change = r.start("code_change", "the change", "Add new.py", "other")
        other = r.start("research", "other", "Look up A.")
        finish(gates, "the change")
        await until(lambda: status(r, change) == "done")
        r.merge_later(change["id"], after=[other["id"]])
        r.stop(change["id"])
        finish(gates, "other")
        await ended(r, other)
        await asyncio.sleep(0.05)
        return r, change

    r, change = asyncio.run(main())
    assert not r.store.merged(change["id"]) and r.store.get(change["id"])["merge_waits_on"] is None


def test_a_wait_that_would_close_a_loop_is_refused(env, monkeypatch):
    gates, began = gated(monkeypatch, new_file)

    async def main():
        r = runner.Runner()
        change = r.start("code_change", "the change", "Add new.py", "other")
        after = r.start("research", "after it", "Go on.", after_merge=[change["id"]])
        later = r.start("research", "later", "Then this.", after=[after["id"]])
        with pytest.raises(ValueError, match="neither would ever happen"):
            r.merge_later(change["id"], after=[later["id"]])  # later -> after -> this merge -> later
        with pytest.raises(ValueError, match="can't wait for itself"):
            r.merge_later(change["id"], after_merge=[change["id"]])
        return r, change

    r, change = asyncio.run(main())
    assert r.store.get(change["id"])["merge_waits_on"] is None


# ── Restarts ────────────────────────────────────────────────────


def test_waiting_tasks_survive_a_restart_and_start_when_what_they_wait_for_ends(env, monkeypatch):
    gates, began = gated(monkeypatch)

    async def before():
        r = runner.Runner()
        first = r.start("research", "first", "Look up A.")
        second = r.start("research", "second", "Look up B.", after=[first["id"]])
        await until(lambda: began == ["first"])
        await r.shutdown()
        return first, second

    first, second = asyncio.run(before())

    async def after():
        r = runner.Runner()
        assert [i for i, _ in r.resuming] == [first["id"]] and r.store.get(second["id"])["status"] == "queued"
        r.start_background()
        assert second["id"] not in r.jobs  # still waits
        finish(gates, "first")
        await until(lambda: "second" in began)
        finish(gates, "second")
        await ended(r, first, second)
        return r

    r = asyncio.run(after())
    assert status(r, second) == "done"


def test_what_ended_while_the_runner_was_down_is_settled_at_the_next_start(env, monkeypatch):
    gates, _ = gated(monkeypatch)
    finish(gates, "starts")
    r = runner.Runner()
    done = r.store.create("research", "done one", "A.", None)
    r.store.set(done["id"], status="done")
    cut = r.store.create("research", "cut one", "B.", None)  # stopped by the restart without a word
    r.store.set(cut["id"], status="stopped")
    starts = r.store.create("research", "starts", "C.", None)
    r.store.set(starts["id"], waits_on=json.dumps([{"task": done["id"], "merged": False}]))
    held = r.store.create("research", "held", "D.", None)
    r.store.set(held["id"], waits_on=json.dumps([{"task": cut["id"], "merged": False}]))

    async def main():
        again = runner.Runner()
        again.start_background()
        await ended(again, starts)
        return again

    again = asyncio.run(main())
    assert status(again, starts) == "done" and "starts now" in " ".join(
        e["text"] for e in again.store.history(starts["id"]))
    assert status(again, held) == "waiting" and "was stopped" in again.store.get(held["id"])["question"]


# ── The voice side ──────────────────────────────────────────────


def test_the_tools_offer_waiting_and_the_prompt_says_to_use_it_instead_of_remembering():
    from jarvis import tools
    from jarvis.brain import persona

    start, merge = tools.get("task_start"), tools.get("task_merge")
    assert {"after", "after_merge"} <= set(start.properties) and {"after", "after_merge"} <= set(merge.properties)
    assert "rather than promising to remember" in start.properties["after"]["description"]
    assert "waits_for" in tools.get("task_list").description and "waits_for" in tools.get("task_detail").description
    assert "calls that merge off" in tools.get("task_stop").description
    assert "Never promise to remember it for later" in persona.persona()


def test_task_start_passes_what_it_waits_for_and_says_it_was_saved(monkeypatch):
    sent = {}

    async def start(*args, **kwargs):
        sent.update(kwargs)
        return {"id": 9, "title": "rebuild", "kind": "research", "status": "queued", "updated": 0, "seen": 1,
                "activity": "waiting for task 3 to be merged, then it starts by itself",
                "waits_for": ["task 3 to be merged"]}

    monkeypatch.setattr(task_tools.client, "start", start)
    out = asyncio.run(task_tools.task_start("research", "rebuild", "Rebuild.", after_merge=[3]))
    assert sent == {"after": None, "after_merge": [3]}
    assert out["waits_for"] == ["task 3 to be merged"] and out["now"].startswith("saved, not started yet")
