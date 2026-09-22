"""Tests for jarvis.tasks.heartbeat. Run with: make test"""

import asyncio
import importlib
import logging
from dataclasses import replace
from datetime import datetime, time, timedelta

import pytest

from jarvis.core import config, profile
from jarvis.tasks import heartbeat
from jarvis.tasks.heartbeat import Alert, Heartbeat, in_quiet_hours, parse_quiet_hours

TZ = profile.tz()
AFTERNOON = datetime(2026, 9, 26, 15, 0, tzinfo=TZ)


class Notifier:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def __call__(self, title: str, text: str) -> None:
        self.sent.append((title, text))


def source(*alerts: Alert):
    calls = []

    async def fn(now: datetime) -> list[Alert]:
        calls.append(now)
        return list(alerts)

    fn.calls = calls
    return fn


def make(tmp_path, sources, at=AFTERNOON, **kw) -> tuple[Heartbeat, Notifier]:
    notify = Notifier()
    kw.setdefault("quiet_hours", "23:00-08:00")
    return Heartbeat(sources, notify, state_file=tmp_path / "heartbeat.json", clock=lambda: at, **kw), notify


MEETING = Alert("calendar:work:2026-09-26T15:10:00+05:30:Standup", "calendar", "Standup starts in 10 minutes.")


# ── Scheduling and config ───────────────────────────────────────


def test_config_defaults_and_overrides(monkeypatch):
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: None)  # the real .env mustn't leak in
    for var in ("JARVIS_HEARTBEAT", "JARVIS_HEARTBEAT_MINUTES", "JARVIS_HEARTBEAT_QUIET_HOURS", "JARVIS_HEARTBEAT_TRIAGE"):
        monkeypatch.delenv(var, raising=False)
    try:
        importlib.reload(config)
        assert config.HEARTBEAT_ENABLED is True
        assert config.HEARTBEAT_MINUTES == 15
        assert config.HEARTBEAT_QUIET_HOURS == "23:00-08:00"
        assert config.HEARTBEAT_TRIAGE is False

        monkeypatch.setenv("JARVIS_HEARTBEAT", "off")
        monkeypatch.setenv("JARVIS_HEARTBEAT_MINUTES", "5")
        importlib.reload(config)
        assert config.HEARTBEAT_ENABLED is False
        assert config.HEARTBEAT_MINUTES == 5

        monkeypatch.setenv("JARVIS_HEARTBEAT_MINUTES", "0")
        importlib.reload(config)
        assert config.HEARTBEAT_MINUTES == 1  # never a busy loop
    finally:
        monkeypatch.undo()
        importlib.reload(config)


class Stop(BaseException):
    pass


def test_run_beats_every_interval_and_survives_a_failed_beat(tmp_path):
    waits: list[float] = []

    async def sleep(seconds: float) -> None:
        waits.append(seconds)
        if len(waits) == 3:
            raise Stop

    hb, _ = make(tmp_path, {}, minutes=5, sleep=sleep)
    beats = []

    async def beat():
        beats.append(1)
        if len(beats) == 1:
            raise RuntimeError("boom")
        return []

    hb.beat = beat
    with pytest.raises(Stop):
        asyncio.run(hb.run())
    assert len(beats) == 3
    assert waits == [300, 300, 300]


# ── Flagging ────────────────────────────────────────────────────


def test_new_item_is_notified_and_announced(tmp_path):
    events = []
    reminder = Alert("task:3:waiting:1", "tasks", "Task 3, retry logic, is still waiting for your answer: which DB?", task_id=3)
    hb, notify = make(tmp_path, {"calendar": source(MEETING), "tasks": source(reminder)}, announce=events.append)
    flagged = asyncio.run(hb.beat())

    assert flagged == [MEETING, reminder]
    assert notify.sent == [("JARVIS · calendar", "Standup starts in 10 minutes."), ("JARVIS · tasks", reminder.text)]
    assert events == [{"type": "heartbeat", "tasks": [3],
                       "text": "Heads up. Standup starts in 10 minutes. " + reminder.text,
                       "urgency": "soon", "at": None, "pushed": False}]


