"""Tests for tasks carrying on by themselves: after running out of steps (up to TASK_CONTINUES times per ask), and
after Claude's usage limit resets (once per reset)."""

import asyncio
import json
import time

import pytest
from test_in_place import env  # noqa: F401  (fixture)
from test_nudges import fake_run, finishes, settle, until

from jarvis.brain import retry, usage
from jarvis.core.database import Store
from jarvis.tasks import runner
from jarvis.tasks.worker import STEPS_OUT, Outcome


@pytest.fixture(autouse=True)
def quiet_usage(monkeypatch):
    """No usage limit known on this computer unless a test says so."""
    monkeypatch.setattr(usage, "hold_until", lambda now=None: None)


def steps_out(summary: str = "Got halfway."):
    async def body(task, prompt, reporter) -> Outcome:
        reporter.session(task["id"], "s1")
        return Outcome(summary, STEPS_OUT, 0.01, retry.ERROR)

    return body


def limited(resets: float | None):
    async def body(task, prompt, reporter) -> Outcome:
        reporter.session(task["id"], "s1")
        return Outcome("", "it hit the Claude usage limit", 0.01, retry.LIMIT, resets)

    return body


def logged(r: runner.Runner, task_id: int, kind: str) -> list[str]:
    return [e["text"] for e in r.store.history(task_id, 100) if e["kind"] == kind]


async def run_one(*bodies, kind: str = "research", **start):
    r = runner.Runner()
    events: asyncio.Queue = asyncio.Queue()
    r.listeners.add(events)
    task = r.start(kind, "Watch the build", "Watch the build until it ends.", **start)
    await settle(r)
    return r, r.store.get(task["id"]), [events.get_nowait()["text"] for _ in range(events.qsize())]


# ── Running out of steps ────────────────────────────────────────


def test_a_task_out_of_steps_carries_on_in_its_session_and_finishes(env, monkeypatch):
    prompts = fake_run(monkeypatch, steps_out(), steps_out(), finishes("Watched it to the end."))
    r, task, said = asyncio.run(run_one())
    assert prompts[1:] == [runner.CONTINUED, runner.CONTINUED]  # same session, everything it did kept
    assert task["status"] == "done" and task["error"] is None and task["summary"] == "Watched it to the end."
    assert logged(r, task["id"], runner.CONTINUED_LOG) == [
        "ran out of steps, so JARVIS carried it on by itself (1 of 3)",
        "ran out of steps, so JARVIS carried it on by itself (2 of 3)"]
    assert len(said) == 1  # carrying on isn't announced, only the ending


def test_carrying_on_stops_at_the_cap_and_says_so(env, monkeypatch):
    monkeypatch.setattr(runner, "TASK_CONTINUES", 2)
    prompts = fake_run(monkeypatch, steps_out(), steps_out(), steps_out())
    r, task, said = asyncio.run(run_one())
    assert len(prompts) == 3 and task["status"] == "failed"
    assert task["error"] == (f"{STEPS_OUT} again after JARVIS carried it on by itself 2 times, the most it does, so it "
                             "needs a reply to carry on")
    assert len(said) == 1 and "2 times" in said[0]


def test_a_reply_after_the_cap_gives_it_a_fresh_set(env, monkeypatch):
    monkeypatch.setattr(runner, "TASK_CONTINUES", 1)
    prompts = fake_run(monkeypatch, steps_out(), steps_out(), steps_out(), finishes())

    async def main():
        r, task, _ = await run_one()
        assert task["status"] == "failed"
        r.reply(task["id"], "carry on")
        await settle(r)
        return r, r.store.get(task["id"])

    r, task = asyncio.run(main())
    assert prompts[1] == runner.CONTINUED and prompts[2].endswith("carry on") and prompts[3] == runner.CONTINUED
    assert task["status"] == "done"


def test_carrying_on_can_be_turned_off(env, monkeypatch):
    monkeypatch.setattr(runner, "TASK_CONTINUES", 0)
    prompts = fake_run(monkeypatch, steps_out())
    _, task, _ = asyncio.run(run_one())
    assert len(prompts) == 1 and task["status"] == "failed" and task["error"] == STEPS_OUT


def test_a_task_out_of_steps_with_a_question_open_is_not_carried_on(env, monkeypatch):
    async def asks(task, prompt, reporter) -> Outcome:
        reporter.session(task["id"], "s1")
        reporter.ask(task["id"], "Which branch?")
        return Outcome("", STEPS_OUT, 0.01, retry.ERROR)

    prompts = fake_run(monkeypatch, asks)
    _, task, _ = asyncio.run(run_one())
    assert len(prompts) == 1 and task["status"] == "failed" and task["question"] == "Which branch?"


