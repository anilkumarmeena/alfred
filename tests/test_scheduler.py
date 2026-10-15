"""Tests for jarvis.tasks.schedules and scheduler: due runs, catch-up, watches, announcing, job memory, voice tools."""

import asyncio
import json
from datetime import datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from test_approvals import drained, events, pushes  # noqa: F401  (fixture)
from test_in_place import env  # noqa: F401  (fixture)
from test_nudges import fake_run, finishes, settle

from conftest import ACCOUNTS, use_profile, use_task_cache
from jarvis import tools
from jarvis.brain import retry
from jarvis.core import config, database, profile
from jarvis.core.database import Store
from jarvis.integrations import accounts
from jarvis.state import settings
from jarvis.tasks import runner, schedules, scheduler, worker
from jarvis.tasks.scheduler import Found, Reading, Scheduler

# A Tuesday, 09:15
NOW = datetime(2026, 10, 6, 9, 15, tzinfo=profile.tz())


@pytest.fixture
def db(tmp_path, monkeypatch):
    """A profile of its own, with the shared Google accounts, so tool watches can call their reading tools."""
    use_profile(monkeypatch, tmp_path)
    with accounts.change() as found:
        found.accounts, found.default = list(ACCOUNTS.accounts), ACCOUNTS.default
    return tmp_path / "tasks.db"


@pytest.fixture
def flags(monkeypatch):
    """Fake sdk.ask_flag: answers per field from `flags.answers`, then None (failed); records `flags.asked`."""
    fake = SimpleNamespace(answers={}, asked=[])

    async def ask_flag(prompt, field, system_prompt, model, **options):
        fake.asked.append((field, prompt))
        queued = fake.answers.get(field, [])
        return queued.pop(0) if queued else None

    monkeypatch.setattr("jarvis.brain.sdk.ask_flag", ask_flag)
    return fake


def at(hours: float = 0, minutes: float = 0, days: float = 0) -> datetime:
    return NOW + timedelta(days=days, hours=hours, minutes=minutes)


class FakeStore:
    def __init__(self) -> None:
        self.tasks: dict[int, dict] = {}

    def get(self, task_id: int) -> dict | None:
        return self.tasks.get(task_id)


class FakeRunner:
    """Records what the scheduler starts; each task stays running until a test says otherwise."""

    def __init__(self) -> None:
        self.store = FakeStore()
        self.started: list[dict] = []

    def start(self, kind, title, instruction, project=None, job=None, hidden=False) -> dict:
        task = {"id": len(self.started) + 1, "kind": kind, "title": title, "instruction": instruction,
                "project": project, "job": job, "hidden": hidden, "status": "running"}
        self.started.append(task)
        self.store.tasks[task["id"]] = task
        return task


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def make(probes: dict | None = None, quiet: str | None = "", now: datetime = NOW):
    said: dict[str, list] = {"notified": [], "events": [], "pushed": []}

    async def notify(title, text):
        said["notified"].append((title, text))

    async def push(title, text, url, tag):
        said["pushed"].append((title, text, url, tag))

    clock = Clock(now)
    s = Scheduler(FakeRunner(), notify, said["events"].append, push, quiet_hours=quiet, probes=probes, clock=clock)
    s.pruned = float("inf")  # no tidying unless a test asks
    return s, said, clock


def tick(s: Scheduler) -> list[int]:
    async def main():
        ran = await s.tick()
        await s.drain()
        return ran

    return asyncio.run(main())


# ── Times ───────────────────────────────────────────────────────


def test_times_are_local_and_a_bare_time_is_its_next_occurrence():
    assert schedules.parse_time("17:00", NOW) == datetime(2026, 10, 6, 17, 0, tzinfo=profile.tz())
    assert schedules.parse_time("9:00", NOW) == datetime(2026, 10, 7, 9, 0, tzinfo=profile.tz())  # past today: tomorrow
    assert schedules.parse_time("2026-10-09T08:30", NOW) == datetime(2026, 10, 9, 8, 30, tzinfo=profile.tz())
    with pytest.raises(ValueError, match="isn't a time"):
        schedules.parse_time("five o'clock", NOW)


def test_days_of_the_week():
    assert schedules.parse_days("weekdays") == "mon,tue,wed,thu,fri"
    assert schedules.parse_days("Friday, mon") == "mon,fri"
    with pytest.raises(ValueError, match="repeat"):
        schedules.parse_days("fortnightly")
    friday_evening = datetime(2026, 10, 9, 18, 0, tzinfo=profile.tz())
    assert schedules.next_on_days("09:00", "mon,tue,wed,thu,fri", friday_evening) == datetime(2026, 10, 12, 9, 0, tzinfo=profile.tz())
    assert schedules.next_on_days("09:00", "mon,tue,wed,thu,fri", at(minutes=-30)) == at(minutes=-15)


def test_jobs_say_when_they_run(db):
    brief = schedules.add("task", "Morning brief", "Brief me.", when="09:00", repeat="weekdays", now=NOW)
    assert schedules.describe(brief, NOW) == "every weekday at 09:00"
    assert brief["next_run"] == datetime(2026, 10, 7, 9, 0, tzinfo=profile.tz()).timestamp()  # today's has passed
    once = schedules.add("reminder", "Call Priya", "Call Priya.", when="17:00", now=NOW)
    assert schedules.describe(once, NOW) == "once, at 17:00"
    watch = schedules.add("watch", "Deploy", "v2 is live", probe="url", target="https://app.example/v", now=NOW)
    assert schedules.describe(watch, NOW) == "every 5 minutes, until tomorrow 09:15"
    hourly = schedules.add("reminder", "Stretch", "Stretch.", every_minutes=60, until="18:00", now=NOW)
    assert schedules.describe(hourly, NOW) == "every hour, until 18:00"


# ── Adding, changing, cancelling ────────────────────────────────


def test_a_watch_looks_now_every_few_minutes_for_a_day_unless_told(db):
    job = schedules.add("watch", "Priya's reply", "Priya replied about the budget", now=NOW)
    assert (job["probe"], job["task_kind"], job["every"]) == ("task", "research", 15)
    assert job["next_run"] == NOW.timestamp() and job["until"] == at(hours=24).timestamp()
    cheap = schedules.add("watch", "Reply", "Priya replied", probe="tool", target="gmail_search",
                          arguments={"query": "from:priya@acme.example"}, now=NOW)
    assert cheap["every"] == 10 and cheap["task_kind"] is None


@pytest.mark.parametrize("kwargs, error", [
    ({"kind": "reminder", "text": "x"}, "Say when"),
    ({"kind": "reminder", "text": " "}, "text is empty"),
    ({"kind": "nag", "text": "x", "when": "17:00"}, "kind must be"),
    ({"kind": "reminder", "text": "x", "when": "2026-10-05T17:00"}, "already passed"),
    ({"kind": "reminder", "text": "x", "when": "17:00", "until": "18:00"}, "only for repeating"),
    ({"kind": "task", "text": "x", "repeat": "daily"}, "time of day"),
    ({"kind": "task", "text": "x", "every_minutes": 10}, "Every 30 minutes"),
    ({"kind": "task", "text": "x", "when": "17:00", "task_kind": "code_change"}, "needs a project"),
    ({"kind": "watch", "text": "x", "repeat": "daily", "when": "09:00"}, "every so many minutes"),
    ({"kind": "watch", "text": "x", "every_minutes": 2}, "Every 10 minutes"),
    ({"kind": "watch", "text": "x", "until": "2026-10-20T09:00"}, "at most 7 days"),
    ({"kind": "watch", "text": "x", "probe": "url", "target": "app.example"}, "http"),
    ({"kind": "watch", "text": "x", "probe": "tool"}, "the reading tool it calls"),
    ({"kind": "watch", "text": "x", "probe": "tool", "target": "gmail_modify"}, "isn't a reading tool"),
    ({"kind": "watch", "text": "x", "probe": "tool", "target": "gmail_draft_send"}, "isn't a reading tool"),
    ({"kind": "watch", "text": "x", "probe": "tool", "target": "mcp__acme__look"}, "switched on"),
    ({"kind": "watch", "text": "x", "probe": "tool", "target": "gmail_search"}, "needs arguments query"),
    ({"kind": "watch", "text": "x", "probe": "tool", "target": "gmail_search", "arguments": {"query": "x", "limit": 3}},
     "takes no limit"),
    ({"kind": "watch", "text": "x", "probe": "tool", "target": "gmail_search", "arguments": "from:x"}, "as an object"),
    ({"kind": "reminder", "text": "x", "in_minutes": 5, "arguments": {"query": "x"}}, "only for tool watches"),
    ({"kind": "watch", "text": "x", "task_kind": "code_change", "project": "p"}, "research, code_question"),
])
def test_bad_jobs_are_refused_saying_why(db, kwargs, error):
    kwargs = {"title": "t"} | kwargs
    with pytest.raises(ValueError, match=error):
        schedules.add(now=NOW, **kwargs)
    assert schedules.items() == []