def test_same_item_is_never_flagged_twice_even_across_restarts(tmp_path):
    hb, notify = make(tmp_path, {"calendar": source(MEETING)})
    asyncio.run(hb.beat())
    asyncio.run(hb.beat())
    assert len(notify.sent) == 1

    later = Alert("calendar:work:2026-09-26T15:20:00+05:30:Review", "calendar", "Review starts in 20 minutes.")
    hb2, notify2 = make(tmp_path, {"calendar": source(MEETING, later)})  # a fresh runner, same state file
    asyncio.run(hb2.beat())
    assert notify2.sent == [("JARVIS · calendar", later.text)]


def test_notification_failure_doesnt_break_the_beat(tmp_path):
    async def broken(title, text):
        raise RuntimeError("osascript failed")

    hb = Heartbeat({"calendar": source(MEETING)}, broken, state_file=tmp_path / "hb.json", clock=lambda: AFTERNOON)
    assert asyncio.run(hb.beat()) == [MEETING]


def test_first_answer_from_mail_is_only_recorded(tmp_path):
    old = Alert("email:work:1", "email", "New email from Alice: Old thread.", soft=True)
    new = Alert("email:work:2", "email", "New email from Alice: Contract renewal.", soft=True)
    hb, notify = make(tmp_path, {"email": source(old), "calendar": source(MEETING)})
    asyncio.run(hb.beat())
    assert notify.sent == [("JARVIS · calendar", MEETING.text)]  # the calendar isn't a backlog

    hb.sources = {"email": source(old, new)}
    asyncio.run(hb.beat())
    assert notify.sent[1:] == [("JARVIS · email", new.text)]


def test_mail_backlog_waits_for_a_source_that_failed_at_first(tmp_path):
    async def down(now):
        raise RuntimeError("offline")

    old = Alert("email:work:1", "email", "New email from Alice: Old thread.", soft=True)
    hb, notify = make(tmp_path, {"email": down})
    asyncio.run(hb.beat())
    hb.sources = {"email": source(old)}
    asyncio.run(hb.beat())  # first real answer: recorded, not flagged
    assert notify.sent == []


def test_at_most_a_few_alerts_per_beat_and_the_rest_wait(tmp_path):
    many = [Alert(f"calendar:{i}", "calendar", f"Meeting {i} starts soon.") for i in range(7)]
    hb, notify = make(tmp_path, {"calendar": source(*many)})
    asyncio.run(hb.beat())
    assert len(notify.sent) == heartbeat.MAX_ALERTS
    asyncio.run(hb.beat())
    assert len(notify.sent) == 7


# ── Push to the phone ───────────────────────────────────────────


class Pusher:
    def __init__(self, fail: bool = False) -> None:
        self.sent: list[tuple[str, str, str, str]] = []
        self.fail = fail

    async def __call__(self, title: str, body: str, url: str, tag: str) -> None:
        self.sent.append((title, body, url, tag))
        if self.fail:
            raise RuntimeError("push service down")


def test_flagged_alerts_are_pushed_alongside_the_mac_notification(tmp_path):
    phone = Pusher()
    reminder = Alert("task:3:waiting:1", "tasks", "Task 3, retry logic, is still waiting for your answer: which DB?", task_id=3)
    hb, notify = make(tmp_path, {"calendar": source(MEETING), "tasks": source(reminder)}, push=phone)
    asyncio.run(hb.beat())

    assert len(notify.sent) == 2
    (cal_title, cal_body, cal_url, cal_tag), task_push = phone.sent
    assert (cal_title, cal_body, cal_url) == ("Calendar", MEETING.text, "/?alert=calendar")
    assert cal_tag.startswith("alert-") and "Standup" not in cal_tag  # stable per item, but says nothing about it
    assert task_push == ("Tasks", reminder.text, "/?task=3", "task-3")  # opens the app on the task


