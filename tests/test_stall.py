"""Tests for tasks.stall: a running task gone quiet is flagged once, never stopped."""

import asyncio
import time

import pytest
from claude_agent_sdk import AssistantMessage, RateLimitEvent, RateLimitInfo, ToolResultBlock, ToolUseBlock, UserMessage
from test_in_place import env  # noqa: F401  (a fixture)
from test_notices import AFTERNOON, Notifier
from test_nudges import FakeClient, result, settle, until, worker_run
from test_subagents import Recorder

from jarvis.llm import usage
from jarvis.safety import host
from jarvis.tasks import notices, runner, stall
from jarvis.tasks.stall import GRACE, Pulse
from jarvis.tasks.worker import HOST_RUN, JOB_WAIT, Outcome
from jarvis.tools import tasks as task_tools

NOW = AFTERNOON.timestamp()
MINUTE = 60


class Clock:
    def __init__(self, at: float = NOW) -> None:
        self.at = at

    def __call__(self) -> float:
        return self.at


class Store:
    """Just what the check reads of tasks.db: a task's pending requests and its latest log line."""

    def __init__(self, approvals: list | None = None, logged: float = 0.0) -> None:
        self.pending, self.logged = approvals or [], logged

    def approvals(self, task_id: int) -> list:
        return self.pending

    def last_logged(self, task_id: int) -> float:
        return self.logged


class Jobs:
    def __init__(self, *states: str) -> None:
        self.states = states

    def status(self, task: int | None = None) -> list[dict]:
        return [{"id": i, "state": s} for i, s in enumerate(self.states, 1)]


def running(**fields) -> dict:
    return {"id": 7, "title": "retry logic", "status": "running", "updated": NOW - 20 * MINUTE, "question": None,
            "wants": None} | fields


def check(task: dict, store=None, pulse: Pulse | None = None, jobs=None, at: float = NOW, minutes: int = 15):
    return stall.check(task, store or Store(), pulse or Pulse(Clock()), jobs or Jobs(), at, minutes)


def called(call: str, name: str, **args) -> AssistantMessage:
    return AssistantMessage(content=[ToolUseBlock(id=call, name=name, input=args)], model="m", parent_tool_use_id=None)


def answered(call: str) -> UserMessage:
    return UserMessage(content=[ToolResultBlock(tool_use_id=call, content="ok")])


def limit(status: str, resets_at: int | None = None) -> RateLimitEvent:
    return RateLimitEvent(rate_limit_info=RateLimitInfo(status=status, resets_at=resets_at, rate_limit_type="five_hour"),
                          uuid="u", session_id="s1")


@pytest.fixture(autouse=True)
def no_usage_limit(monkeypatch):
    monkeypatch.setattr(usage, "out", lambda now=None: None)


# ── When a task counts as stalled ───────────────────────────────


def test_a_running_task_quiet_past_the_limit_is_stalled():
    assert check(running()) == {"minutes": 20, "since": NOW - 20 * MINUTE}
    assert check(running(updated=NOW - 14 * MINUTE)) is None  # not long enough yet
    assert check(running(), minutes=0) is None  # 0 turns the watchdog off


@pytest.mark.parametrize("status", ["queued", "waiting", "done", "failed", "stopped"])
def test_only_running_tasks_can_stall(status):
    assert check(running(status=status, updated=NOW - 5 * 3600)) is None


def test_any_sign_of_life_counts_as_progress():
    pulse = Pulse(Clock(NOW - 2 * MINUTE))
    pulse.saw(7, AssistantMessage(content=[], model="m"))  # a message from its session
    assert check(running(), pulse=pulse) is None
    assert check(running(), store=Store(logged=NOW - 3 * MINUTE)) is None  # a log line
    assert check(running(updated=NOW - MINUTE)) is None  # a change in tasks.db
    assert check(running(), pulse=pulse, at=NOW + 14 * MINUTE)["minutes"] == 16  # quiet since the last of them


