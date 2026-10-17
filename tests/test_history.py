"""Tests for jarvis.state.history: days, a day's exchanges and search over the log; the passcode-gated panel."""

import asyncio
import json
from datetime import datetime

import pytest

from conftest import use_profile
from jarvis import panel, server
from jarvis.state import history, memory
from jarvis.storage import profile

DAY = """# Conversations, Monday 5 October 2026

[09:00] Alex: what's on today?
JARVIS: Two meetings, Sir.

[09:05] Alex: run the tests
then tell me
(Approved: make test)
JARVIS: All 40 pass. Task 12 is next. (interrupted)

[09:06] Alex: never mind
JARVIS: (interrupted before replying)

[10:30] JARVIS (announced): Task 12, the login page, is done.

"""


@pytest.fixture
def logs(tmp_path, monkeypatch):
    folder = tmp_path / "conversations"
    use_profile(monkeypatch, tmp_path)
    use_profile(monkeypatch, name="Alex")
    folder.mkdir()
    return folder


def write(folder, day: str, text: str) -> None:
    (folder / f"{day}.md").write_text(text)


def test_a_day_reads_as_who_said_what_newest_first(logs):
    write(logs, "2026-10-05", DAY)
    found = history.day("2026-10-05")
    assert found["newer"] is None and found["older"] is None
    announced, cut, interrupted, first = found["entries"]
    assert first == {"n": 1, "time": "09:00", "kind": "exchange", "who": "Alex", "said": "what's on today?",
                     "approved": [], "reply": "Two meetings, Sir.", "interrupted": False}
    assert interrupted == {"n": 2, "time": "09:05", "kind": "exchange", "who": "Alex", "said": "run the tests\nthen tell me",
                           "approved": ["make test"], "reply": "All 40 pass. Task 12 is next.", "interrupted": True}
    assert (cut["said"], cut["reply"], cut["interrupted"]) == ("never mind", "", True)
    assert announced == {"n": 4, "time": "10:30", "kind": "announcement", "said": "Task 12, the login page, is done."}


def test_what_log_exchange_writes_reads_back_the_same(logs):
    memory.log_exchange("  where are the logs?  ", "In data.", approved=["ls data"])
    memory.log_exchange("and the tests?", "", interrupted=True)
    memory.log_announcement("Task 3 is done.")
    today = f"{datetime.now(profile.tz()):%Y-%m-%d}"
    entries = history.day(today)["entries"]
    assert [e["kind"] for e in entries] == ["announcement", "exchange", "exchange"]
    assert (entries[2]["who"], entries[2]["said"], entries[2]["approved"], entries[2]["reply"]) == \
        ("Alex", "where are the logs?", ["ls data"], "In data.")
    assert (entries[1]["reply"], entries[1]["interrupted"]) == ("", True)
    listed = history.days()
    assert listed["today"] == today and listed["days"][0]["date"] == today


def test_days_are_listed_newest_first_a_page_at_a_time(logs):
    write(logs, "2026-10-05", DAY)
    write(logs, "2026-10-03", "[22:00] Alex: good night\nJARVIS: Good night.\n\n")
    write(logs, "2026-10-01", "[08:00] Alex: morning\nJARVIS: Morning.\n\n")
    write(logs, "notes.md", "not a day")
    write(logs, "2026-13-45", "not a date")
    page = history.days(limit=2)
    assert [d["date"] for d in page["days"]] == ["2026-10-05", "2026-10-03"] and page["more"]
    first = page["days"][0]
    assert (first["count"], first["exchanges"], first["first"], first["last"]) == (4, 3, "09:00", "10:30")
    assert first["preview"] == "never mind"  # the latest thing the user said
    older = history.days(before="2026-10-03", limit=2)
    assert [d["date"] for d in older["days"]] == ["2026-10-01"] and not older["more"]


def test_a_day_names_the_days_either_side_and_an_empty_one_is_empty(logs):
    for day in ("2026-10-01", "2026-10-03", "2026-10-05"):
        write(logs, day, "[08:00] Alex: hi\nJARVIS: Hello.\n\n")
    middle = history.day("2026-10-03")
    assert (middle["newer"], middle["older"]) == ("2026-10-05", "2026-10-01")
    gap = history.day("2026-10-04")
    assert gap["entries"] == [] and (gap["newer"], gap["older"]) == ("2026-10-05", "2026-10-03")