def test_pushes_respect_dedup_and_quiet_hours(tmp_path):
    phone = Pusher()
    hb, _ = make(tmp_path, {"calendar": source(MEETING)}, push=phone, at=datetime(2026, 9, 26, 23, 30, tzinfo=TZ))
    asyncio.run(hb.beat())
    assert phone.sent == []  # quiet hours: nothing checked, nothing pushed

    hb.clock = lambda: datetime(2026, 9, 27, 8, 5, tzinfo=TZ)
    asyncio.run(hb.beat())
    asyncio.run(hb.beat())
    assert [body for _, body, _, _ in phone.sent] == [MEETING.text]  # once, at the first beat after


def test_triaged_out_alerts_are_not_pushed(tmp_path):
    phone = Pusher()
    promo = Alert("email:work:9", "email", "New email from Shop: Sale!", soft=True)

    async def triage(alerts):
        return set()

    hb, _ = make(tmp_path, {"email": source(promo)}, push=phone, triage=triage)
    hb.seen.started.add("email")
    asyncio.run(hb.beat())
    assert phone.sent == []


def test_push_failure_doesnt_break_the_beat_or_the_mac_notification(tmp_path):
    phone = Pusher(fail=True)
    hb, notify = make(tmp_path, {"calendar": source(MEETING)}, push=phone)
    assert asyncio.run(hb.beat()) == [MEETING]
    assert notify.sent == [("JARVIS · calendar", MEETING.text)]
    asyncio.run(hb.beat())
    assert len(phone.sent) == 1  # still counts as flagged: not retried every beat


def test_make_wires_push_only_when_given():
    phone = Pusher()

    class Store:
        def recent(self):
            return []

    assert heartbeat.make(Store(), Notifier(), push=phone).push is phone
    assert heartbeat.make(Store(), Notifier()).push is None


# ── Quiet hours ─────────────────────────────────────────────────


def test_parse_quiet_hours():
    assert parse_quiet_hours("23:00-08:00") == (time(23), time(8))
    assert parse_quiet_hours(" 8:30 - 9:00 ") == (time(8, 30), time(9))
    assert parse_quiet_hours("") is None
    assert parse_quiet_hours("10:00-10:00") is None
    assert parse_quiet_hours("late") is None


@pytest.mark.parametrize("hour, minute, quiet", [(23, 0, True), (2, 0, True), (7, 59, True), (8, 0, False),
                                                 (15, 0, False), (22, 59, False)])
def test_overnight_quiet_hours(hour, minute, quiet):
    now = datetime(2026, 9, 26, hour, minute, tzinfo=TZ)
    assert in_quiet_hours(now, parse_quiet_hours("23:00-08:00")) is quiet


def test_daytime_quiet_hours():
    window = parse_quiet_hours("13:00-14:00")
    assert in_quiet_hours(datetime(2026, 9, 26, 13, 30, tzinfo=TZ), window)
    assert not in_quiet_hours(datetime(2026, 9, 26, 14, 0, tzinfo=TZ), window)


def test_quiet_hours_suppress_then_flag_in_the_morning(tmp_path):
    cal = source(MEETING)
    hb, notify = make(tmp_path, {"calendar": cal}, at=datetime(2026, 9, 26, 23, 30, tzinfo=TZ))
    assert asyncio.run(hb.beat()) == []
    assert notify.sent == [] and cal.calls == []  # nothing is even checked

    hb.clock = lambda: datetime(2026, 9, 27, 8, 5, tzinfo=TZ)
    asyncio.run(hb.beat())
    assert notify.sent == [("JARVIS · calendar", MEETING.text)]


# ── Failing sources ─────────────────────────────────────────────


def test_failing_source_doesnt_break_the_beat_or_leak_its_message(tmp_path, caplog):
    async def broken(now):
        raise RuntimeError("token=s3cr3t-value")

    async def hangs(now):
        await asyncio.sleep(3600)

    hb, notify = make(tmp_path, {"email": broken, "github": hangs, "calendar": source(MEETING)})
    heartbeat.SOURCE_TIMEOUT, saved = 0.05, heartbeat.SOURCE_TIMEOUT
    try:
        with caplog.at_level(logging.INFO, logger="jarvis.tasks.heartbeat"):
            asyncio.run(hb.beat())
    finally:
        heartbeat.SOURCE_TIMEOUT = saved
    assert notify.sent == [("JARVIS · calendar", MEETING.text)]
    assert "s3cr3t" not in caplog.text
    assert "email failed" in caplog.text and "github failed" in caplog.text and "calendar 1" in caplog.text


