"""Tests for tasks carrying on by themselves: after running out of steps (up to TASK_CONTINUES times per ask), and
after Claude's usage limit resets (once per reset), or at once on another Claude login."""

import asyncio
import json
import time
from types import SimpleNamespace

import pytest
from test_in_place import env  # noqa: F401  (fixture)
from test_nudges import fake_run, finishes, settle, until

from jarvis import config
from jarvis.llm import claude_logins, retry, usage
from jarvis.tasks import runner
from jarvis.tasks.store import Store
from jarvis.tasks.worker import STEPS_OUT, Outcome


@pytest.fixture(autouse=True)
def quiet_usage(monkeypatch):
    """No usage limit known on this computer unless a test says so."""
    monkeypatch.setattr(usage, "used_up_until", lambda now=None: None)
    monkeypatch.setattr(usage, "available", lambda model, now=None: model)


def steps_out(summary: str = "Got halfway."):
    async def body(task, prompt, reporter) -> Outcome:
        reporter.session(task["id"], "s1")
        return Outcome(summary, STEPS_OUT, 0.01, retry.ERROR)

    return body


def limited(resets: float | None = None, after: float | None = None):
    """Fake run hitting the usage limit, which resets at `resets`, or `after` seconds from when it's hit."""
    async def body(task, prompt, reporter) -> Outcome:
        reporter.session(task["id"], "s1")
        return Outcome("", "it hit the Claude usage limit", 0.01, retry.LIMIT,
                       time.time() + after if after is not None else resets)

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
    _, task, said = asyncio.run(run_one())
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

    _, task = asyncio.run(main())
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
    prompts = fake_run(monkeypatch, limited(after=0.5), finishes())

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
        found = again.limit_waits.get(task["id"]), again.store.get(task["id"]), logged(again, task["id"], "interrupted")
        await again.shutdown()
        return cut, *found

    cut, waits, task, interrupted = asyncio.run(main())
    assert cut["after"] == resets and cut["prompt"] == runner.LIMIT_RESUMED
    assert task["status"] == "queued" and waits == resets
    assert runner.RESUMED_LOG not in interrupted  # nothing was cut off


@pytest.mark.parametrize("after", [3600, -60])  # still to come, or passed while JARVIS was down
def test_a_crash_while_waiting_doesnt_lose_the_wait(env, monkeypatch, after):
    store = Store()
    waiting = store.create("research", "Flights", "Find flights to Goa.", None)
    store.set(waiting["id"], status="queued", session_id="s1",
              interrupted=json.dumps({"prompt": runner.LIMIT_RESUMED, "after": time.time() + after}))
    cut = store.create("research", "Hotels", "Find a hotel.", None)
    store.set(cut["id"], status="queued", interrupted=json.dumps({"at": 1.0, "on_mac": [], "prompt": "Find a hotel."}))
    monkeypatch.setattr(runner, "RESUME_TASKS", 1)  # waits don't count against it
    monkeypatch.setattr(runner, "LIMIT_SLACK", 7200.0)  # still waiting when looked at
    fake_run(monkeypatch)  # nothing runs
    r = runner.Runner()
    assert [i for i, _ in r.resuming] == [waiting["id"], cut["id"]]

    async def main():
        r.start_background()
        await asyncio.sleep(0)
        found = r.store.get(waiting["id"]), logged(r, waiting["id"], "interrupted")
        await r.shutdown()
        return found

    task, interrupted = asyncio.run(main())
    assert task["status"] == "queued" and "usage limit to reset" in task["activity"] and interrupted == []


def test_a_reset_time_nobody_knows_is_guessed_only_once(env, monkeypatch):
    monkeypatch.setattr(usage, "UNKNOWN_RESET", 0.0)
    monkeypatch.setattr(runner, "LIMIT_SLACK", 0.0)
    prompts = fake_run(monkeypatch, limited(), limited())
    _, task, _ = asyncio.run(run_one())
    assert len(prompts) == 2 and task["status"] == "failed"  # never an endless half-hourly loop


