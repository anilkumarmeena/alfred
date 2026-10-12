"""Tests for jarvis.tasks.notices: JARVIS's own news said once, pushed, held in quiet hours, and safe when checks fail."""

import asyncio
import importlib
import json
import logging
from datetime import datetime, time

import pytest

from conftest import use_profile
from jarvis.core import config, profile
from jarvis.tasks import hold, notices
from jarvis.tasks.hold import in_quiet_hours, parse_quiet_hours
from jarvis.tasks.notices import Notice, Notices

TZ = profile.tz()
AFTERNOON = datetime(2026, 9, 26, 15, 0, tzinfo=TZ)


class Notifier:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def __call__(self, title: str, text: str) -> None:
        self.sent.append((title, text))


def check(*found: Notice):
    calls = []

    async def fn(now: datetime) -> list[Notice]:
        calls.append(now)
        return list(found)

    fn.calls = calls
    return fn


def make(tmp_path, checks, at=AFTERNOON, **kw) -> tuple[Notices, Notifier]:
    notify = Notifier()
    kw.setdefault("quiet_hours", "23:00-08:00")
    return Notices(checks, notify, state_file=tmp_path / "notices.json", clock=lambda: at, **kw), notify


WAITING = Notice("task:3:waiting:1", "tasks", "Task 3, retry logic, is still waiting for your answer: which DB?",
                 task_id=3)
LIMIT = Notice("usage:five_hour:1790000000:close", "usage", "JARVIS has used 90% of Claude's five-hour limit.")
STUCK = Notice("task:7:stalled", "tasks", "Task 7 seems stuck.", task_id=7, ongoing=True)


# ── Config and the loop ─────────────────────────────────────────


def test_config_defaults_and_overrides(monkeypatch):
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: None)  # the real .env mustn't leak in
    for var in ("JARVIS_NOTICES", "JARVIS_QUIET_HOURS"):
        monkeypatch.delenv(var, raising=False)
    try:
        importlib.reload(config)
        assert config.NOTICES_ENABLED is True
        assert config.QUIET_HOURS == "23:00-08:00"
        monkeypatch.setenv("JARVIS_NOTICES", "off")
        monkeypatch.setenv("JARVIS_QUIET_HOURS", "")
        importlib.reload(config)
        assert config.NOTICES_ENABLED is False and config.QUIET_HOURS == ""
    finally:
        monkeypatch.undo()
        importlib.reload(config)


class Stop(BaseException):
    pass


def test_it_checks_every_interval_and_survives_a_failed_round(tmp_path):
    waits: list[float] = []

    async def sleep(seconds: float) -> None:
        waits.append(seconds)
        if len(waits) == 3:
            raise Stop

    n, _ = make(tmp_path, {}, minutes=5, sleep=sleep)
    rounds = []

    async def once():
        rounds.append(1)
        if len(rounds) == 1:
            raise RuntimeError("boom")
        return []

    n.check = once
    with pytest.raises(Stop):
        asyncio.run(n.run())
    assert len(rounds) == 3 and waits == [300, 300, 300]


# ── Giving notices ──────────────────────────────────────────────


def test_a_new_notice_is_notified_and_announced(tmp_path):
    events = []
    n, notify = make(tmp_path, {"usage": check(LIMIT), "tasks": check(WAITING)}, announce=events.append)
    assert asyncio.run(n.check()) == [LIMIT, WAITING]
    assert notify.sent == [("JARVIS · usage", LIMIT.text), ("JARVIS · tasks", WAITING.text)]
    assert events == [{"type": "alert", "tasks": [3], "text": f"Heads up. {LIMIT.text} {WAITING.text}",
                       "urgency": "soon", "pushed": False}]


def test_the_same_notice_is_never_given_twice_even_across_restarts(tmp_path):
    n, notify = make(tmp_path, {"tasks": check(WAITING)})
    asyncio.run(n.check())
    asyncio.run(n.check())
    assert len(notify.sent) == 1

    later = Notice("task:4:waiting:2", "tasks", "Task 4 is still waiting for your answer: which branch?", task_id=4)
    again, notify2 = make(tmp_path, {"tasks": check(WAITING, later)})  # a fresh runner, same state file
    asyncio.run(again.check())
    assert notify2.sent == [("JARVIS · tasks", later.text)]


def test_an_ongoing_notice_is_given_once_until_it_clears(tmp_path):
    found = [STUCK]

    async def checks(now):
        return list(found)

    n, notify = make(tmp_path, {"tasks": checks})
    assert asyncio.run(n.check()) == [STUCK]
    assert asyncio.run(n.check()) == []  # still stuck: not said again
    found.clear()
    assert asyncio.run(n.check()) == []  # moving again
    found.append(STUCK)
    assert asyncio.run(n.check()) == [STUCK]  # stuck anew: said again
    assert len(notify.sent) == 2
    assert json.loads((tmp_path / "notices.json").read_text())["ongoing"] == {"tasks": [STUCK.key]}