def test_watches_checked_by_tasks_are_limited(db):
    for i in range(schedules.MAX_TASK_WATCHES):
        schedules.add("watch", f"w{i}", "x", now=NOW)
    with pytest.raises(ValueError, match="url or tool probe"):
        schedules.add("watch", "one more", "x", now=NOW)
    schedules.add("watch", "cheap", "x", probe="url", target="https://a.example", now=NOW)


def test_change_retimes_pauses_resumes_and_keeps_the_rest(db):
    job = schedules.add("task", "Morning brief", "Brief me.", when="09:00", repeat="weekdays", now=NOW)
    moved = schedules.change(job["id"], when="08:30", repeat="daily", now=NOW)
    assert (moved["at"], moved["days"], moved["text"]) == ("08:30", "mon,tue,wed,thu,fri,sat,sun", "Brief me.")
    assert moved["next_run"] == datetime(2026, 10, 7, 8, 30, tzinfo=profile.tz()).timestamp()
    assert schedules.change(job["id"], paused=True, now=NOW)["status"] == schedules.PAUSED
    # Resumed days later: the times missed meanwhile are skipped, not caught up
    resumed = schedules.change(job["id"], paused=False, now=at(days=3))
    assert resumed["status"] == schedules.ACTIVE
    assert resumed["next_run"] == datetime(2026, 10, 10, 8, 30, tzinfo=profile.tz()).timestamp()
    with pytest.raises(ValueError, match="Nothing to change"):
        schedules.change(job["id"], now=NOW)

    watch = schedules.add("watch", "Deploy", "live", probe="url", target="https://a.example", until="18:00", now=NOW)
    faster = schedules.change(watch["id"], every_minutes=2, now=NOW)
    assert faster["every"] == 2 and faster["until"] == watch["until"]  # a new timing keeps its end
    with pytest.raises(ValueError, match="only for url and tool watches"):
        schedules.change(job["id"], target="https://b.example")


def test_cancelled_jobs_stay_cancelled(db):
    job = schedules.add("reminder", "Call", "Call Priya.", in_minutes=30, now=NOW)
    assert schedules.cancel(job["id"])["status"] == schedules.CANCELLED
    with pytest.raises(ValueError, match="already cancelled"):
        schedules.cancel(job["id"])
    with pytest.raises(ValueError, match="schedule a new one"):
        schedules.change(job["id"], text="x")
    assert schedules.items() == [] and [j["id"] for j in schedules.items(7)] == [job["id"]]


# ── Running when due ────────────────────────────────────────────


def test_a_reminder_is_said_every_way_once(db):
    job = schedules.add("reminder", "Call Priya", "Call Priya about the budget", when="17:00", now=NOW)
    s, said, clock = make()
    assert tick(s) == []
    clock.now = at(hours=7, minutes=45, )  # 17:00
    assert tick(s) == [job["id"]]
    assert said["notified"] == [("JARVIS · Reminder", "Call Priya about the budget.")]
    assert said["events"] == [{"type": "alert", "text": "Reminder: Call Priya about the budget.", "tasks": [],
                               "urgency": "soon", "pushed": True}]
    assert said["pushed"] == [("Reminder", "Call Priya about the budget.", "/?alert=schedule", f"schedule-{job['id']}")]
    assert schedules.get(job["id"])["status"] == schedules.DONE
    assert tick(s) == []


def test_a_late_reminder_says_when_it_was_due(db):
    schedules.add("reminder", "Call Priya", "Call Priya", when="17:00", now=NOW)
    s, said, _ = make(now=at(hours=9))  # JARVIS was down at 17:00, back at 18:15
    tick(s)
    assert said["notified"] == [("JARVIS · Reminder", "Call Priya. It was due at 17:00.")]


def test_a_scheduled_task_starts_a_background_task_with_its_brief(db):
    job = schedules.add("task", "Morning brief", "Summarise my day: calendar, important mail.", when="09:00",
                        repeat="weekdays", now=at(minutes=-30))
    s, said, _ = make()
    tick(s)
    [task] = s.runner.started
    assert (task["kind"], task["title"], task["job"], task["hidden"]) == ("research", "Morning brief", job["id"], False)
    assert "Summarise my day" in task["instruction"] and "always give the full report" in task["instruction"]
    assert "stays silent" not in task["instruction"]  # a brief isn't offered silence
    after = schedules.get(job["id"])
    assert (after["runs"], after["last_task"]) == (1, 1)
    assert after["next_run"] == datetime(2026, 10, 7, 9, 0, tzinfo=profile.tz()).timestamp()
    assert said["notified"] == []  # the task announces itself when it's done


def test_missed_days_run_once_if_recent_and_never_pile_up(db):
    job = schedules.add("task", "Morning brief", "Brief me.", when="09:00", repeat="daily", now=at(days=-5))
    s, _, _ = make()  # back at 09:15 after five missed runs: the latest is 15 minutes old
    tick(s)
    assert len(s.runner.started) == 1
    assert schedules.get(job["id"])["next_run"] == datetime(2026, 10, 7, 9, 0, tzinfo=profile.tz()).timestamp()

    late = schedules.add("task", "Evening brief", "Brief me.", when="06:00", repeat="daily", now=at(days=-1))
    tick(s)  # 06:00 is past CATCH_UP_HOURS: skipped, said so, waits for tomorrow
    assert len(s.runner.started) == 1
    after = schedules.get(late["id"])
    assert "Skipped its run due" in after["last_result"] and after["runs"] == 0
    assert after["next_run"] == datetime(2026, 10, 7, 6, 0, tzinfo=profile.tz()).timestamp()


def test_a_job_repeating_by_the_clock_runs_once_however_much_it_missed(db):
    job = schedules.add("reminder", "Stretch", "Stretch.", every_minutes=30, now=at(hours=-6))
    s, said, _ = make()
    tick(s)
    assert len(said["notified"]) == 1
    assert schedules.get(job["id"])["next_run"] == at(minutes=30).timestamp()


def test_a_run_still_going_makes_the_next_one_skip(db):
    job = schedules.add("task", "Digest", "Digest.", every_minutes=30, now=at(minutes=-31))
    s, _, clock = make()
    tick(s)
    clock.now = at(minutes=30)
    tick(s)  # the first is still running
    assert len(s.runner.started) == 1
    assert schedules.get(job["id"])["last_result"] == "Skipped a run: the previous one was still going."
    s.runner.store.tasks[1]["status"] = "done"
    clock.now = at(minutes=60)
    tick(s)
    assert len(s.runner.started) == 2


def test_a_job_that_cant_start_waits_for_its_next_time_and_says_so_once(db):
    job = schedules.add("task", "Digest", "Digest.", every_minutes=30, now=at(minutes=-31))
    s, said, clock = make()

    def refuse(*args, **kwargs):
        raise ValueError("There's no project called video-agent.")

    s.runner.start = refuse
    for minutes in (0, 1, 30):
        clock.now = at(minutes=minutes)
        tick(s)
    after = schedules.get(job["id"])
    assert (after["fails"], after["next_run"]) == (2, at(minutes=60).timestamp())
    assert said["notified"] == [("JARVIS · Digest", "JARVIS couldn't run \"Digest\": There's no project called video-agent.")]