def test_unreadable_state_file_starts_afresh(tmp_path):
    (tmp_path / "heartbeat.json").write_text("{not json")
    hb, notify = make(tmp_path, {"calendar": source(MEETING)})
    asyncio.run(hb.beat())
    assert len(notify.sent) == 1


# ── Triage ──────────────────────────────────────────────────────


def test_triage_drops_soft_alerts_for_good(tmp_path):
    promo = Alert("email:work:9", "email", "New email from Shop: Sale!", soft=True)
    real = Alert("email:work:8", "email", "New email from Alice: Contract.", soft=True)
    asked = []

    async def triage(alerts):
        asked.append(alerts)
        return {real.key}

    hb, notify = make(tmp_path, {"calendar": source(MEETING), "email": source(promo, real)}, triage=triage)
    hb.seen.started.add("email")
    asyncio.run(hb.beat())
    assert [t for _, t in notify.sent] == [MEETING.text, real.text]
    assert asked == [[promo, real]]  # hard alerts never go to the model
    asyncio.run(hb.beat())
    assert len(notify.sent) == 2 and len(asked) == 1  # the dropped one isn't asked about again


def test_triage_can_mark_an_alert_urgent_for_the_voice(tmp_path):
    alarm = Alert("github:1", "github", "Security alert on jarvis: leaked key.", soft=True)
    events = []

    async def triage(alerts):
        return {alarm.key: True}

    hb, _ = make(tmp_path, {"github": source(alarm)}, triage=triage, announce=events.append)
    hb.seen.started.add("github")
    asyncio.run(hb.beat())
    assert events[0]["urgency"] == "urgent"


def test_the_triage_model_reads_numbers_kept_and_marks(monkeypatch):
    a, b, c = (Alert(f"email:{i}", "email", f"Mail {i}.", soft=True) for i in range(3))
    replies = ['{"keep": [{"n": 1, "urgent": false}, {"n": 3, "urgent": true}, {"n": 9, "urgent": true}]}',
               '{"keep": []}', "1, 3!", '{"keep": [{"n": "1"}]}']

    async def ask(prompt, system, model, **options):
        assert "1. [email] Mail 0." in prompt and '"keep"' in system
        return replies.pop(0), 0.0

    monkeypatch.setattr("jarvis.brain.sdk.ask", ask)
    assert asyncio.run(heartbeat.model_triage([a, b, c])) == {a.key: False, c.key: True}  # 9 isn't an item
    assert asyncio.run(heartbeat.model_triage([a, b, c])) == {}  # none kept
    for _ in range(2):  # not JSON matching the schema: an error, so the heartbeat keeps everything
        with pytest.raises(ValueError):
            asyncio.run(heartbeat.model_triage([a, b, c]))


def test_triage_failure_keeps_everything(tmp_path):
    mail = Alert("email:work:8", "email", "New email from Alice: Contract.", soft=True)

    async def triage(alerts):
        raise RuntimeError("usage limit")

    hb, notify = make(tmp_path, {"email": source(mail)}, triage=triage)
    hb.seen.started.add("email")
    asyncio.run(hb.beat())
    assert notify.sent == [("JARVIS · email", mail.text)]


# ── Sources ─────────────────────────────────────────────────────


