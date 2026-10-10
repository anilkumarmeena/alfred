"""Tests for what task_start and task_reply report: started or queued per the runner, never a stale status."""

import asyncio

import pytest
from test_in_place import env  # noqa: F401  (a fixture)
from test_nudges import settle, until

from jarvis.brain import persona
from jarvis.tasks import client, runner
from jarvis.tasks.worker import Outcome
from jarvis import tools
from jarvis.tools import tasks as task_tools


def gated_run(monkeypatch, gate: asyncio.Event) -> list[str]:
    """Fake worker.run: each run waits on `gate`. Returns the titles of runs that began."""
    began: list[str] = []

    async def run(task: dict, prompt: str, reporter) -> Outcome:
        began.append(task["title"])
        reporter.session(task["id"], f"s{task['id']}")
        await gate.wait()
        return Outcome("Did it.", "", 0.01)

    monkeypatch.setattr(runner, "run", run)
    return began


def test_a_task_with_a_free_slot_has_started_and_one_without_is_queued(env, monkeypatch):
    gate = asyncio.Event()
    began = gated_run(monkeypatch, gate)

    async def main():
        r = runner.Runner(capacity=1)
        first = r.start("research", "first", "One")
        second = r.start("research", "second", "Two")
        await until(lambda: began == ["first"])
        waiting = r.store.get(second["id"])
        gate.set()
        await settle(r)
        return first, second, waiting, [r.store.get(t["id"]) for t in (first, second)]

    first, second, waiting, ended = asyncio.run(main())
    assert first["status"] == "running" and task_tools._launched(first)["now"] == "started: it's running"
    assert second["status"] == waiting["status"] == "queued"
    assert second["activity"] == "waiting for a free slot: 1 task runs at once"
    now = task_tools._launched(second)["now"]
    assert now.startswith("queued, not started yet: waiting for a free slot") and now.endswith("it starts by itself")
    assert [t["status"] for t in ended] == ["done", "done"]


def test_a_task_queued_behind_another_in_its_folder_says_so(env, monkeypatch):
    gate = asyncio.Event()
    gated_run(monkeypatch, gate)

    async def main():
        r = runner.Runner()
        first = r.start("code_change", "first", "One", "video-agent")
        second = r.start("code_change", "second", "Two", "video-agent")
        gate.set()
        await settle(r)
        return first, second

    first, second = asyncio.run(main())
    assert first["status"] == "running"
    assert second["status"] == "queued"
    assert f"queued behind task {first['id']}" in task_tools._launched(second)["now"]


@pytest.mark.parametrize("slot_free", [True, False])
def test_a_reply_to_a_waiting_task_says_how_it_stands_now_not_what_it_asked(env, monkeypatch, slot_free):
    gate = asyncio.Event()
    gated_run(monkeypatch, gate)

    async def main():
        r = runner.Runner(capacity=1)
        asked = r.store.create("research", "asked", "Look it up", None)
        r.store.set(asked["id"], status="waiting", question="Which year?", session_id="s1",
                    spoken="Task 1, asked, has a question: which year?")
        if not slot_free:
            busy = r.start("research", "busy", "Something else")
            assert busy["status"] == "running"
        replied = r.reply(asked["id"], "2024")
        gate.set()
        await settle(r)
        return replied, r.store.get(asked["id"])

    replied, ended = asyncio.run(main())
    brief = task_tools._launched(replied)
    assert "spoken" not in brief and "question" not in brief
    if slot_free:
        assert replied["status"] == "running" and brief["now"] == "started: it's running"
    else:
        assert replied["status"] == "queued" and brief["now"].startswith("queued, not started yet: waiting for a free slot")
    assert ended["status"] == "done"


def test_a_folder_freed_task_waiting_for_a_slot_says_so(env, monkeypatch):
    gate = asyncio.Event()
    began = gated_run(monkeypatch, gate)

    async def main():
        r = runner.Runner(capacity=1)
        r.folders["video-agent"] = lock = asyncio.Lock()
        await lock.acquire()  # as a merge would
        held = r.start("code_change", "held", "One", "video-agent")
        other = r.start("research", "other", "Two")
        await until(lambda: began == ["other"])
        lock.release()
        await until(lambda: r.store.get(held["id"])["activity"].startswith("waiting for a free slot"))
        gate.set()
        await settle(r)
        return held, other, r.store.get(held["id"])

    held, other, ended = asyncio.run(main())
    assert held["status"] == "queued" and "merge or git setup" in held["activity"]
    assert other["status"] == "running"
    assert ended["status"] == "done"


def test_a_task_stopped_before_its_job_began_ends_stopped_and_frees_its_place(env, monkeypatch):
    gate = asyncio.Event()
    began = gated_run(monkeypatch, gate)

    async def main():
        r = runner.Runner(capacity=1)
        gone = r.start("research", "gone", "One")
        r.stop(gone["id"])  # before its job's first step
        await until(lambda: not r.jobs)
        after = r.start("research", "after", "Two")
        gate.set()
        await settle(r)
        return r, r.store.get(gone["id"]), after

    r, gone, after = asyncio.run(main())
    assert gone["status"] == "stopped" and began == ["after"]
    assert after["status"] == "running"  # the stopped one holds no place in line
    assert not r.in_line


def test_access_granted_before_a_run_begins_comes_with_it_from_the_start(env, monkeypatch):
    gate = asyncio.Event()
    began = gated_run(monkeypatch, gate)

    async def main():
        r = runner.Runner()
        task = r.start("research", "list", "List them")
        r.reply(task["id"], "Run the build too.", ["shell"])  # before its job's first step
        nudge = r.store.get(task["id"])["nudges"]
        gate.set()
        await settle(r)
        return nudge

    nudge = asyncio.run(main())
    assert "You now also have" in nudge and "next turn" not in nudge  # no need to end its turn for it
    assert began == ["list"]


def test_the_tools_say_started_queued_or_nudged(monkeypatch):
    results = iter([
        {"id": 1, "title": "a", "kind": "research", "status": "running", "activity": "starting", "updated": 0},
        {"id": 2, "title": "b", "kind": "research", "status": "queued", "updated": 0,
         "activity": "waiting for a free slot: 3 tasks run at once"},
        {"id": 3, "title": "c", "kind": "research", "status": "running", "updated": 0, "nudges": '["also X"]'},
    ])

    async def start(*args, **kwargs):
        return next(results)

    async def reply(*args, **kwargs):
        return next(results)

    monkeypatch.setattr(client, "start", start)
    monkeypatch.setattr(client, "reply", reply)
    started = asyncio.run(task_tools.task_start("research", "a", "Look it up"))
    queued = asyncio.run(task_tools.task_reply(2, "Go ahead"))
    nudged = asyncio.run(task_tools.task_reply(3, "also X"))
    assert started["now"] == "started: it's running"
    assert queued["now"] == "queued, not started yet: waiting for a free slot: 3 tasks run at once; it starts by itself"
    assert nudged["now"] == "running: it gets the message after its current step"
    for name in ("task_start", "task_reply"):
        assert "never that it started when it's queued" in tools.get(name).description
    assert "whether it started or is queued" in persona.persona()