def test_paused_jobs_dont_run_and_ended_ones_stop(db):
    paused = schedules.add("reminder", "Stretch", "Stretch.", every_minutes=30, now=at(hours=-1))
    schedules.change(paused["id"], paused=True, now=at(hours=-1))
    ending = schedules.add("reminder", "Water", "Drink water.", every_minutes=30, until="09:00", now=at(hours=-2))
    s, said, _ = make()
    tick(s)
    assert said["notified"] == []
    assert schedules.get(ending["id"])["status"] == schedules.DONE  # past its end: over, quietly


# ── Watches ─────────────────────────────────────────────────────


def probe_that(*results):
    """A probe returning results in turn: a Found, or an exception to raise."""
    calls = []

    async def probe(job):
        calls.append(job)
        r = results[len(calls) - 1]
        if isinstance(r, Exception):
            raise r
        return r

    return probe, calls


def test_a_watch_is_silent_until_met_then_says_so_once_and_stops(db):
    job = schedules.add("watch", "Deploy live", "v2 is live", probe="url", target="https://app.example", now=NOW)
    probe, calls = probe_that(Found(False, "It answers 502."), Found(True, "https://app.example is up."))
    s, said, clock = make({"url": probe})
    tick(s)
    assert said == {"notified": [], "events": [], "pushed": []}
    after = schedules.get(job["id"])
    assert (after["last_result"], after["next_run"], after["runs"]) == ("It answers 502.", at(minutes=5).timestamp(), 1)
    clock.now = at(minutes=2)
    tick(s)  # not due yet
    clock.now = at(minutes=5)
    tick(s)
    assert len(calls) == 2
    assert said["notified"] == [("JARVIS · Deploy live", "https://app.example is up.")]
    assert said["events"][0]["text"] == "Deploy live: https://app.example is up."
    assert schedules.get(job["id"])["status"] == schedules.DONE
    clock.now = at(minutes=10)
    tick(s)
    assert len(calls) == 2


def test_failing_checks_are_said_once_and_the_watch_keeps_going(db):
    job = schedules.add("watch", "Reply", "Priya replied", probe="tool", target="gmail_search",
                        arguments={"query": "from:priya"}, now=NOW)
    probe, _ = probe_that(*[scheduler.ProbeFailed("every account failed")] * 4)
    s, said, clock = make({"tool": probe})
    for i in range(4):
        clock.now = at(minutes=10 * i)
        tick(s)
    assert [text for _, text in said["notified"]] == [
        "JARVIS couldn't check \"Reply\" 3 times running: every account failed. It keeps trying."]
    assert schedules.get(job["id"])["status"] == schedules.ACTIVE and schedules.get(job["id"])["fails"] == 4


def test_a_watch_that_never_sees_it_says_so_when_it_ends(db):
    job = schedules.add("watch", "Deploy live", "v2 is live", probe="url", target="https://a.example", until="10:00",
                        now=NOW)
    probe, _ = probe_that(*[Found(False, "Not yet.")] * 20)
    s, said, clock = make({"url": probe})
    clock.now = at(hours=1)
    tick(s)
    assert schedules.get(job["id"])["status"] == schedules.EXPIRED
    assert said["notified"] == [("JARVIS · Deploy live", "Stopped watching for \"Deploy live\": it hadn't happened by 10:00.")]


def test_watches_wait_out_quiet_hours(db):
    night = datetime(2026, 10, 6, 23, 30, tzinfo=profile.tz())
    job = schedules.add("watch", "Deploy", "live", probe="url", target="https://a.example", now=night)
    probe, calls = probe_that(Found(False, "Not yet."))
    s, _, clock = make({"url": probe}, quiet="23:00-08:00", now=night)
    tick(s)
    assert calls == [] and schedules.get(job["id"])["runs"] == 0
    clock.now = datetime(2026, 10, 7, 8, 0, tzinfo=profile.tz())
    tick(s)
    assert len(calls) == 1


def test_watches_follow_the_users_quiet_hours_as_they_change(db, monkeypatch):
    monkeypatch.delenv("JARVIS_QUIET_HOURS", raising=False)
    evening = datetime(2026, 10, 6, 22, 0, tzinfo=profile.tz())
    job = schedules.add("watch", "Deploy", "live", probe="url", target="https://a.example", now=evening)
    probe, calls = probe_that(Found(False, "Not yet."))
    s, _, _ = make({"url": probe}, quiet=None, now=evening)
    settings.set_preference("quiet_hours", "21:00-07:00")
    tick(s)
    assert calls == [] and schedules.get(job["id"])["runs"] == 0
    settings.set_preference("quiet_hours", "off")
    tick(s)
    assert len(calls) == 1


def test_a_watch_checked_by_a_task_starts_a_hidden_one_at_a_time(db):
    job = schedules.add("watch", "Priya's reply", "Priya replied about the budget", now=NOW)
    s, _, clock = make()
    tick(s)
    [task] = s.runner.started
    assert task["hidden"] and task["job"] == job["id"] and task["title"] == "Check: Priya's reply"
    assert "Priya replied about the budget" in task["instruction"] and "hasn't happened yet" in task["instruction"]
    clock.now = at(minutes=15)
    tick(s)  # the check is still going: the next waits for it
    assert len(s.runner.started) == 1
    s.runner.store.tasks[1]["status"] = "done"
    tick(s)
    assert len(s.runner.started) == 2


def test_a_watch_check_is_read_as_met_or_not_by_a_model_with_its_instruction(db, flags):
    job = schedules.add("watch", "Deploy", "Version 2 is live on app.example", now=NOW)
    flags.answers = {"met": [True, False]}
    task = {"id": 1, "job": job["id"]}
    assert asyncio.run(scheduler.read(task, "Version 2 is live.\nChecked the footer.")) == Reading(met=True)
    assert asyncio.run(scheduler.read(task, "Still 1.9.")) == Reading(met=False)
    assert asyncio.run(scheduler.read(task, "Couldn't tell.")) == Reading(met=None)  # the call failed
    field, prompt = flags.asked[0]
    assert field == "met" and "Version 2 is live on app.example" in prompt and "Checked the footer." in prompt


def test_only_an_only_changes_run_is_read_for_nothing_new_without_its_notepad(db, flags):
    every = schedules.add("task", "Brief", "Brief me.", every_minutes=60, now=NOW)
    changes = schedules.add("task", "Builds", "Tell me if a build failed.", every_minutes=60, only_changes=True,
                            now=NOW)
    flags.answers = {"nothing_new": [True]}
    assert asyncio.run(scheduler.read({"id": 1, "job": every["id"]}, "All quiet.")) == Reading()
    assert asyncio.run(scheduler.read({"id": 2, "job": None}, "Done.")) == Reading()
    assert flags.asked == []  # no call for a run that's always delivered, or isn't scheduled
    reading = asyncio.run(scheduler.read({"id": 3, "job": changes["id"]}, "All builds green.\nNOTEPAD: green."))
    assert reading == Reading(nothing_new=True)
    [(field, prompt)] = flags.asked
    assert field == "nothing_new" and "All builds green." in prompt and "NOTEPAD" not in prompt
    # A call that fails delivers the run
    assert asyncio.run(scheduler.read({"id": 4, "job": changes["id"]}, "One failed.")) == Reading(nothing_new=False)


def test_a_watch_check_that_couldnt_be_read_counts_as_not_yet(db):
    s, _, _ = make()
    job = schedules.add("watch", "Deploy", "v2 is live", now=NOW)
    tick(s)
    verdict = s.ended(s.runner.started[-1], "Version 2 is live.", "", Reading(met=None))
    assert verdict.quiet and verdict.spoken == "Deploy: not yet. Version 2 is live."
    assert schedules.get(job["id"])["status"] == schedules.ACTIVE


# ── Probes without a model ──────────────────────────────────────


def fake_http(monkeypatch, handler):
    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))