def test_calendar_soon_picks_timed_events_within_the_lead(monkeypatch):
    from ai_armory.toolsets.google import calendar, common

    def at(**later) -> str:  # in JARVIS_TIMEZONE, whichever that is
        return (AFTERNOON + timedelta(**later)).isoformat()

    events = {
        "work": [
            {"account": "work", "title": "Standup", "start": at(minutes=12)},
            {"account": "work", "title": "Later", "start": at(minutes=90)},
            {"account": "work", "title": "Started", "start": at(minutes=-10)},
            {"account": "work", "title": "Holiday", "start": "2026-09-26"},
        ],
        "personal": [{"account": "personal", "title": "Call mum", "start": at(seconds=20)}],
    }
    monkeypatch.setattr(common, "connected", lambda: ["work", "personal"])
    monkeypatch.setattr(calendar, "calendar_events", lambda days, account: events[account])
    alerts = asyncio.run(heartbeat.calendar_soon(AFTERNOON, lead=30))
    assert [a.text for a in alerts] == ["Standup starts in 12 minutes.", "Call mum starts now."]
    assert [round(a.at - AFTERNOON.timestamp()) for a in alerts] == [720, 20]  # when each starts, for the voice


def test_no_connected_google_accounts_is_simply_empty(monkeypatch):
    from ai_armory.toolsets.google import common

    monkeypatch.setattr(common, "connected", lambda: [])
    assert asyncio.run(heartbeat.important_mail(AFTERNOON)) == []
    assert asyncio.run(heartbeat.calendar_soon(AFTERNOON)) == []


def test_one_broken_google_account_doesnt_hide_the_other(monkeypatch):
    from ai_armory.toolsets.google import common, gmail

    def search(query, account, max_results):
        if account == "work":
            raise RuntimeError("The work Google account is not connected yet.")
        return [{"account": "personal", "id": "m1", "from": "Alice Shaw <p@example.com>", "subject": "Dinner?"}]

    monkeypatch.setattr(common, "connected", lambda: ["work", "personal"])
    monkeypatch.setattr(gmail, "gmail_search", search)
    alerts = asyncio.run(heartbeat.important_mail(AFTERNOON))
    assert [(a.key, a.text, a.soft) for a in alerts] == [("email:personal:m1", "New email from Alice Shaw: Dinner?", True)]


def test_github_news(monkeypatch):
    async def overview():
        return {
            "review_requests": [{"repository": {"name": "pdf-tools", "nameWithOwner": "a/pdf-tools"}, "title": "Add OCR",
                                 "number": 12, "url": "https://github.com/a/pdf-tools/pull/12"}],
            "unread_notifications": [
                {"repo": "a/atlas", "title": "Crash on login", "type": "Issue", "reason": "mention"},
                {"repo": "a/atlas", "title": "Bump deps", "type": "PullRequest", "reason": "subscribed"},
            ],
        }

    monkeypatch.setattr(heartbeat, "github_overview", overview)
    alerts = asyncio.run(heartbeat.github_news(AFTERNOON))
    assert [(a.text, a.soft) for a in alerts] == [
        ("pdf-tools pull request 12 is waiting for your review: Add OCR.", False),
        ("You were mentioned on atlas: Crash on login.", True),
    ]


def test_task_changes():
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

    alerts = asyncio.run(heartbeat.task_changes(Store(), minutes=15)(AFTERNOON))
    assert [(a.task_id, a.text) for a in alerts] == [
        (1, "Task 1, Retry logic, is still waiting for your answer: Which DB?"),
        (3, "Task 3, Auth, stopped. JARVIS restarted while it was running. Reply to carry on."),
    ]


def test_heartbeat_only_reads():
    """The heartbeat must never send, create, start or run anything on its own."""
    code = open(heartbeat.__file__).read()
    for write in ("gmail_create_draft", "gmail_modify", "calendar_create_event", "sheets_write", "sheets_append",
                  "sheets_add_tab", "sheets_delete_tab", "sheets_format", "docs_beautify", "docs_format", "docs_replace_text", "docs_insert",
                  "docs_insert_image", "docs_delete", "docs_draft_delete", "sheets_draft_delete_tab",
                  "calendar_draft_invite", "calendar_send_invite", "calendar_update_event", "calendar_draft_update",
                  "calendar_send_update", "calendar_draft_respond", "calendar_send_response", "calendar_draft_delete",
                  "calendar_delete_event", "gmail_draft_send", "gmail_draft_reply", "gmail_send",
                  "task_start", "task_reply", "task_stop", "task_discard", "task_approve",
                  "mac_open", "mac_run_shortcut", "mac_set_volume", "chat_mark_read", "chat_draft", "chat_send", "_exec",
                  "subprocess", '"Bash"'):
        assert write not in code, write


