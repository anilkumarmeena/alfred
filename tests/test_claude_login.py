"""Tests for the Claude login JARVIS runs on: the user's own unless JARVIS_CLAUDE_CONFIG_DIR names one of JARVIS's."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from jarvis import claude_login, config
from jarvis.brain import sdk
from jarvis.config import ROOT
from jarvis.llm import claude, claude_logins, usage


def config_dirs(**env: str) -> list[str]:
    """CLAUDE_OWN_CONFIG_DIR and CLAUDE_CONFIG_DIR as a fresh process reads them (set-but-empty = unset, not .env)."""
    code = "from jarvis import config; print(config.CLAUDE_OWN_CONFIG_DIR); print(config.CLAUDE_CONFIG_DIR)"
    env = os.environ | {"JARVIS_CLAUDE_CONFIG_DIR": "", "CLAUDE_CONFIG_DIR": ""} | env
    return subprocess.run([sys.executable, "-c", code], env=env, cwd=ROOT, capture_output=True, text=True,
                          check=True).stdout.splitlines()


def test_by_default_the_cli_gets_no_config_dir_so_it_finds_the_users_login(monkeypatch):
    monkeypatch.setattr(config, "CLAUDE_OWN_CONFIG_DIR", "")
    assert "CLAUDE_CONFIG_DIR" not in sdk.sdk_options().env  # even ~/.claude would miss the macOS Keychain login


def test_a_login_of_jarvis_own_is_passed_on(monkeypatch):
    monkeypatch.setattr(config, "CLAUDE_OWN_CONFIG_DIR", "/j/secrets/claude")
    assert sdk.sdk_options().env["CLAUDE_CONFIG_DIR"] == "/j/secrets/claude"


def test_the_cli_folder_is_the_users_unless_jarvis_has_its_own():
    assert config_dirs() == ["", str(Path.home() / ".claude")]
    assert config_dirs(CLAUDE_CONFIG_DIR="/u/claude") == ["", "/u/claude"]
    own = str(ROOT / "secrets/claude")
    assert config_dirs(JARVIS_CLAUDE_CONFIG_DIR="secrets/claude") == [own, own]  # relative to the checkout
    assert config_dirs(JARVIS_CLAUDE_CONFIG_DIR="/abs/claude") == ["/abs/claude", "/abs/claude"]


# ── Named logins (core.claude_logins) ──



@pytest.fixture
def logins_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CLAUDE_LOGINS_DIR", tmp_path / "claude-logins")
    monkeypatch.setattr(config, "CLAUDE_OWN_CONFIG_DIR", "")
    return tmp_path / "claude-logins"


class Info:
    """A rate-limit event's info: the weekly limit used up."""
    rate_limit_type, status, resets_at, utilization, overage_status = "seven_day", "rejected", 9e9, 1.0, None


def test_jarvis_runs_on_the_first_login_by_priority(logins_dir):
    assert claude_logins.order() == [""] and claude_logins.active() == ""
    claude_logins.make("work")
    assert claude_logins.order() == ["", "work"] and claude_logins.active() == ""  # a new login goes last
    claude_logins.set_order(["work", ""])
    assert claude_logins.active() == "work"
    assert sdk.sdk_options().env["CLAUDE_CONFIG_DIR"] == str(logins_dir / "work")
    claude_logins.set_order(["", "work"])
    assert "CLAUDE_CONFIG_DIR" not in sdk.sdk_options().env


def test_the_order_has_to_name_every_login_once(logins_dir):
    claude_logins.make("work")
    for bad in (["work"], ["", "work", "work"], ["", "nope"]):
        with pytest.raises(ValueError):
            claude_logins.set_order(bad)


def test_a_call_can_name_its_login(logins_dir):
    claude_logins.make("work")
    assert sdk.sdk_options(login="work").env["CLAUDE_CONFIG_DIR"] == str(logins_dir / "work")
    claude_logins.set_order(["work", ""])
    assert "CLAUDE_CONFIG_DIR" not in sdk.sdk_options(login="").env


def test_names_are_checked_and_unknown_ones_refused(logins_dir):
    for bad in ("default", "order", "Work Stuff", "../x", "", "-x"):
        with pytest.raises(ValueError):
            claude_logins.valid(bad)
    assert claude_logins.valid(" Work ") == "work"
    with pytest.raises(ValueError):
        claude_logins.known("nope")


def test_an_environment_names_its_login(logins_dir):
    work = claude_logins.make("work")
    assert claude_logins.of_env({}) == "" and claude_logins.of_env({"CLAUDE_CONFIG_DIR": str(work)}) == "work"
    assert claude_logins.of_env({"CLAUDE_CONFIG_DIR": "/elsewhere"}) is None