def test_url_probe(monkeypatch):
    job = {"target": "https://app.example/version", "expect": "2.4.0"}
    pages = iter([httpx.ConnectError("refused"), httpx.Response(503), httpx.Response(200, text="v2.3.9"),
                  httpx.Response(200, text="<b>v2.4.0</b>")])

    def handler(request):
        page = next(pages)
        if isinstance(page, Exception):
            raise page
        return page

    fake_http(monkeypatch, handler)
    found = [asyncio.run(scheduler.probe_url(job)) for _ in range(4)]
    assert [f.met for f in found] == [False, False, False, True]
    assert found[0].text == "It doesn't answer yet (ConnectError)."
    assert found[1].text == "It answers 503."
    assert found[3].text == "https://app.example/version is up and shows \"2.4.0\"."


# ── Tool watches ────────────────────────────────────────────────


@pytest.fixture
def results(monkeypatch):
    """Fake built-in tool calls: each answers `results.of[name]` as it is then (an exception: its text, as an
    error), recording `results.calls`."""
    fake = SimpleNamespace(of={}, calls=[])

    async def run(name, args):
        fake.calls.append((name, args))
        found = fake.of[name]
        return (str(found), True) if isinstance(found, Exception) else (json.dumps(found), False)

    monkeypatch.setattr(tools, "run", run)
    return fake


@pytest.fixture
def judged(monkeypatch):
    """Fake model reading a tool watch's new items: answers `judged.answers` in turn, then fails; records each
    prompt in `judged.asked`."""
    fake = SimpleNamespace(answers=[], asked=[])

    async def ask_json(prompt, schema, system_prompt, model, **options):
        fake.asked.append(prompt)
        if not fake.answers:
            raise RuntimeError("the model call failed")
        return fake.answers.pop(0), 0.0

    monkeypatch.setattr("jarvis.brain.sdk.ask_json", ask_json)
    return fake


def mail_from(who: str, subject: str, when: datetime, unread: bool = True) -> dict:
    return {"account": "work", "id": f"{who}-{subject}", "from": f"{who} <{who.lower()}@acme.example>",
            "subject": subject, "date": when.strftime("%a, %d %b %Y %H:%M:%S %z"), "unread": unread}


def gmail_watch(search: str = "from:priya") -> dict:
    tool, args, _ = schedules.gmail_watch(search)
    return schedules.add("watch", "Priya's reply", "Priya replies about the budget", probe="tool", target=tool,
                         arguments=args, now=NOW)


def test_a_gmail_watch_says_new_mail_once_a_model_has_read_only_that(db, results, judged):
    results.of["gmail_search"] = mail = [mail_from("Priya", "Old news", at(hours=-6))]
    job = gmail_watch()
    s, said, clock = make({"tool": scheduler.probe_tool})
    tick(s)
    assert results.calls == [("gmail_search", {"query": "from:priya", "max_results": 10})]
    assert judged.asked == [] and said["notified"] == []  # mail from before the watch: no model call
    assert schedules.get(job["id"])["last_result"] == "Nothing new (1 there already)."
    clock.now = at(minutes=10)
    tick(s)
    assert judged.asked == []  # the same result: no model call
    mail.insert(0, mail_from("Priya", "Re: budget", at(minutes=15)))
    judged.answers = [{"met": True, "notice": "New email from Priya: Re: budget",
                       "spoken": "Priya wrote back about the budget, saying it looks fine."}]
    clock.now = at(minutes=20)
    tick(s)
    [prompt] = judged.asked
    assert "Priya replies about the budget" in prompt and "Re: budget" in prompt and "Old news" not in prompt
    assert said["notified"] == [("JARVIS · Priya's reply", "New email from Priya: Re: budget.")]
    assert said["events"][0]["text"] == "Priya's reply: Priya wrote back about the budget, saying it looks fine."
    assert schedules.get(job["id"])["status"] == schedules.DONE


def test_a_late_first_check_still_counts_what_is_dated_since_the_watch_began(db, results, judged):
    results.of["gmail_search"] = [mail_from("Priya", "Re: budget", at(minutes=30)),
                                  mail_from("Priya", "Old news", at(hours=-6))]
    job = gmail_watch()
    judged.answers = [{"met": True, "notice": "New email from Priya: Re: budget"}]
    s, said, clock = make({"tool": scheduler.probe_tool})
    clock.now = at(hours=1)  # held back, say by quiet hours
    tick(s)
    assert "Re: budget" in judged.asked[0] and "Old news" not in judged.asked[0]
    assert said["events"][0]["text"] == "Priya's reply: New email from Priya: Re: budget."
    assert schedules.get(job["id"])["status"] == schedules.DONE


def test_something_new_that_isnt_it_stays_quiet_and_isnt_read_again(db, results, judged):
    results.of["gmail_search"] = mail = []
    job = gmail_watch()
    s, said, clock = make({"tool": scheduler.probe_tool})
    tick(s)
    mail.append(mail_from("Priya", "Out of office", at(minutes=5)))
    judged.answers = [{"met": False, "notice": "An out-of-office reply from Priya"}]
    clock.now = at(minutes=10)
    tick(s)
    after = schedules.get(job["id"])
    assert after["status"] == schedules.ACTIVE and said["notified"] == [] and said["events"] == []
    assert after["last_result"] == "Something new, not it yet: An out-of-office reply from Priya."
    clock.now = at(minutes=20)
    tick(s)
    assert len(judged.asked) == 1


def test_a_chat_watch_reads_only_new_messages_that_name_the_person(db, results, judged):
    def dm(who: str, *texts: str) -> dict:
        return {"account": "work", "space": f"spaces/{who}", "name": who, "kind": "dm", "unread": len(texts),
                "latest": [{"from": who, "time": "Tue 06 Oct 08:00", "text": t} for t in texts]}

    results.of["chat_unread"] = chat = {"unread": [dm("Priya Shah", "old")]}
    tool, args, expect = schedules.chat_watch("priya")
    job = schedules.add("watch", "Priya on Chat", "Priya writes to me", probe="tool", target=tool, arguments=args,
                        expect=expect, now=NOW)
    s, said, clock = make({"tool": scheduler.probe_tool})
    tick(s)
    assert results.calls == [("chat_unread", {"mentions_only": True, "days": 1})] and judged.asked == []
    chat["unread"].append(dm("Build Bot", "deploy done"))
    clock.now = at(minutes=10)
    tick(s)
    assert judged.asked == []  # new, but not from Priya
    chat["unread"][0] = dm("Priya Shah", "old", "Budget looks fine, go ahead")
    judged.answers = [{"met": True, "notice": "Priya Shah wrote to you on Chat",
                       "spoken": "Priya Shah wrote to you on Chat: budget looks fine, go ahead."}]
    clock.now = at(minutes=20)
    tick(s)
    [prompt] = judged.asked
    assert "Budget looks fine" in prompt and "deploy done" not in prompt
    assert said["notified"] == [("JARVIS · Priya on Chat", "Priya Shah wrote to you on Chat.")]  # her words never pushed
    assert said["events"][0]["text"] == "Priya on Chat: Priya Shah wrote to you on Chat: budget looks fine, go ahead."
    assert schedules.get(job["id"])["status"] == schedules.DONE


def test_new_finds_that_cant_be_read_yet_wait_for_the_next_check(db, results, judged, usage_file):
    results.of["gmail_search"] = mail = []
    job = gmail_watch()
    s, said, clock = make({"tool": scheduler.probe_tool})
    tick(s)
    mail.append(mail_from("Priya", "Re: budget", at(minutes=5)))
    clock.now = at(minutes=10)
    tick(s)  # the model call fails: a failed check
    assert schedules.get(job["id"])["last_result"] == ("Couldn't check: something new turned up, but reading it "
                                                       "failed")
    limit_used_up(at(hours=1).timestamp())
    clock.now = at(minutes=20)
    tick(s)  # Claude's usage limit is used up: no model call, not a failed check
    after = schedules.get(job["id"])
    assert len(judged.asked) == 1 and after["fails"] == 0 and "usage limit resets" in after["last_result"]
    usage_file.unlink()
    judged.answers = [{"met": True, "notice": "New email from Priya: Re: budget"}]
    clock.now = at(minutes=30)
    tick(s)
    assert "Re: budget" in judged.asked[-1] and schedules.get(job["id"])["status"] == schedules.DONE