# ── Google Chat ─────────────────────────────────────────────────

CHAT_NEWS = [
    {"account": "work", "space": "spaces/DM1", "name": "Alice Carter", "kind": "dm", "from": "Alice Carter",
     "count": 1, "latest": "spaces/DM1/messages/1", "text": "Can you join at four? The salary numbers for the team are in the doc."},
    {"account": "work", "space": "spaces/DEV", "name": "Platform Dev", "kind": "space", "from": "Bob Nguyen",
     "count": 2, "latest": "spaces/DEV/messages/3", "text": "@Alex please review the player fix"},
]


@pytest.fixture
def chat_news(monkeypatch):
    from ai_armory.toolsets.google import chat, common

    monkeypatch.setattr(common, "connected", lambda: ["work", "personal"])
    monkeypatch.setattr(chat, "news",lambda label: [dict(n) for n in CHAT_NEWS if n["account"] == label])


def test_chat_alerts_say_who_and_where_but_only_the_voice_hears_what(chat_news):
    alerts = asyncio.run(heartbeat.chat_news(AFTERNOON))
    assert [(a.key, a.source, a.text, a.soft) for a in alerts] == [
        ("chat:work:spaces/DM1/messages/1", "chat", "New Chat message from Alice Carter.", True),
        ("chat:work:spaces/DEV/messages/3", "chat", "Bob Nguyen mentioned you in Platform Dev on Chat.", True)]
    assert alerts[0].spoken == ("New Chat message from Alice Carter: Can you join at four? The salary numbers "
                                "for the team are…")
    assert alerts[1].spoken == "Bob Nguyen mentioned you in Platform Dev on Chat: @Alex please review the player fix."
    assert "join at four" not in alerts[0].text


def test_chat_text_never_reaches_the_lock_screen(tmp_path, chat_news):
    events, phone = [], Pusher()
    hb, notify = make(tmp_path, {"chat": heartbeat.chat_news}, announce=events.append, push=phone)
    assert asyncio.run(hb.beat()) == []  # the first answer only records what's there already
    CHAT_NEWS.append({"account": "work", "space": "spaces/DM2", "name": "Sam Park", "kind": "dm",
                      "from": "Sam Park", "count": 3, "latest": "spaces/DM2/messages/9", "text": "Secret plan"})
    try:
        asyncio.run(hb.beat())
    finally:
        CHAT_NEWS.pop()
    assert notify.sent == [("JARVIS · chat", "3 new Chat messages from Sam Park.")]
    assert [(title, body) for title, body, _, _ in phone.sent] == [("Chat", "3 new Chat messages from Sam Park.")]
    assert events[0]["text"] == "Heads up. 3 new Chat messages from Sam Park: Secret plan."


def test_the_chat_source_is_there_only_when_an_account_has_chat(monkeypatch):
    from jarvis.integrations import accounts

    assert "chat" in heartbeat.sources_on(None)
    monkeypatch.setattr(accounts, "load", lambda: accounts.Accounts([accounts.Account("work", "me@acme.example")], "work"))
    assert "chat" not in heartbeat.sources_on(None)
    assert next(s for s in heartbeat.described()["sources"] if s["id"] == "chat")["ready"] is False


# ── Sources, as each user has them ──────────────────────────────


def stored() -> dict:
    import json

    from jarvis.state import settings

    return json.loads(settings.get(heartbeat.SETTINGS_KEY) or "{}")


def test_with_nothing_changed_every_built_in_source_is_on_as_before(own_profile, monkeypatch):
    monkeypatch.setitem(heartbeat.KINDS, "chat", replace(heartbeat.KINDS["chat"], ready=lambda: True))
    on = heartbeat.sources_on(None)
    assert list(on) == ["calendar", "email", "github", "chat", "tasks", "usage", "signins"]
    assert on["email"].keywords == {"query": "is:unread is:important newer_than:1d"}
    assert on["calendar"].keywords == {"lead": config.HEARTBEAT_EVENT_MINUTES}
    assert on["github"] is heartbeat.github_news
    assert stored() == {}
    assert [heartbeat.baseline(n) for n in ("email", "github", "chat", "calendar", "tasks")] == [True] * 3 + [False] * 2