def test_a_task_woken_early_isnt_put_back_to_wait_by_a_restart(env, monkeypatch):
    go_on = asyncio.Event()

    async def works(task, prompt, reporter) -> Outcome:
        await go_on.wait()

    fake_run(monkeypatch, limited(after=3600), works)

    async def main():
        r = runner.Runner()
        task = r.start("research", "Flights", "Find flights to Goa.")
        await until(lambda: task["id"] in r.wakes)
        r.reply(task["id"], "carry on now")
        await until(lambda: r.store.get(task["id"])["status"] == "running")
        await r.shutdown()
        return json.loads(Store().get(task["id"])["interrupted"])

    cut = asyncio.run(main())
    assert cut["after"] is None and cut["prompt"].startswith(runner.LIMIT_RESUMED)


def test_a_message_sent_before_the_limit_goes_with_the_run_after_the_reset(env, monkeypatch):
    monkeypatch.setattr(runner, "LIMIT_SLACK", 0.0)

    async def hits_with_a_message_waiting(task, prompt, reporter) -> Outcome:
        reporter.session(task["id"], "s1")
        runner_.store.add_nudge(task["id"], "use the cached results")
        return Outcome("", "it hit the Claude usage limit", 0.01, retry.LIMIT, time.time() + 0.3)

    prompts = fake_run(monkeypatch, hits_with_a_message_waiting, finishes())

    async def main():
        global runner_
        runner_ = runner.Runner()
        task = runner_.start("research", "Flights", "Find flights to Goa.")
        await settle(runner_)
        return runner_.store.get(task["id"])

    task = asyncio.run(main())
    assert prompts[1].startswith(runner.LIMIT_RESUMED) and "use the cached results" in prompts[1]
    # it waited the reset out rather than running straight into the limit again
    assert logged(runner_, task["id"], "limit")[-1] == "Claude's usage limit has reset, so it carries on by itself"


def test_a_reply_after_a_stop_lets_the_same_limit_be_waited_for_again(env, monkeypatch):
    resets = time.time() + 3600
    prompts = fake_run(monkeypatch, limited(resets), limited(resets))

    async def main():
        r = runner.Runner()
        task = r.start("research", "Flights", "Find flights to Goa.")
        await until(lambda: task["id"] in r.wakes)
        r.stop(task["id"])
        await until(lambda: r.store.get(task["id"])["status"] == "stopped")
        r.reply(task["id"], "try again")
        await until(lambda: len(prompts) == 2 and task["id"] in r.wakes)
        found = r.store.get(task["id"])
        await r.shutdown()
        return found

    assert asyncio.run(main())["status"] == "queued"  # waiting again, not failed


def test_a_limit_on_its_model_alone_carries_on_with_a_lighter_one(env, monkeypatch):
    monkeypatch.setattr(runner, "LIMIT_SLACK", 0.0)
    monkeypatch.setattr(usage, "available", lambda model, now=None: "claude-sonnet-5")
    prompts = fake_run(monkeypatch, limited(after=7 * 86400), finishes())
    _, task, said = asyncio.run(run_one())
    assert prompts[1] == runner.LIMIT_RESUMED and task["status"] == "done"
    assert "carries on with a lighter one" in said[0]


@pytest.fixture
def backup(tmp_path, monkeypatch):
    """This computer's own Claude login, holding session s1, and a backup login after it in the priority order."""
    monkeypatch.setattr(config, "CLAUDE_LOGINS_DIR", tmp_path / "claude-logins")
    monkeypatch.setattr(config, "CLAUDE_OWN_CONFIG_DIR", "")
    monkeypatch.setattr(config, "CLAUDE_CONFIG_DIR", str(tmp_path / "home-claude"))
    monkeypatch.setattr(usage, "USAGE_FILE", tmp_path / "usage.json")
    (tmp_path / "home-claude/projects/-x").mkdir(parents=True)
    (tmp_path / "home-claude/projects/-x/s1.jsonl").write_text("{}")
    claude_logins.make("backup")


def used_up(login: str, resets: float) -> None:
    """Claude reports `login`'s weekly limit used up until `resets`."""
    usage.record(SimpleNamespace(rate_limit_type="seven_day", status="rejected", resets_at=resets, utilization=1.0,
                                 overage_status=None), login=login)