def test_a_tool_that_fails_is_a_failed_check_with_its_reason(db, results):
    results.of["gmail_search"] = RuntimeError("The work account needs signing in again.")
    job = gmail_watch()
    s, said, clock = make({"tool": scheduler.probe_tool})
    tick(s)
    after = schedules.get(job["id"])
    assert (after["fails"], after["last_result"]) == (1, "Couldn't check: gmail_search failed")  # its words: log only


def test_old_mail_that_changes_or_comes_back_isnt_new(db, results, judged):
    old = mail_from("Priya", "Re: budget", at(hours=-6))
    other = {**mail_from("Priya", "Lunch?", at(minutes=5)), "account": "personal"}
    results.of["gmail_search"] = [old]
    gmail_watch()
    s, said, clock = make({"tool": scheduler.probe_tool})
    tick(s)
    results.of["gmail_search"] = [old | {"unread": False}]  # read on the phone
    clock.now = at(minutes=10)
    tick(s)
    assert judged.asked == []
    results.of["gmail_search"] = [old, other]
    judged.answers = [{"met": False, "notice": "Priya asked about lunch"}]
    clock.now = at(minutes=20)
    tick(s)
    results.of["gmail_search"] = [old]  # the personal account failed this time
    clock.now = at(minutes=30)
    tick(s)
    results.of["gmail_search"] = [old, other]  # and is back
    clock.now = at(minutes=40)
    tick(s)
    assert len(judged.asked) == 1 and said["notified"] == []


def test_expect_matches_what_an_item_says_not_its_field_names(db, results, judged):
    results.of["chat_unread"] = chat = {"unread": []}
    tool, args, _ = schedules.chat_watch("tim")
    schedules.add("watch", "Tim on Chat", "Tim writes to me", probe="tool", target=tool, arguments=args, expect="Tim",
                  now=NOW)
    s, _, clock = make({"tool": scheduler.probe_tool})
    tick(s)
    chat["unread"].append({"name": "Build Bot", "kind": "dm", "latest": [{"from": "Build Bot", "time": "Tue 06 Oct 09:20",
                                                                          "text": "deploy done"}]})
    clock.now = at(minutes=10)
    tick(s)
    assert judged.asked == []


def test_gmail_and_chat_watches_made_before_tool_watches_keep_working(db, results, judged):
    def chat_with(*texts: str) -> dict:
        return {"unread": [{"account": "work", "name": "Priya Shah", "kind": "dm", "unread": len(texts),
                            "latest": [{"from": "Priya Shah", "text": t} for t in texts]}]}

    # Made before migration 3, in a table migrated to 2 (resume and todo), as a live database has them
    with database.table("schedules", schedules._SCHEMA, migrations=schedules._MIGRATIONS[:1]) as old:
        for title, probe, target, baseline in (("Priya's reply", "gmail", "from:priya", None),
                                               ("Priya on Chat", "chat", "Priya", '["work:m1"]')):
            old.execute("insert into schedules (kind, title, text, every, until, probe, target, baseline, status, "
                        "next_run, created, updated) values ('watch', ?, ?, 10, ?, ?, ?, ?, 'active', ?, ?, ?)",
                        (title, f"{title} comes", at(hours=24).timestamp(), probe, target, baseline,
                         *[at(minutes=-30).timestamp()] * 3))
    results.of["gmail_search"] = mail = [mail_from("Priya", "Old news", at(hours=-6))]
    results.of["chat_unread"] = chat_with("old")
    s, said, clock = make({"tool": scheduler.probe_tool})
    tick(s)
    assert sorted(results.calls) == [("chat_unread", {"mentions_only": True, "days": 1}),
                                     ("gmail_search", {"query": "from:priya", "max_results": 10})]
    assert judged.asked == [] and said["notified"] == []  # what was there already isn't news
    mail.insert(0, mail_from("Priya", "Re: budget", at(minutes=5)))
    results.of["chat_unread"] = chat_with("old", "Budget looks fine")
    judged.answers = [{"met": True, "notice": "Priya wrote"}, {"met": True, "notice": "Priya wrote"}]
    clock.now = at(minutes=10)
    tick(s)
    assert any("Re: budget" in p for p in judged.asked) and any("Budget looks fine" in p for p in judged.asked)
    assert schedules.get(1)["status"] == schedules.get(2)["status"] == schedules.DONE


def test_a_check_that_ends_after_its_watch_changed_leaves_it_to_check_afresh(db, results, judged):
    results.of["gmail_search"] = [mail_from("Priya", "Re: budget", at(minutes=-1))]
    job = gmail_watch()
    s, said, _ = make({"tool": scheduler.probe_tool})
    judged.answers = [{"met": True, "notice": "New email from Priya: Re: budget"}]

    async def check():
        probing = schedules.update(job["id"], last_run=NOW.timestamp())
        schedules.change(job["id"], arguments={"query": "from:sam"}, now=NOW)
        await s._probe(probing)

    asyncio.run(check())
    after = schedules.get(job["id"])
    assert (after["status"], after["baseline"], said["notified"]) == (schedules.ACTIVE, None, [])


# ── Scheduled runs in the real runner ───────────────────────────


def with_scheduler(r: runner.Runner) -> Scheduler:
    async def nothing(*args):
        pass

    r.scheduler = Scheduler(r, nothing, r.broadcast, nothing, quiet_hours="")
    r.scheduler.pruned = float("inf")  # never touch this machine's data/tasks folders
    return r.scheduler


def test_a_watch_check_that_finds_nothing_leaves_no_trace(env, pushes, monkeypatch, flags):
    fake_run(monkeypatch, finishes("No reply from Priya so far.\nSearched mail."))
    flags.answers = {"met": [False]}
    job = schedules.add("watch", "Priya's reply", "Priya replied", now=datetime.now(profile.tz()))

    async def main():
        r = runner.Runner()
        q = events(r)
        await with_scheduler(r).tick()
        await settle(r)
        return r, drained(q)

    r, said = asyncio.run(main())
    task = r.store.get(schedules.get(job["id"])["last_task"])
    assert (task["status"], task["hidden"], task["seen"], task["job"]) == ("done", 1, 1, job["id"])
    assert [e for e in said if e["type"] == "status"] == [] and pushes == []
    assert r.store.recent() == [] and r.store.unseen() == []
    assert schedules.get(job["id"])["last_result"] == "No reply from Priya so far."
    assert [field for field, _ in flags.asked] == ["met"]


def test_a_watch_check_that_finds_it_is_announced_under_the_watch(env, pushes, monkeypatch, flags):
    fake_run(monkeypatch, finishes("Priya replied: the budget is approved.\nFrom her email at 10:02."))
    flags.answers = {"met": [True]}
    job = schedules.add("watch", "Priya's reply", "Priya replied", now=datetime.now(profile.tz()))

    async def main():
        r = runner.Runner()
        q = events(r)
        await with_scheduler(r).tick()
        await settle(r)
        await asyncio.sleep(0.05)
        return r, drained(q)

    r, said = asyncio.run(main())
    task = r.store.get(schedules.get(job["id"])["last_task"])
    assert (task["status"], task["hidden"], task["seen"]) == ("done", 0, 0)
    [status] = [e for e in said if e["type"] == "status"]
    assert status["text"] == "Priya's reply: Priya replied: the budget is approved."
    assert [p["title"] for p in pushes] == ["Priya's reply"]
    assert schedules.get(job["id"])["status"] == schedules.DONE