def test_switching_sources_and_changing_settings_keeps_only_what_differs(own_profile):
    heartbeat.change_source("github", on=False)
    heartbeat.change_source("email", values={"query": "  from:boss   is:unread "})
    c = heartbeat.change_source("calendar", values={"minutes": "10"})
    assert (c.on, c.settings) == (True, {"minutes": "10"})
    on = heartbeat.sources_on(None)
    assert "github" not in on
    assert on["email"].keywords == {"query": "from:boss is:unread"}
    assert on["calendar"].keywords == {"lead": 10}
    assert stored() == {"github": {"on": False}, "email": {"query": "from:boss is:unread"}, "calendar": {"minutes": "10"}}

    heartbeat.change_source("github", on=True)
    heartbeat.change_source("email", values={"query": ""})  # back to the default
    heartbeat.change_source("calendar", values={"minutes": str(config.HEARTBEAT_EVENT_MINUTES)})
    assert stored() == {}


@pytest.mark.parametrize("id_, values, said", [
    ("calendar", {"minutes": "soon"}, "isn't a whole number"),
    ("calendar", {"minutes": "0"}, "from 1 to 240"),
    ("email", {"query": "x" * 301}, "300 characters"),
    ("email", {"colour": "red"}, "Mail has no setting called colour"),
    ("rss", {}, "no heartbeat source called rss"),
])
def test_settings_a_source_cant_take_are_refused_and_nothing_is_kept(own_profile, id_, values, said):
    with pytest.raises(ValueError, match=said):
        heartbeat.change_source(id_, values=values)
    assert stored() == {}


def test_another_gmail_search_is_added_checked_and_removed(own_profile):
    c = heartbeat.add_source("email", "Invoices", {"query": "from:billing@acme.example"}, "by voice")
    assert (c.id, c.title, c.on, c.added, c.settings) == (
        "email:invoices", "Invoices", True, "by voice", {"query": "from:billing@acme.example"})
    assert heartbeat.add_source("email", "invoices!", {"query": "subject:invoice"}, "by voice").id == "email:invoices-2"
    on = heartbeat.sources_on(None)
    assert on["email:invoices"].keywords == {"query": "from:billing@acme.example"}
    assert on["email"].keywords == {"query": heartbeat.MAIL_QUERY}  # the built-in one is as it was
    assert heartbeat.baseline("email:invoices")

    heartbeat.change_source("email:invoices", on=False)
    assert "email:invoices" not in heartbeat.sources_on(None)
    heartbeat.remove_source("email:invoices")
    assert [c.id for c in heartbeat.lineup() if c.added] == ["email:invoices-2"]
    with pytest.raises(ValueError, match="built in: switch it off"):
        heartbeat.remove_source("email")


@pytest.mark.parametrize("kind, title, values, said", [
    ("github", "Work repos", {}, "Only these can be added more than once: email"),
    ("email", "  ", {"query": "from:x"}, "title of up to 40"),
    ("email", "Invoices", {}, "Say its Gmail search"),
])
def test_only_a_kind_that_allows_more_is_added_and_only_whole(own_profile, kind, title, values, said):
    with pytest.raises(ValueError, match=said):
        heartbeat.add_source(kind, title, values, "by voice")
    assert stored() == {}


def test_added_sources_are_capped(own_profile, monkeypatch):
    monkeypatch.setattr(heartbeat, "MAX_ADDED", 2)
    for title in ("One", "Two"):
        heartbeat.add_source("email", title, {"query": f"label:{title}"}, "in the panel")
    with pytest.raises(ValueError, match="2 added sources already"):
        heartbeat.add_source("email", "Three", {"query": "label:three"}, "in the panel")


