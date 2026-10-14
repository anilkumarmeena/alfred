"""Tests for jarvis.brain.usage: shared usage-limit state and its effects: lighter model, held runs, warnings."""

import asyncio
import json
from datetime import datetime

import pytest
from claude_agent_sdk import RateLimitEvent, RateLimitInfo

from jarvis import tools
from jarvis.brain import sdk, usage
from jarvis.core import profile
from jarvis.core.config import FAST_MODEL, HELPER_MODEL

NOW = datetime(2026, 10, 6, 13, 0, tzinfo=profile.tz()).timestamp()  # a Tuesday, 13:00


@pytest.fixture(autouse=True)
def usage_file(tmp_path, monkeypatch):
    monkeypatch.setattr(usage, "USAGE_FILE", tmp_path / "usage.json")
    return tmp_path / "usage.json"


def said(status: str, kind: str | None = "five_hour", hours: float | None = 2, utilization: float | None = None,
         overage: str | None = None, now: float = NOW) -> None:
    usage.record(RateLimitInfo(status=status, resets_at=int(now + hours * 3600) if hours is not None else None,
                               rate_limit_type=kind, utilization=utilization, overage_status=overage), now=now)


def test_nothing_known_is_nothing_held():
    assert usage.windows(NOW) == {} and usage.out(NOW) is None and usage.hold_until(NOW) is None
    assert usage.available("claude-opus-5-5", NOW) == "claude-opus-5-5"
    assert "hasn't reported anything" in usage.said(NOW)


def test_each_window_keeps_its_latest_word_until_it_resets(usage_file):
    said("allowed_warning", utilization=0.82)
    said("allowed", kind="seven_day", hours=72, utilization=0.4)
    said("rejected", hours=2)  # later, the five-hour window ran out
    assert set(usage.windows(NOW)) == {"five_hour", "seven_day"}
    assert usage.windows(NOW)["five_hour"]["status"] == "rejected"
    assert usage.windows(NOW + 3 * 3600).keys() == {"seven_day"}  # the five-hour one has reset
    assert json.loads(usage_file.read_text())["seven_day"]["utilization"] == 0.4


def test_a_used_up_window_holds_scheduled_runs_until_its_reset():
    said("rejected", hours=2)
    assert usage.out(NOW)[0] == "five_hour"
    assert usage.hold_until(NOW) == NOW + 2 * 3600
    assert usage.hold_until(NOW + 2 * 3600 + 1) is None


def test_extra_usage_carries_on_past_a_used_up_window():
    said("rejected", hours=2, overage="allowed")
    assert usage.out(NOW) is None and usage.hold_until(NOW) is None


def test_the_five_hour_window_nearly_used_up_holds_scheduled_runs(monkeypatch):
    said("allowed_warning", utilization=0.85)
    assert usage.hold_until(NOW) is None
    said("allowed_warning", utilization=0.93)
    assert usage.hold_until(NOW) == NOW + 2 * 3600
    said("allowed_warning", kind="seven_day", hours=50, utilization=0.97)  # a weekly warning only warns
    assert usage.hold_until(NOW) == NOW + 2 * 3600


def test_used_up_with_no_reset_time_holds_for_half_an_hour():
    said("rejected", kind=None, hours=None)
    assert usage.hold_until(NOW) == NOW + usage.UNKNOWN_RESET
    assert usage.windows(NOW + usage.UNKNOWN_RESET + 1) == {}


def test_a_models_own_limit_moves_to_a_lighter_model():
    said("rejected", kind="seven_day_opus", hours=48)
    assert usage.out(NOW) is None  # other models carry on, so nothing is held
    assert usage.available("claude-opus-5-5", NOW) == HELPER_MODEL
    assert usage.available(HELPER_MODEL, NOW) == HELPER_MODEL
    said("rejected", kind="seven_day_sonnet", hours=48)
    assert usage.available("claude-opus-5-5", NOW) == FAST_MODEL  # and on down, when Sonnet's is out too


def test_warnings_are_keyed_per_window_so_each_is_said_once():
    said("allowed_warning", utilization=0.91)
    [(key, text)] = usage.alerts(NOW)
    assert key == f"usage:five_hour:{int(NOW + 7200)}:close"
    assert text == "JARVIS has used 91% of Claude's five-hour limit; it resets at 3 PM."
    said("rejected", kind="seven_day_opus", hours=48)
    texts = dict(usage.alerts(NOW))
    assert texts[f"usage:seven_day_opus:{int(NOW + 48 * 3600)}:out"] == (
        "Claude's weekly Opus limit is used up, until it resets on Thursday at 1 PM; JARVIS uses Sonnet meanwhile.")


def test_usage_left_says_what_is_known(monkeypatch):
    monkeypatch.setattr(usage.time, "time", lambda: NOW)
    said("allowed_warning", utilization=0.6)
    said("rejected", kind="seven_day", hours=30)
    content, error = asyncio.run(tools.run("usage_left", {}))
    found = json.loads(content)
    assert not error
    assert found["usage"] == ("60% of the five-hour limit is used, resetting at 3 PM; the weekly limit is used up, "
                              "resetting on Wednesday at 7 PM (as of 13:00).")
    assert found["scheduled_runs"] == "Scheduled tasks and checks wait until it resets on Wednesday at 7 PM."


def test_the_limit_watch_records_what_the_cli_says(usage_file):
    watch = sdk.LimitWatch("test")
    watch.saw(RateLimitEvent(RateLimitInfo(status="allowed_warning", rate_limit_type="five_hour", utilization=0.8,
                                           resets_at=int(NOW + 600)), "u1", "s1"))
    assert json.loads(usage_file.read_text())["five_hour"]["utilization"] == 0.8
    assert watch.describe("API Error: usage limit reached") == "the Claude usage limit"


def test_a_window_extra_usage_carries_on_past_isnt_what_stopped_a_run(usage_file):
    watch = sdk.LimitWatch("test")
    watch.saw(RateLimitEvent(RateLimitInfo(status="rejected", rate_limit_type="five_hour", resets_at=int(NOW + 600),
                                           overage_status="allowed"), "u1", "s1"))
    assert watch.rejected is None and watch.describe("tool failed") is None
    watch.saw(RateLimitEvent(RateLimitInfo(status="rejected", rate_limit_type="five_hour", resets_at=int(NOW + 600)),
                             "u2", "s1"))
    assert watch.rejected is not None


def test_an_unreadable_file_is_nothing_known(usage_file):
    usage_file.write_text("{not json")
    assert usage.windows(NOW) == {}
    said("allowed")  # and it's written afresh
    assert "five_hour" in usage.windows(NOW)