def test_a_scheduled_task_with_nothing_to_report_stays_silent(env, pushes, monkeypatch, flags):
    fake_run(monkeypatch, finishes("No failing builds."), finishes("Two builds failed overnight.\n..."))
    flags.answers = {"nothing_new": [True, False]}
    job = schedules.add("task", "Build check", "Tell me only if a build failed.", every_minutes=60,
                        only_changes=True, now=datetime.now(profile.tz()) - timedelta(minutes=61))

    async def main():
        r = runner.Runner()
        q = events(r)
        s = with_scheduler(r)
        await s.tick()
        await settle(r)
        first = drained(q)
        schedules.update(job["id"], next_run=0)
        await s.tick()
        await settle(r)
        await asyncio.sleep(0.05)
        return r, first, drained(q)

    r, first, second = asyncio.run(main())
    assert [e for e in first if e["type"] == "status"] == []
    [status] = [e for e in second if e["type"] == "status"]
    assert status["text"] == "Build check: Two builds failed overnight."
    assert [p["title"] for p in pushes] == ["Build check"]
    assert [t["title"] for t in r.store.recent()] == ["Build check"]  # the silent run is left out


def test_a_scheduled_run_keeps_its_summary_without_the_notepad(env, pushes, monkeypatch, flags):
    fake_run(monkeypatch, finishes("Two builds failed overnight.\n- web\n- api\nNOTEPAD: failed: web, api."))
    job = schedules.add("task", "Build check", "Which builds failed?", every_minutes=60,
                        now=datetime.now(profile.tz()) - timedelta(minutes=61))

    async def main():
        r = runner.Runner()
        await with_scheduler(r).tick()
        await settle(r)
        await asyncio.sleep(0.05)
        return r

    r = asyncio.run(main())
    task = r.store.get(schedules.get(job["id"])["last_task"])
    assert task["summary"] == "Two builds failed overnight.\n- web\n- api"
    assert schedules.notepad(schedules.get(job["id"]))["text"] == "failed: web, api."
    assert [p["body"] for p in pushes] == ["Build check: Two builds failed overnight."]
    assert flags.asked == []  # always delivered: nothing to read


def test_do_not_disturb_keeps_task_updates_off_the_mac_and_the_phone(env, pushes, monkeypatch):
    from jarvis.tasks import hold

    fake_run(monkeypatch, finishes("Found three flights."))
    hold.start(minutes=30)

    async def main():
        r = runner.Runner()
        q = events(r)
        r.start("research", "Flights", "Find flights to Goa.")
        await settle(r)
        await asyncio.sleep(0.05)
        return r, drained(q)

    r, said = asyncio.run(main())
    assert pushes == []
    [status] = [e for e in said if e["type"] == "status"]  # still said in a live session
    assert status["text"].startswith("Your task, Flights, is done.")
    assert r.store.get(1)["seen"] == 0  # and left unheard for the next greeting


def test_a_restart_drops_a_watch_check_quietly_but_not_a_task(env):
    store = Store()
    check = store.create("research", "Check: deploy", "x", None, job=1, hidden=True)
    brief = store.create("research", "Morning brief", "x", None, job=2)
    for t in (check, brief):
        store.set(t["id"], status="running")
    r = runner.Runner()
    assert r.store.get(check["id"])["error"] is None
    assert "restarted" in r.store.get(brief["id"])["error"]
    assert [t["id"] for t in r.store.recent()] == [brief["id"]]


def test_scheduled_runs_use_the_lighter_model(env):
    research = {"kind": "research", "job": 3}
    assert worker._model(research) == (worker.SCHEDULE_MODEL, worker.SCHEDULE_EFFORT)
    assert worker._model({"kind": "code_change", "job": 3}) == (worker.TASK_MODEL, worker.TASK_EFFORT)
    assert worker._model({"kind": "research", "job": None}) == (worker.TASK_MODEL, worker.TASK_EFFORT)


# ── Storage ─────────────────────────────────────────────────────


def test_old_hidden_runs_are_forgotten_but_the_newest_task_kept(db):
    store = Store()
    old = [store.create("research", f"Check {i}", "x", None, job=1, hidden=True) for i in range(3)]
    for t in old:
        store.set(t["id"], status="done")
        store.log(t["id"], "done", "not yet")
    store.db.execute("update tasks set updated = 0")
    assert store.prune_hidden(3) == [old[0]["id"], old[1]["id"]]
    assert store.get(old[0]["id"]) is None and store.get(old[2]["id"]) is not None
    assert store.history(old[0]["id"]) == []


def test_tidying_removes_forgotten_runs_folders_and_old_jobs(db, tmp_path, monkeypatch):
    monkeypatch.setattr(scheduler, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))  # the default login's folder
    store = Store()
    old, newest = (store.create("research", "Check", "x", None, job=1, hidden=True) for _ in range(2))
    store.db.execute("update tasks set status = 'done', updated = 0")
    folder = tmp_path / "data" / "tasks" / str(old["id"])
    sessions = tmp_path / "claude" / "projects" / str(folder).replace("/", "-").replace("_", "-").replace(".", "-")
    for d in (folder, sessions, tmp_path / "data" / "tasks" / str(newest["id"])):
        d.mkdir(parents=True)
    ended = schedules.add("reminder", "Old", "x", in_minutes=1, now=NOW)
    schedules.update(ended["id"], status=schedules.DONE, updated=0)
    kept = schedules.add("reminder", "Live", "x", in_minutes=1, now=NOW)
    s = Scheduler(type("R", (), {"store": store})(), None)
    asyncio.run(s.prune())
    assert not folder.exists() and not sessions.exists()
    assert (tmp_path / "data" / "tasks" / str(newest["id"])).exists()
    assert schedules.get(ended["id"]) is None and schedules.get(kept["id"])


def test_tidying_removes_ended_tasks_temp_folders_after_a_week(db, tmp_path, monkeypatch):
    monkeypatch.setattr(scheduler, "DATA_DIR", tmp_path / "data")
    temp = use_task_cache(monkeypatch, tmp_path / "cache") / "tmp"
    store = Store()
    ended, running, recent = (store.create("research", "Check", "x", None) for _ in range(3))
    store.db.execute("update tasks set status = 'done', updated = 0")
    store.set(running["id"], status="running")
    store.set(recent["id"], status="done")  # ended just now: a follow-up may still use its folder
    folders = {name: temp / f"task-{i}" for name, i in
               (("ended", ended["id"]), ("running", running["id"]), ("recent", recent["id"]), ("gone", 999))}
    for d in [*folders.values(), temp / "task-notes"]:
        d.mkdir(parents=True)
    asyncio.run(Scheduler(type("R", (), {"store": store})(), None).prune())
    assert not folders["ended"].exists() and not folders["gone"].exists()
    assert folders["running"].exists() and folders["recent"].exists() and (temp / "task-notes").exists()


# ── The voice tools ─────────────────────────────────────────────


def call(name: str, args: dict):
    content, is_error = asyncio.run(tools.run(name, args))
    return (content if is_error else json.loads(content)), is_error


def test_schedule_tools(db):
    added, error = call("schedule_add", {"kind": "reminder", "title": "Call Priya", "text": "Call Priya.",
                                         "in_minutes": 30})
    assert not error and added["kind"] == "reminder" and added["when"].startswith("once, at ")
    watch, _ = call("schedule_add", {"kind": "watch", "title": "Deploy", "text": "v2 live", "probe": "url",
                                     "target": "https://app.example", "expect": "v2"})
    assert watch["check"] == "JARVIS checks the page: https://app.example, for \"v2\""
    listed, _ = call("schedule_list", {})
    assert [j["title"] for j in listed] == ["Deploy", "Call Priya"]  # soonest first: the watch looks straight away
    changed, _ = call("schedule_change", {"id": added["id"], "paused": True})
    assert changed["status"] == "paused" and changed["outcome"] == "Changed Call Priya."
    cancelled, _ = call("schedule_cancel", {"id": watch["id"]})
    assert cancelled["status"] == "cancelled"
    refused, error = call("schedule_add", {"kind": "reminder", "title": "x", "text": "x"})
    assert error and "Say when" in refused