def test_a_kind_registered_elsewhere_is_checked_and_set_up_like_the_rest(own_profile, monkeypatch, caplog):
    def rss(settings, context):
        async def check(now):
            return [Alert(f"rss:{settings['feed']}", "rss", "A new post.")]
        return check

    monkeypatch.setitem(heartbeat.KINDS, "rss", heartbeat.Kind(
        "rss", "Feeds", "New posts on a feed", rss, (heartbeat.Setting("feed", "Feed", "https://a.example/feed", str),),
        on=False, more=True))
    assert "rss" not in heartbeat.sources_on(None)  # off unless the user switches it on
    heartbeat.change_source("rss", on=True)
    heartbeat.add_source("rss", "B's blog", {"feed": "https://b.example/feed"}, "by the reading skill")
    on = heartbeat.sources_on(None)
    assert asyncio.run(on["rss:b-s-blog"](AFTERNOON))[0].key == "rss:https://b.example/feed"
    assert asyncio.run(on["rss"](AFTERNOON))[0].key == "rss:https://a.example/feed"
    assert [k["name"] for k in heartbeat.described()["kinds"]] == ["email", "rss"]

    # Its module not loaded, or its source not set up: the beat goes on without it, and what the user set is kept
    monkeypatch.setitem(heartbeat.KINDS, "rss", replace(heartbeat.KINDS["rss"], make=lambda s, c: 1 / 0))
    assert "rss" not in heartbeat.sources_on(None) and "couldn't be set up: ZeroDivisionError" in caplog.text
    monkeypatch.delitem(heartbeat.KINDS, "rss")
    assert not any(c.id.startswith("rss") for c in heartbeat.lineup())
    assert "rss:b-s-blog" in stored()


def test_an_unreadable_setting_means_the_defaults(own_profile, caplog):
    from jarvis.state import settings

    settings.put(heartbeat.SETTINGS_KEY, "{not json")
    assert {"calendar", "email", "github"} <= set(heartbeat.sources_on(None))
    assert "heartbeat sources unreadable" in caplog.text


def test_each_beat_checks_the_sources_on_then(tmp_path, own_profile):
    """A source switched off, or added, in the panel counts from the next beat; one added only records what's there."""
    mail = [Alert("email:work:m1", "email", "New email from Ann: Invoice 7.", soft=True)]

    def lineup():
        return {c.id: source(*mail) for c in heartbeat.lineup() if c.on and c.kind.name == "email"}

    hb, notify = make(tmp_path, lineup)
    hb.seen.started.add("email")
    assert [a.key for a in asyncio.run(hb.beat())] == ["email:work:m1"]
    heartbeat.change_source("email", on=False)
    heartbeat.add_source("email", "Invoices", {"query": "subject:invoice"}, "in the panel")
    mail[:] = [Alert("email:work:m2", "email", "New email from Ann: Invoice 8.", soft=True)]
    assert asyncio.run(hb.beat()) == []  # the added source's first answer
    mail[:] = [Alert("email:work:m3", "email", "New email from Ann: Invoice 9.", soft=True)]
    assert [a.key for a in asyncio.run(hb.beat())] == ["email:work:m3"]
    assert notify.sent[-1] == ("JARVIS · email", "New email from Ann: Invoice 9.")


def test_the_voice_tools_change_the_same_sources(own_profile):
    from jarvis import tools

    def run(name, args=None):
        return asyncio.run(tools.run(name, args or {}))

    listed, _ = run("heartbeat_sources")
    assert '"id": "email"' in listed and '"can_add"' in listed
    assert run("heartbeat_source_change", {"id": "github", "on": False}) == ("GitHub is off.", False)
    said, error = run("heartbeat_source_add", {"kind": "email", "title": "Invoices",
                                               "settings": {"query": "from:billing"}, "for_skill": "billing"})
    assert (said, error) == ("Added Invoices (id email:invoices): checked from the next beat.", False)
    assert heartbeat.lineup()[-1].added == "by the billing skill"
    said, error = run("heartbeat_source_change", {"id": "calendar", "settings": {"minutes": "never"}})
    assert error and "isn't a whole number" in said
    assert run("heartbeat_source_remove", {"id": "email:invoices"}) == ("Took email:invoices out of the heartbeat.", False)
    assert stored() == {"github": {"on": False}}