def test_a_failed_check_doesnt_count_as_cleared(tmp_path):
    answers = [[STUCK], RuntimeError("database locked"), [STUCK]]

    async def checks(now):
        found = answers.pop(0)
        if isinstance(found, Exception):
            raise found
        return found

    n, notify = make(tmp_path, {"tasks": checks})
    for _ in range(3):
        asyncio.run(n.check())
    assert len(notify.sent) == 1


def test_at_most_a_few_per_round_and_the_rest_wait(tmp_path):
    many = [Notice(f"task:{i}:waiting:1", "tasks", f"Task {i} is waiting.", task_id=i) for i in range(7)]
    n, notify = make(tmp_path, {"tasks": check(*many)})
    asyncio.run(n.check())
    assert len(notify.sent) == notices.MAX_NOTICES
    asyncio.run(n.check())
    assert len(notify.sent) == 7


def test_a_failing_notification_doesnt_stop_the_round(tmp_path):
    async def broken(title, text):
        raise RuntimeError("osascript failed")

    n = Notices({"tasks": check(WAITING)}, broken, state_file=tmp_path / "n.json", clock=lambda: AFTERNOON)
    assert asyncio.run(n.check()) == [WAITING]


def test_a_failing_check_doesnt_stop_the_round_or_leak_its_message(tmp_path, caplog, monkeypatch):
    async def broken(now):
        raise RuntimeError("token=s3cr3t-value")

    async def hangs(now):
        await asyncio.sleep(3600)

    monkeypatch.setattr(notices, "CHECK_TIMEOUT", 0.05)
    n, notify = make(tmp_path, {"broken": broken, "usage": hangs, "tasks": check(WAITING)})
    with caplog.at_level(logging.INFO, logger="jarvis.tasks.notices"):
        asyncio.run(n.check())
    assert notify.sent == [("JARVIS · tasks", WAITING.text)]
    assert "s3cr3t" not in caplog.text
    assert "broken failed" in caplog.text and "usage failed" in caplog.text


def test_an_unreadable_state_file_starts_afresh(tmp_path):
    (tmp_path / "notices.json").write_text("{not json")
    n, notify = make(tmp_path, {"tasks": check(WAITING)})
    asyncio.run(n.check())
    assert len(notify.sent) == 1


def test_what_was_already_said_before_is_kept(tmp_path, monkeypatch):
    use_profile(monkeypatch, tmp_path)
    data = profile.current().data
    data.mkdir(parents=True, exist_ok=True)
    (data / "heartbeat.json").write_text(json.dumps({"started": ["email"], "seen": {WAITING.key: 1.0}}))
    n = notices.make(None, Notifier(), clock=lambda: AFTERNOON)
    n.checks = {"tasks": check(WAITING)}
    assert asyncio.run(n.check()) == []
    assert not (data / "heartbeat.json").exists() and profile.current().notices_file.exists()


# ── Push to the phone ───────────────────────────────────────────


class Pusher:
    def __init__(self, fail: bool = False) -> None:
        self.sent: list[tuple[str, str, str, str]] = []
        self.fail = fail

    async def __call__(self, title: str, body: str, url: str, tag: str) -> None:
        self.sent.append((title, body, url, tag))
        if self.fail:
            raise RuntimeError("push service down")


def test_notices_are_pushed_alongside_the_desktop_notification(tmp_path):
    phone = Pusher()
    n, notify = make(tmp_path, {"usage": check(LIMIT), "tasks": check(WAITING)}, push=phone)
    asyncio.run(n.check())
    assert len(notify.sent) == 2
    (title, body, url, tag), task_push = phone.sent
    assert (title, body, url) == ("Claude usage", LIMIT.text, "/?alert=usage")
    assert tag.startswith("alert-") and "five_hour" not in tag  # stable per notice, but reveals nothing about it
    assert task_push == ("Tasks", WAITING.text, "/?task=3", "task-3")  # opens the app on the task


def test_a_failing_push_doesnt_stop_the_round_or_repeat(tmp_path):
    phone = Pusher(fail=True)
    n, notify = make(tmp_path, {"tasks": check(WAITING)}, push=phone)
    assert asyncio.run(n.check()) == [WAITING]
    assert notify.sent == [("JARVIS · tasks", WAITING.text)]
    asyncio.run(n.check())
    assert len(phone.sent) == 1


# ── Quiet hours and do not disturb ──────────────────────────────