def test_a_new_watch_is_told_the_users_quiet_hours(db, monkeypatch):
    monkeypatch.delenv("JARVIS_QUIET_HOURS", raising=False)
    watch = {"kind": "watch", "title": "Deploy", "text": "v2 live", "probe": "url", "target": "https://app.example"}
    settings.set_preference("quiet_hours", "22:00-06:30")
    added, _ = call("schedule_add", watch)
    assert added["note"].startswith("Watches aren't checked in quiet hours (22:00 to 06:30)")
    settings.set_preference("quiet_hours", "off")
    added, _ = call("schedule_add", watch)
    assert "note" not in added


def test_a_tool_watch_is_set_up_and_changed_by_voice(db):
    watch, error = call("schedule_add", {"kind": "watch", "title": "Priya on Chat", "text": "Priya writes to me",
                                         "probe": "tool", "target": "mcp__jarvis__chat_unread",
                                         "arguments": {"mentions_only": True, "days": 1}, "expect": "Priya"})
    assert not error and watch["when"].startswith("every 10 minutes")
    assert watch["check"] == 'JARVIS calls the tool: chat_unread with {"mentions_only": true, "days": 1}, for "Priya"'
    schedules.update(watch["id"], baseline=json.dumps(["a1"]))
    changed, error = call("schedule_change", {"id": watch["id"], "target": "gmail_search",
                                              "arguments": {"query": "from:priya"}})
    assert not error
    assert changed["check"] == 'JARVIS calls the tool: gmail_search with {"query": "from:priya"}, for "Priya"'
    assert schedules.get(watch["id"])["baseline"] is None  # compared afresh
    refused, error = call("schedule_change", {"id": watch["id"], "target": "gmail_modify"})
    assert error and "isn't a reading tool" in refused


def test_schedule_tools_show_the_notepad_and_set_only_changes(db):
    added, _ = call("schedule_add", {"kind": "task", "title": "PR digest", "text": "Which PRs await my review?",
                                     "when": "09:00", "repeat": "weekdays", "only_changes": True})
    assert added["only_changes"] is True and "notepad" not in added
    schedules.update(added["id"], notepad=json.dumps({"at": datetime.now(profile.tz()).timestamp(), "text": "PRs 12 and 14."}))
    [listed], _ = call("schedule_list", {})
    assert listed["notepad"].endswith(": PRs 12 and 14.")
    changed, _ = call("schedule_change", {"id": added["id"], "only_changes": False})
    assert "only_changes" not in changed
    refused, error = call("schedule_add", {"kind": "reminder", "title": "x", "text": "x", "in_minutes": 5,
                                           "only_changes": True})
    assert error and "only_changes is for scheduled tasks" in refused


def test_schedule_hold_pauses_and_resumes_everything(db):
    on, error = call("schedule_hold", {"until": "23:30"})
    assert not error and on["outcome"].startswith("Do not disturb is on until ")
    schedules.add("reminder", "Stretch", "Stretch.", in_minutes=5)
    listed, _ = call("schedule_list", {})
    assert listed["do_not_disturb"].startswith("on until") and [j["title"] for j in listed["jobs"]] == ["Stretch"]
    asked, _ = call("schedule_hold", {})
    assert asked["do_not_disturb"].startswith("on until")
    off, _ = call("schedule_hold", {"on": False})
    assert off == {"do_not_disturb": "off", "outcome": "Do not disturb is off."}  # nothing was due yet
    assert call("schedule_hold", {"on": False})[0]["outcome"] == "Do not disturb wasn't on."
    _, error = call("schedule_hold", {"minutes": 60 * 24 * 8})
    assert error


# ── Job memory: notepad and only_changes ────────────────────────


def brief_ran(s: Scheduler, clock: Clock, only_changes: bool = False) -> tuple[dict, dict]:
    """A daily 09:30 brief whose run has just started: the job and its task."""
    job = schedules.add("task", "Morning brief", "Brief me on my day.", when="09:30", repeat="daily",
                        only_changes=only_changes, now=NOW)
    clock.now = at(minutes=15)
    assert tick(s) == [job["id"]]
    return schedules.get(job["id"]), s.runner.started[-1]


def test_a_run_keeps_a_notepad_for_the_next_which_hears_it(db):
    s, _, clock = make()
    job, task = brief_ran(s, clock)
    assert "no notepad from an earlier run" in task["instruction"] and "NOTEPAD:" in task["instruction"]
    verdict = s.ended(task, "Two meetings, one new PR.\n- Standup 10:00\n- PR 14 by Priya\n\n"
                            "NOTEPAD: Meetings: standup, design review. PRs seen: 12, 14.", "")
    assert verdict.spoken == "Morning brief: Two meetings, one new PR." and not verdict.quiet
    assert verdict.summary == "Two meetings, one new PR.\n- Standup 10:00\n- PR 14 by Priya"  # kept without the notepad
    note = schedules.notepad(schedules.get(job["id"]))
    assert note == {"at": at(minutes=15).timestamp(), "text": "Meetings: standup, design review. PRs seen: 12, 14."}

    s.runner.store.tasks[task["id"]]["status"] = "done"
    clock.now = at(days=1, minutes=15)
    tick(s)
    second = s.runner.started[-1]["instruction"]
    assert "Your notepad from the last run, Tue 06 Oct 09:30: Meetings: standup, design review. PRs seen: 12, 14." in second
    assert "what's new since the last run" in second


def test_a_run_without_a_notepad_keeps_the_last_one(db):
    s, _, clock = make()
    job, task = brief_ran(s, clock)
    schedules.update(job["id"], notepad=json.dumps({"at": NOW.timestamp(), "text": "PRs seen: 12."}))
    verdict = s.ended(task, "One new PR.", "")
    assert verdict.summary is None  # nothing to take out
    assert schedules.notepad(schedules.get(job["id"]))["text"] == "PRs seen: 12."


def test_only_changes_stays_silent_when_nothing_is_new(db):
    s, _, clock = make()
    job, task = brief_ran(s, clock, only_changes=True)
    assert "wants to hear only what's new" in task["instruction"]
    assert not s.ended(task, "PR 14 waits for your review.\nNOTEPAD: PR 14.", "").quiet
    # The run should say nothing new; a word-for-word repeat is silent anyway
    again = s.ended(task, "PR 14 waits for your review.\nNOTEPAD: PR 14.", "")
    assert again.quiet and again.spoken.startswith("Morning brief: nothing new.")
    assert not s.ended(task, "PR 15 waits for your review too.\nNOTEPAD: PR 14, 15.", "").quiet
    # Read as nothing new: silent, its notepad kept
    silent = s.ended(task, "Nothing new since PR 15.\nNOTEPAD: PR 14, 15.", "", Reading(nothing_new=True))
    assert silent.quiet and silent.spoken == "Morning brief: nothing to report."
    assert silent.summary == "Nothing new since PR 15."
    assert schedules.notepad(schedules.get(job["id"]))["text"] == "PR 14, 15."


def test_a_brief_is_always_delivered_even_when_its_run_says_nothing_new(db):
    s, _, clock = make()
    job, task = brief_ran(s, clock)
    assert "always give the full report" in task["instruction"] and "stays silent" not in task["instruction"]
    verdict = s.ended(task, "Your leads syncup is at ten thirty. Sam asked to work from home today.\n"
                            "NOTEPAD: Syncup 10:30, Sam WFH ask.", "")
    assert not verdict.quiet and verdict.title == "Morning brief"
    assert verdict.spoken == "Morning brief: Your leads syncup is at ten thirty. Sam asked to work from home today."
    assert verdict.summary == "Your leads syncup is at ten thirty. Sam asked to work from home today."
    assert schedules.get(job["id"])["last_result"] == verdict.spoken.removeprefix("Morning brief: ")
    # A nothing-new reading doesn't silence a job without only_changes
    quiet_day = s.ended(task, "A quiet day: no meetings and no new mail.", "", Reading(nothing_new=True))
    assert not quiet_day.quiet and quiet_day.spoken == "Morning brief: A quiet day: no meetings and no new mail."
    bare = s.ended(task, "NOTEPAD: nothing.", "")
    assert not bare.quiet and bare.spoken == "Morning brief: Nothing to report this time."