@pytest.mark.parametrize("why, fields, store, jobs", [
    ("waiting for your go-ahead", {}, Store(approvals=[{"id": 3}]), Jobs()),
    ("waiting for your answer", {"question": "Which DB?"}, Store(), Jobs()),
    ("waiting for your answer", {"wants": '["mac"]'}, Store(), Jobs()),
    ("a long job it started is still running", {}, Store(), Jobs("done", host.RUNNING)),
])
def test_waiting_on_the_user_or_a_long_job_is_not_a_stall(why, fields, store, jobs):
    task = running(**fields)
    assert stall.excuse(task, store, Pulse(Clock()), jobs, NOW) == why
    assert check(task, store=store, jobs=jobs) is None
    assert check(running(), jobs=Jobs("done", "failed, exit code 1")) is not None  # only one still running excuses


def test_waiting_for_the_usage_limit_is_not_a_stall(monkeypatch):
    pulse = Pulse(Clock(NOW - 20 * MINUTE))
    pulse.saw(7, limit("rejected", resets_at=int(NOW + 3600)))
    assert stall.excuse(running(), Store(), pulse, Jobs(), NOW) == "waiting for Claude's usage limit to reset"
    assert check(running(), pulse=pulse) is None
    assert check(running(), pulse=pulse, at=NOW + 2 * 3600) is not None  # it has reset, and still nothing
    pulse.saw(7, limit("allowed"))
    assert 7 not in pulse.limited

    monkeypatch.setattr(usage, "out", lambda now=None: ("seven_day", {"status": "rejected"}))  # used up for every model
    assert check(running()) is None


def test_a_command_is_given_its_own_time_limit_before_its_quiet_counts():
    clock = Clock(NOW - 20 * MINUTE)
    pulse = Pulse(clock)
    pulse.saw(7, called("h1", HOST_RUN, command="xcodebuild", minutes=10))
    assert stall.excuse(running(), Store(), pulse, Jobs(), NOW - 15 * MINUTE) == "running a command on the computer"
    assert check(running(), pulse=pulse, at=NOW - 20 * MINUTE + 10 * MINUTE + GRACE - 1, minutes=5) is None
    assert check(running(), pulse=pulse) == {"minutes": 20, "since": NOW - 20 * MINUTE}  # past its limit: stuck

    clock.at = NOW - 3 * MINUTE
    pulse.saw(7, called("w1", JOB_WAIT, id=4))
    assert pulse.busy(7, NOW) == "waiting for one of its jobs"
    pulse.saw(7, answered("w1"))  # it came back
    assert pulse.busy(7, NOW) == ""
    assert check(running(), pulse=pulse, at=NOW + 13 * MINUTE)["minutes"] == 16


def test_how_long_each_tool_call_may_take():
    assert stall.lasts(ToolUseBlock(id="b", name="Bash", input={"command": "pytest"})) == 120
    assert stall.lasts(ToolUseBlock(id="b", name="Bash", input={"command": "make", "timeout": 300000})) == 300
    assert stall.lasts(ToolUseBlock(id="b", name="Bash", input={"command": "x", "timeout": 10 ** 9})) == 600
    assert stall.lasts(ToolUseBlock(id="b", name="Bash", input={"command": "x", "run_in_background": True})) == 0
    assert stall.lasts(ToolUseBlock(id="h", name=HOST_RUN, input={"command": "x"})) == 600
    assert stall.lasts(ToolUseBlock(id="h", name=HOST_RUN, input={"command": "x", "minutes": 2})) == 120
    assert stall.lasts(ToolUseBlock(id="w", name=JOB_WAIT, input={"id": 1, "minutes": "lots"})) == 600
    assert stall.lasts(ToolUseBlock(id="f", name="WebFetch", input={"url": "x"})) == 0  # a hang there is a stall


# ── The worker and the runner ───────────────────────────────────


def test_the_worker_reports_every_message_as_a_sign_of_life(monkeypatch):
    class Pulsing(Recorder):
        def __init__(self) -> None:
            super().__init__()
            self.pulses: list = []

        def alive(self, task_id: int, msg: object) -> None:
            self.pulses.append(msg)

    messages = [called("b1", "Bash", command="pytest -q"), answered("b1"), result("Done.", 0.1)]
    reporter = Pulsing()
    assert worker_run(monkeypatch, FakeClient(list(messages)), reporter).result == "Done."
    assert reporter.pulses == messages