def test_any_login_but_the_macs_own_can_be_removed(logins_dir):
    claude_logins.make("work")
    claude_logins.set_order(["work", ""])
    claude_logins.note("work", True, "fine")
    claude_logins.remove("work")
    assert claude_logins.order() == [""] and claude_logins.active() == "" and "work" not in claude_logins.checks()
    with pytest.raises(ValueError):
        claude_logins.remove("")  # the terminal uses it: it always stays
    with pytest.raises(ValueError):
        claude_logins.remove("nope")


def test_the_macs_own_login_is_never_signed_out(logins_dir, monkeypatch):
    calls = []
    monkeypatch.setattr(claude_login.subprocess, "run", lambda *a, **k: calls.append(a))
    with pytest.raises(ValueError):
        claude_login.sign_out("")
    assert not calls and claude_logins.order() == [""]


def test_a_used_up_limit_moves_jarvis_down_the_order_until_it_resets(logins_dir, monkeypatch, tmp_path):
    monkeypatch.setattr(usage, "USAGE_FILE", tmp_path / "usage.json")
    claude_logins.make("work")
    usage.record(Info(), login="")
    assert claude_logins.active() == "work"
    assert "weekly limit is used up" in claude_logins.skipped("")
    assert usage.windows() == {}  # the work login's own limits: none known
    assert any(key.startswith("usage:login:") for key, _ in usage.alerts())  # a notice says so
    assert claude_logins.active(now=9e9 + 1) == ""  # past the reset: back to the first


def test_with_every_login_used_up_jarvis_stays_on_the_first(logins_dir, monkeypatch, tmp_path):
    monkeypatch.setattr(usage, "USAGE_FILE", tmp_path / "usage.json")
    claude_logins.make("work")
    usage.record(Info(), login="")
    usage.record(Info(), login="work")
    assert claude_logins.active() == "" and usage.hold_until()  # scheduled runs wait for its reset


def test_a_login_that_fails_to_authenticate_is_passed_over_until_it_works_again(logins_dir):
    claude_logins.make("work")
    watch = claude.LimitWatch("test", login="")
    watch.describe("Failed to authenticate: OAuth session expired and could not be refreshed")
    assert claude_logins.active() == "work" and "sign-in" in claude_logins.skipped("")
    watch.describe("Some other error")
    claude_logins.note("", True, "fine")  # signed in again and checked
    assert claude_logins.active() == ""


def test_usage_is_recorded_under_the_login_a_call_ran_on(logins_dir, monkeypatch, tmp_path):
    monkeypatch.setattr(usage, "USAGE_FILE", tmp_path / "usage.json")
    claude_logins.make("work")
    claude.record(Info(), sdk.sdk_options(login="work"))
    assert "seven_day" in usage.of("work") and usage.of("") == {}
    assert json.loads((tmp_path / "usage-work.json").read_text())["seven_day"]["status"] == "rejected"


def test_a_task_resumes_on_its_own_login_or_carries_on_on_the_next(logins_dir, monkeypatch, tmp_path):
    monkeypatch.setattr(usage, "USAGE_FILE", tmp_path / "usage.json")
    monkeypatch.setattr(config, "CLAUDE_CONFIG_DIR", str(tmp_path / "home-claude"))
    work = claude_logins.make("work")
    claude_logins.set_order(["work", ""])
    (work / "projects/-x/abc-123").mkdir(parents=True)  # a subagent folder beside the transcript
    (work / "projects/-x/abc-123.jsonl").write_text("{}")
    assert claude_logins.holding("abc-123") == "work" and claude_logins.resume_on("abc-123") == "work"
    assert claude_logins.resume_on("other") is None and claude_logins.holding("../../etc") is None
    usage.record(Info(), login="work")
    assert claude_logins.resume_on("abc-123") == ""  # copied over to the default login, which isn't used up
    home = tmp_path / "home-claude" / "projects/-x"
    assert (home / "abc-123.jsonl").read_text() == "{}" and (home / "abc-123").is_dir()
    assert claude_logins.holding("abc-123") == ""  # the copy is the latest: work keeps an older one


def test_a_session_moves_elsewhere_only_while_its_login_is_passed_over_and_another_isnt(logins_dir, monkeypatch,
                                                                                         tmp_path):
    monkeypatch.setattr(usage, "USAGE_FILE", tmp_path / "usage.json")
    monkeypatch.setattr(config, "CLAUDE_CONFIG_DIR", str(tmp_path / "home-claude"))
    (home := tmp_path / "home-claude/projects/-x").mkdir(parents=True)
    (home / "abc-123.jsonl").write_text("{}")
    claude_logins.make("backup")
    assert claude_logins.elsewhere("abc-123") is None  # its own login works
    usage.record(Info(), login="")
    assert claude_logins.elsewhere("abc-123") == "backup" and claude_logins.elsewhere("other") is None
    usage.record(Info(), login="backup")
    assert claude_logins.elsewhere("abc-123") is None  # every login used up: it waits