def test_a_long_preview_is_shortened(logs):
    write(logs, "2026-10-05", f"[08:00] Alex: {'word ' * 60}\nJARVIS: Yes.\n\n")
    preview = history.days()["days"][0]["preview"]
    assert len(preview) == history.PREVIEW_CHARS and preview.endswith("…")


@pytest.mark.parametrize("bad", ["yesterday", "2026-10-5", "../secrets", "2026-02-30", "", 7])
def test_only_real_dates_are_read(logs, bad):
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        history.day(bad)


def test_a_line_in_no_known_shape_is_kept_as_a_note(logs):
    write(logs, "2026-10-05", "# Conversations\n\n[09:00] a hand-written line with no speaker\n\n")
    assert history.day("2026-10-05")["entries"] == [
        {"n": 1, "time": "09:00", "kind": "note", "said": "a hand-written line with no speaker"}]


def test_search_finds_every_word_in_any_case_across_days_newest_first(logs):
    write(logs, "2026-10-05", DAY)
    write(logs, "2026-10-01", "[08:00] Alex: are the TESTS green?\nJARVIS: They pass.\n\n")
    found = history.search("tests  PASS")
    assert found["text"] == "tests pass" and not found["more"]
    assert [(h["date"], h["time"]) for h in found["hits"]] == [("2026-10-05", "09:05"), ("2026-10-01", "08:00")]
    assert history.search("login")["hits"][0]["kind"] == "announcement"
    assert history.search("renders")["hits"] == []
    with pytest.raises(ValueError, match="what to look for"):
        history.search("   ")


def test_search_stops_at_its_limit_and_says_there_is_more(logs):
    write(logs, "2026-10-05", "".join(f"[09:{m:02d}] Alex: ping {m}\nJARVIS: Pong.\n\n" for m in range(5)))
    found = history.search("ping", limit=3)
    assert [h["said"] for h in found["hits"]] == ["ping 4", "ping 3", "ping 2"] and found["more"]


def test_no_log_yet_lists_nothing(tmp_path, monkeypatch):
    use_profile(monkeypatch, tmp_path / "none")
    assert history.days()["days"] == [] and history.search("x")["hits"] == []
    assert history.day("2026-10-05")["entries"] == []


# ── The panel ───────────────────────────────────────────────────


class Request:
    remote = "127.0.0.1"

    def __init__(self, action: str, body: dict):
        self.match_info, self.body = {"action": action}, body

    async def json(self):
        return self.body


def reply(action: str, body: dict) -> tuple[int, dict]:
    r = asyncio.run(server.panel_action(Request(action, body)))
    return r.status, json.loads(r.text)


@pytest.fixture
def site(logs, monkeypatch):
    monkeypatch.setenv("JARVIS_PASSCODE", "123456")  # no login yet: the first-setup passcode
    monkeypatch.setattr(server, "_failures", [])
    write(logs, "2026-10-05", DAY)
    write(logs, "2026-10-01", "[08:00] Alex: morning\nJARVIS: Morning.\n\n")


def test_history_is_behind_the_passcode(site):
    for action in ("conversations", "conversation", "conversation_search"):
        assert action in panel.ACTIONS
        assert reply(action, {"passcode": "000000", "date": "2026-10-05", "text": "x"}) == (401, {"error": "Wrong passcode."})


def test_the_panel_lists_reads_and_searches_past_conversations(site):
    status, body = reply("conversations", {"passcode": "123456"})
    assert status == 200 and [d["date"] for d in body["result"]["days"]] == ["2026-10-05", "2026-10-01"]
    older = reply("conversations", {"passcode": "123456", "before": "2026-10-05"})[1]["result"]
    assert [d["date"] for d in older["days"]] == ["2026-10-01"]
    day = reply("conversation", {"passcode": "123456", "date": "2026-10-05"})[1]["result"]
    assert len(day["entries"]) == 4 and day["older"] == "2026-10-01"
    hits = reply("conversation_search", {"passcode": "123456", "text": "morning"})[1]["result"]["hits"]
    assert [(h["date"], h["said"]) for h in hits] == [("2026-10-01", "morning")]


def test_the_panel_refuses_a_bad_date_or_an_empty_search(site):
    assert reply("conversation", {"passcode": "123456", "date": "../../secrets/x"})[0] == 400
    assert reply("conversations", {"passcode": "123456", "before": "soon"})[0] == 400
    assert reply("conversation_search", {"passcode": "123456", "text": " "}) == (400, {"error": "Say what to look for."})