def test_the_runner_shows_a_stall_in_its_views_and_never_stops_the_task(env, monkeypatch):
    gate = asyncio.Event()

    async def run(task: dict, prompt: str, reporter) -> Outcome:
        await gate.wait()  # stuck
        return Outcome("Done at last.", "", 0.0)

    monkeypatch.setattr(runner, "run", run)

    async def main():
        r = runner.Runner()
        task = r.start("research", "retry logic", "Find out")
        await until(lambda: r.store.get(task["id"])["status"] == "running")
        later = time.time() + 20 * MINUTE
        assert r.view(r.store.get(task["id"]))["stalled"] is None  # just started
        stalled = r.stalls(later)
        assert [t["id"] for t in stalled] == [task["id"]] and stalled[0]["stalled"]["minutes"] >= 19
        assert r.view(r.store.get(task["id"]), later)["stalled"] == stalled[0]["stalled"]
        brief = task_tools._brief(stalled[0])
        assert "No sign of progress for" in brief["stalled"] and "task_reply" in brief["stalled"]
        assert "task_stop" in brief["stalled"]

        r.pulse.clock = lambda: later - MINUTE
        r.alive(task["id"], None)  # it moved on
        assert r.stalls(later) == []
        assert r.store.get(task["id"])["status"] == "running"  # still left to run
        gate.set()
        await settle(r)
        return r, task["id"]

    r, task_id = asyncio.run(main())
    assert r.store.get(task_id)["status"] == "done"
    assert r.stalls(time.time() + 3600) == [] and task_id not in r.pulse.last  # its run is over: forgotten
    assert "stalled" not in task_tools._brief(r.view(r.store.get(task_id)))


# ── Said once, as a notice ──────────────────────────────────────


class Tasks:
    """The store's recent tasks, none of them to flag otherwise."""

    def recent(self) -> list[dict]:
        return []


def test_a_stall_is_said_once_and_again_only_after_it_moved_on(tmp_path):
    stuck = [running(stalled={"minutes": 20, "since": NOW - 20 * MINUTE})]
    phone: list[tuple] = []

    async def push(title, text, url, tag, **extra):
        phone.append((title, text, url, tag))

    events: list[dict] = []
    notify = Notifier()
    told = notices.Notices({"tasks": notices.task_notices(Tasks(), stalls=lambda now: list(stuck))}, notify,
                           events.append, state_file=tmp_path / "notices.json", quiet_hours="", push=push,
                           clock=lambda: AFTERNOON)

    given = asyncio.run(told.check())
    text = "Task 7, retry logic, seems stuck: no progress for 20 minutes. Nudge it or stop it?"
    assert [(n.text, n.task_id) for n in given] == [(text, 7)]
    assert notify.sent == [("JARVIS · tasks", text)]
    assert phone == [("Tasks", text, "/?task=7", "task-7")]
    assert events[0]["tasks"] == [7] and "Shall I nudge it with a message, or stop it?" in events[0]["text"]

    assert asyncio.run(told.check()) == []  # still stuck: not said again
    stuck[0]["stalled"]["minutes"] = 35
    assert asyncio.run(told.check()) == []
    stuck.clear()  # it moved on, or ended
    assert asyncio.run(told.check()) == []
    stuck.append(running(stalled={"minutes": 16, "since": NOW - 16 * MINUTE}))  # and stalled afresh
    assert [n.task_id for n in asyncio.run(told.check())] == [7]
    assert len(notify.sent) == 2


def test_notices_take_the_runners_stalls():
    told = notices.make(Tasks(), Notifier(), stalls=lambda now: [running(stalled={"minutes": 30, "since": NOW})])
    assert [n.key for n in asyncio.run(told.checks["tasks"](AFTERNOON))] == ["task:7:stalled"]
    assert asyncio.run(notices.task_notices(Tasks())(AFTERNOON)) == []  # without the runner's, nothing