def test_only_only_changes_lets_a_run_stay_silent_whatever_its_instruction_says(db):
    s, _, _ = make()
    # Wording isn't matched: without only_changes, even "only tell me if" is delivered every run
    asked = schedules.add("task", "Build check", "Tell me only if a build failed.", every_minutes=60, now=NOW)
    assert "stays silent" not in scheduler.task_prompt(asked)
    said = s.ended({"id": 1, "job": asked["id"]}, "All builds green.", "", Reading(nothing_new=True))
    assert not said.quiet and said.spoken == "Build check: All builds green."
    # With only_changes set at scheduling, a run read as nothing new is silent
    job = schedules.add("task", "Build watch", "Tell me only if a build failed.", every_minutes=60, only_changes=True,
                        now=NOW)
    task = {"id": 2, "job": job["id"]}
    assert "JARVIS stays silent" in scheduler.task_prompt(job)
    assert s.ended(task, "All builds green.", "", Reading(nothing_new=True)).quiet
    said = s.ended(task, "The iOS build failed on signing.", "", Reading(nothing_new=False))
    assert not said.quiet and said.spoken == "Build watch: The iOS build failed on signing."


# ── Failures: retried, held for the usage limit, said once ──────


def test_a_passing_failure_is_retried_after_5_15_and_30_minutes_then_said(db):
    s, _, clock = make()
    job, task = brief_ran(s, clock)
    tomorrow = job["next_run"]
    for minutes, tries in ((5, 1), (15, 2), (30, 3)):
        verdict = s.ended(task, "", "API Error: 529 Overloaded", kind=retry.PASSING)
        job = schedules.get(job["id"])
        assert verdict.quiet and "trying again at" in verdict.spoken
        assert (job["next_run"], job["retries"]) == (clock.now.timestamp() + minutes * 60, tries)
        s.runner.store.tasks[task["id"]]["status"] = "failed"
        clock.now = datetime.fromtimestamp(job["next_run"], profile.tz())
        assert tick(s) == [job["id"]]  # the retry runs
        task = s.runner.started[-1]
    assert s.ended(task, "", "API Error: 529 Overloaded", kind=retry.PASSING) is None  # out of retries: said as usual
    job = schedules.get(job["id"])
    assert (job["retries"], job["failing"], job["next_run"]) == (0, "passing", tomorrow)


def test_a_failure_is_said_once_until_a_run_succeeds(db):
    s, _, clock = make()
    job, task = brief_ran(s, clock)
    assert s.ended(task, "", "ValueError: the project's folder is gone") is None
    assert schedules.get(job["id"])["failing"] == "error"
    again = s.ended(task, "", "ValueError: the project's folder is gone")
    assert again.quiet and again.spoken.startswith("Morning brief: failed again")
    s.ended(task, "All clear.", "")
    assert schedules.get(job["id"])["failing"] is None
    assert s.ended(task, "", "ValueError: the project's folder is gone") is None  # broken anew: said again


def limit_used_up(resets_at: float, kind: str = "five_hour", utilization: float | None = None,
                  status: str = "rejected") -> None:
    from claude_agent_sdk import RateLimitInfo

    from jarvis.brain import usage

    usage.record(RateLimitInfo(status=status, resets_at=int(resets_at), rate_limit_type=kind,
                               utilization=utilization), now=NOW.timestamp())


@pytest.fixture
def usage_file(tmp_path, monkeypatch):
    from jarvis.brain import usage

    monkeypatch.setattr(usage, "USAGE_FILE", tmp_path / "usage.json")
    return tmp_path / "usage.json"


def test_runs_that_need_a_model_wait_for_the_usage_limit_and_reminders_dont(db, usage_file):
    s, said, clock = make()
    reset = at(hours=2).timestamp()
    limit_used_up(reset)
    brief = schedules.add("task", "Morning brief", "Brief me.", when="09:30", repeat="daily", now=NOW)
    nudge = schedules.add("reminder", "Stretch", "Stretch.", when="09:30", now=NOW)
    clock.now = at(minutes=15)
    assert tick(s) == [nudge["id"]] and s.runner.started == []
    held = schedules.get(brief["id"])
    assert held["next_run"] == reset and held["last_result"].startswith("Waiting for Claude's usage limit")
    clock.now = at(hours=2, minutes=1)  # reset: it runs once, not counted as missed
    assert tick(s) == [brief["id"]]
    assert schedules.get(brief["id"])["next_run"] == at(days=1, minutes=15).timestamp()


def test_the_five_hour_window_nearly_used_up_holds_scheduled_runs_too(db, usage_file):
    s, _, clock = make()
    limit_used_up(at(hours=3).timestamp(), utilization=0.95, status="allowed_warning")
    job = schedules.add("watch", "Deploy", "v2 is live", now=NOW)  # checked by a task
    assert tick(s) == [] and schedules.get(job["id"])["next_run"] == at(hours=3).timestamp()


def test_a_run_that_hits_the_usage_limit_runs_again_at_its_reset(db, usage_file):
    s, _, clock = make()
    job, task = brief_ran(s, clock)
    limit_used_up(at(hours=4).timestamp())
    verdict = s.ended(task, "", "it hit the Claude usage limit, which resets at 1:15 PM", kind=retry.LIMIT)
    assert verdict.quiet and "runs again when that resets at 1:15 PM" in verdict.spoken
    assert schedules.get(job["id"])["next_run"] == at(hours=4).timestamp()


def test_a_one_off_run_that_failed_on_a_passing_error_comes_back(db):
    s, _, clock = make()
    job = schedules.add("task", "Look up flights", "Find flights to Goa.", in_minutes=10, now=NOW)
    clock.now = at(minutes=10)
    tick(s)
    assert schedules.get(job["id"])["status"] == schedules.DONE
    s.ended(s.runner.started[-1], "", "CLIConnectionError: the CLI went away", kind=retry.PASSING)
    job = schedules.get(job["id"])
    assert (job["status"], job["next_run"]) == (schedules.ACTIVE, at(minutes=15).timestamp())


def test_a_check_that_hit_the_usage_limit_isnt_a_failed_check(db):
    s, said, clock = make()
    job = schedules.add("watch", "Deploy", "v2 is live", now=NOW)
    tick(s)
    for _ in range(scheduler.MAX_FAILS):
        assert s.ended(s.runner.started[-1], "", "it hit the Claude usage limit", kind=retry.LIMIT).quiet
    assert schedules.get(job["id"])["fails"] == 0 and said["notified"] == []


# ── Do not disturb ──────────────────────────────────────────────


def test_do_not_disturb_holds_every_job_until_it_ends(db):
    from jarvis.tasks import hold

    s, said, clock = make()
    hold.start(until="12:00", now=NOW)
    nudge = schedules.add("reminder", "Stretch", "Stretch.", when="09:30", now=NOW)
    brief = schedules.add("task", "Morning brief", "Brief me.", when="09:30", repeat="daily", now=NOW)
    watch = schedules.add("watch", "Deploy", "v2 is live", probe="url", target="https://app.example", now=NOW)
    clock.now = at(minutes=15)
    assert tick(s) == [] and said["notified"] == [] and s.runner.started == []
    clock.now = at(hours=2, minutes=46)  # 12:01: the brief is 2.5 hours late, but it was held, not missed
    assert set(tick(s)) == {nudge["id"], brief["id"], watch["id"]}
    assert said["notified"][0] == ("JARVIS · Reminder", "Stretch. It was due at 09:30.")
    assert [t["title"] for t in s.runner.started] == ["Morning brief"]


def test_resuming_lets_held_jobs_run_at_once(db):
    from jarvis.tasks import hold

    s, said, clock = make()
    hold.start(now=NOW)  # until resumed
    schedules.add("reminder", "Stretch", "Stretch.", in_minutes=5, now=NOW)
    clock.now = at(hours=5)
    assert tick(s) == []
    hold.end(clock.now.timestamp())
    assert len(tick(s)) == 1 and said["notified"][0][1].startswith("Stretch.")