def test_parse_quiet_hours():
    assert parse_quiet_hours("23:00-08:00") == (time(23), time(8))
    assert parse_quiet_hours(" 8:30 - 9:00 ") == (time(8, 30), time(9))
    assert parse_quiet_hours("") is None
    assert parse_quiet_hours("10:00-10:00") is None
    assert parse_quiet_hours("late") is None


@pytest.mark.parametrize("hour, minute, quiet", [(23, 0, True), (2, 0, True), (7, 59, True), (8, 0, False),
                                                 (15, 0, False), (22, 59, False)])
def test_overnight_quiet_hours(hour, minute, quiet):
    assert in_quiet_hours(datetime(2026, 9, 26, hour, minute, tzinfo=TZ), parse_quiet_hours("23:00-08:00")) is quiet


def test_daytime_quiet_hours():
    window = parse_quiet_hours("13:00-14:00")
    assert in_quiet_hours(datetime(2026, 9, 26, 13, 30, tzinfo=TZ), window)
    assert not in_quiet_hours(datetime(2026, 9, 26, 14, 0, tzinfo=TZ), window)


def test_quiet_hours_hold_notices_till_morning(tmp_path):
    tasks, phone = check(WAITING), Pusher()
    n, notify = make(tmp_path, {"tasks": tasks}, at=datetime(2026, 9, 26, 23, 30, tzinfo=TZ), push=phone)
    assert asyncio.run(n.check()) == []
    assert notify.sent == [] and phone.sent == [] and tasks.calls == []  # nothing is even checked

    n.clock = lambda: datetime(2026, 9, 27, 8, 5, tzinfo=TZ)
    asyncio.run(n.check())
    asyncio.run(n.check())
    assert notify.sent == [("JARVIS · tasks", WAITING.text)] and len(phone.sent) == 1


def test_do_not_disturb_holds_notices_like_quiet_hours(tmp_path, monkeypatch):
    use_profile(monkeypatch, tmp_path)
    tasks = check(WAITING)
    hold.start(until="16:00", now=AFTERNOON)
    n, notify = make(tmp_path, {"tasks": tasks})
    assert asyncio.run(n.check()) == [] and tasks.calls == [] and notify.sent == []
    n.clock = lambda: datetime(2026, 9, 26, 16, 1, tzinfo=TZ)
    assert asyncio.run(n.check()) == [WAITING]


# ── The checks ──────────────────────────────────────────────────


def test_task_notices():
    now = AFTERNOON.timestamp()

    class Store:
        def recent(self):
            return [
                {"id": 1, "title": "Retry logic", "status": "waiting", "seen": 0, "updated": now - 3600,
                 "question": "Which DB?", "error": None},
                {"id": 2, "title": "Fresh question", "status": "waiting", "seen": 0, "updated": now - 60,
                 "question": "Now?", "error": None},  # the runner has only just announced it
                {"id": 3, "title": "Auth", "status": "stopped", "seen": 1, "updated": now - 60, "question": None,
                 "error": "JARVIS restarted while it was running. Reply to carry on."},
                {"id": 4, "title": "Stopped by you", "status": "stopped", "seen": 1, "updated": now, "question": None,
                 "error": None},
                {"id": 5, "title": "Done", "status": "done", "seen": 0, "updated": now - 3600, "question": None,
                 "error": None},
            ]

    found = asyncio.run(notices.task_notices(Store(), minutes=15)(AFTERNOON))
    assert [(n.task_id, n.text) for n in found] == [
        (1, "Task 1, Retry logic, is still waiting for your answer: Which DB?"),
        (3, "Task 3, Auth, stopped. JARVIS restarted while it was running. Reply to carry on."),
    ]


def test_usage_warnings_are_said_once_per_window(tmp_path, monkeypatch):
    from claude_agent_sdk import RateLimitInfo

    from jarvis.brain import usage

    use_profile(monkeypatch, tmp_path)
    monkeypatch.setattr(usage, "USAGE_FILE", tmp_path / "usage.json")
    stamp = AFTERNOON.timestamp()
    usage.record(RateLimitInfo(status="allowed_warning", resets_at=int(stamp + 3600), rate_limit_type="five_hour",
                               utilization=0.9), now=stamp)
    n, notify = make(tmp_path, {"usage": notices.usage_notices})
    asyncio.run(n.check())
    asyncio.run(n.check())
    assert notify.sent == [("JARVIS · usage", "JARVIS has used 90% of Claude's five-hour limit; it resets at 4 PM.")]


def test_the_runner_gets_only_jarvis_s_own_checks():
    class Store:
        def recent(self):
            return []

    n = notices.make(Store(), Notifier(), state_file=None)
    assert list(n.checks) == ["tasks", "usage"]
    assert notices.make(Store(), Notifier(), push=Pusher()).push is not None