# ── Usage limit ─────────────────────────────────────────────────


def test_a_task_cut_short_by_the_usage_limit_waits_for_the_reset_then_carries_on(env, monkeypatch):
    monkeypatch.setattr(runner, "LIMIT_SLACK", 0.05)
    prompts = fake_run(monkeypatch, limited(time.time() + 0.1), finishes())

    async def main():
        r = runner.Runner()
        events: asyncio.Queue = asyncio.Queue()
        r.listeners.add(events)
        task = r.start("research", "Flights", "Find flights to Goa.")
        await until(lambda: r.store.get(task["id"])["id"] in r.limit_waits)
        waiting = r.store.get(task["id"])
        await settle(r)
        return r, waiting, r.store.get(task["id"]), events.get_nowait()

    r, waiting, task, event = asyncio.run(main())
    assert waiting["status"] == "queued" and "usage limit to reset" in waiting["activity"]
    assert "carries on by itself when that resets" in event["text"] and event["task"]["seen"] == 0
    assert prompts == ["Find flights to Goa.", runner.LIMIT_RESUMED]
    assert task["status"] == "done" and task["interrupted"] is None
    assert logged(r, task["id"], "limit")[-1] == "Claude's usage limit has reset, so it carries on by itself"


def test_it_resumes_only_once_per_reset(env, monkeypatch):
    monkeypatch.setattr(runner, "LIMIT_SLACK", 0.0)
    resets = time.time() + 0.05
    prompts = fake_run(monkeypatch, limited(resets), limited(resets))
    r, task, _ = asyncio.run(run_one())
    assert len(prompts) == 2 and task["status"] == "failed"  # hit again before a new reset: told, not looped
    assert r.limit_resumed[task["id"]] == resets


def test_a_message_wakes_a_task_waiting_for_the_reset(env, monkeypatch):
    prompts = fake_run(monkeypatch, limited(time.time() + 3600), finishes())

    async def main():
        r = runner.Runner()
        task = r.start("research", "Flights", "Find flights to Goa.")
        await until(lambda: task["id"] in r.wakes)
        r.reply(task["id"], "the limit's topped up, carry on")
        await settle(r)
        return r, r.store.get(task["id"])

    r, task = asyncio.run(main())
    assert prompts[1].startswith(runner.LIMIT_RESUMED) and "the limit's topped up" in prompts[1]
    assert task["status"] == "done" and task["id"] not in r.limit_resumed  # it didn't wait the reset out


def test_scheduled_runs_leave_the_usage_limit_to_the_scheduler(env, monkeypatch):
    prompts = fake_run(monkeypatch, limited(time.time() + 3600))
    _, task, _ = asyncio.run(run_one(job=5))
    assert len(prompts) == 1 and task["status"] == "failed"


def test_a_wait_for_the_reset_survives_a_restart(env, monkeypatch):
    resets = time.time() + 3600
    fake_run(monkeypatch, limited(resets))

    async def main():
        r = runner.Runner()
        task = r.start("research", "Flights", "Find flights to Goa.")
        await until(lambda: task["id"] in r.wakes)
        await r.shutdown()
        cut = json.loads(Store().get(task["id"])["interrupted"])
        again = runner.Runner()
        again.start_background()
        await until(lambda: task["id"] in again.wakes)
        return cut, again.limit_waits.get(task["id"]), again.store.get(task["id"]), logged(again, task["id"], "interrupted")

    cut, waits, task, interrupted = asyncio.run(main())
    assert cut["after"] == resets and cut["prompt"] == runner.LIMIT_RESUMED
    assert task["status"] == "queued" and waits == resets
    assert runner.RESUMED_LOG not in interrupted  # nothing was cut off


def test_a_crash_while_waiting_doesnt_lose_the_wait(env, monkeypatch):
    store = Store()
    task = store.create("research", "Flights", "Find flights to Goa.", None)
    store.set(task["id"], status="queued", session_id="s1",
              interrupted=json.dumps({"prompt": runner.LIMIT_RESUMED, "after": time.time() + 3600}))
    monkeypatch.setattr(runner, "RESUME_TASKS", 0)  # waits don't count against it, either
    r = runner.Runner()
    assert [i for i, _ in r.resuming] == [task["id"]]