def limited_on(login: str, resets: float):
    """Fake run on session s1 that uses up `login`'s limit."""
    async def body(task, prompt, reporter) -> Outcome:
        used_up(login, resets)
        return await limited(resets)(task, prompt, reporter)

    return body


def test_a_task_cut_short_by_the_usage_limit_carries_on_at_once_on_the_backup_login(env, backup, monkeypatch):
    ran_on = []

    async def resumes(task, prompt, reporter) -> Outcome:
        ran_on.append(claude_logins.resume_on(task["session_id"]))  # as worker.run picks its login
        return await finishes()(task, prompt, reporter)

    prompts = fake_run(monkeypatch, limited_on("", time.time() + 7 * 86400), resumes)
    _, task, said = asyncio.run(run_one())
    assert prompts[1] == runner.LIMIT_RESUMED and ran_on == ["backup"] and task["status"] == "done"
    assert "carries on on the backup Claude login" in said[0]


def test_a_task_waits_for_the_reset_when_the_backup_login_is_used_up_too(env, backup, monkeypatch):
    resets = time.time() + 3600
    used_up("backup", resets)
    fake_run(monkeypatch, limited_on("", resets))

    async def main():
        r = runner.Runner()
        task = r.start("research", "Flights", "Find flights to Goa.")
        await until(lambda: task["id"] in r.wakes)
        found = r.store.get(task["id"])
        await r.shutdown()
        return found

    task = asyncio.run(main())
    assert task["status"] == "queued" and "usage limit to reset" in task["activity"]


def test_a_task_already_waiting_for_the_reset_moves_to_the_backup_login(env, backup, monkeypatch):
    resets = time.time() + 7 * 86400
    used_up("", resets)
    store = Store()
    waiting = store.create("research", "Flights", "Find flights to Goa.", None)
    store.set(waiting["id"], status="queued", session_id="s1",
              interrupted=json.dumps({"prompt": runner.LIMIT_RESUMED, "after": resets}))
    prompts = fake_run(monkeypatch, finishes())

    async def main():
        r = runner.Runner()
        r.start_background()
        await settle(r)
        found = r.store.get(waiting["id"]), logged(r, waiting["id"], "limit")
        await r.shutdown()
        return found

    task, limit = asyncio.run(main())
    assert prompts == [runner.LIMIT_RESUMED] and task["status"] == "done" and task["interrupted"] is None
    assert limit[-1].startswith("the backup Claude login is free, so it carries on on that one")


def test_a_task_waiting_for_the_reset_doesnt_hold_its_project(env, monkeypatch):
    fake_run(monkeypatch, limited(after=3600))

    async def main():
        r = runner.Runner()
        task = r.start("code_change", "Fix", "Fix the bug.", "video-agent")
        await until(lambda: task["id"] in r.wakes)
        on = r._jobs_on("video-agent", in_place=True)
        await r.shutdown()
        return on

    assert asyncio.run(main()) == []  # merges and other in-place tasks needn't wait days for it


def test_a_watch_check_out_of_steps_is_left_to_the_scheduler(env, monkeypatch):
    prompts = fake_run(monkeypatch, steps_out())
    _, task, _ = asyncio.run(run_one(job=5, hidden=True))
    assert len(prompts) == 1 and task["status"] == "failed" and task["error"] == STEPS_OUT


def test_a_message_mid_run_gives_it_a_fresh_set(env, monkeypatch):
    monkeypatch.setattr(runner, "TASK_CONTINUES", 1)

    async def nudged(task, prompt, reporter) -> Outcome:
        runner_.reply(task["id"], "keep going")
        reporter.take_nudges(task["id"])  # delivered mid-run
        return Outcome("", STEPS_OUT, 0.01, retry.ERROR)

    prompts = fake_run(monkeypatch, steps_out(), nudged, finishes())

    async def main():
        global runner_
        runner_ = runner.Runner()
        task = runner_.start("research", "Flights", "Find flights to Goa.")
        await settle(runner_)
        return runner_.store.get(task["id"])

    task = asyncio.run(main())
    assert prompts[1:] == [runner.CONTINUED, runner.CONTINUED] and task["status"] == "done"
