"""Tests for jarvis.auth.logins: hashed passcodes, adopting JARVIS_PASSCODE, and the web server and panel using them."""

import asyncio
import base64
import dataclasses
import json
import os
import stat

import pytest

from jarvis import panel, server
from jarvis.auth import logins, vault
from jarvis.storage import profile
from tests.test_panel import reply
from tests.test_vault import key  # noqa: F401  (fixture: fresh master key, empty vault)
from tests.test_web_build import Request as TokenRequest


def named(monkeypatch, name: str) -> None:
    """The owner profile, named `name`; the first login takes its name from it."""
    owner = dataclasses.replace(profile.owner(), name=name)
    monkeypatch.setattr(profile, "owner", lambda: owner)


@pytest.fixture
def home(tmp_path, monkeypatch):
    """No logins yet, with scratch logins and .env files."""
    monkeypatch.setattr(logins, "FILE", tmp_path / "logins.json")
    monkeypatch.setattr(vault, "ENV_FILE", tmp_path / "dotenv")
    named(monkeypatch, "Alex")
    monkeypatch.delenv(logins.VAR, raising=False)
    logins._known.clear()
    server._failures.clear()
    return tmp_path


def test_a_passcode_is_kept_only_as_a_salted_hash_in_a_file_only_the_user_can_read(home):
    logins.add("Alex", "123456")
    text = logins.FILE.read_text()
    assert "123456" not in text and "scrypt$16384$8$1$" in text
    assert stat.S_IMODE(logins.FILE.stat().st_mode) == 0o600
    assert logins.hashed("123456") != logins.hashed("123456")  # salted
    assert logins.listed() == [{"name": "Alex", "created": pytest.approx(json.loads(text)["logins"][0]["created"]),
                                "used": None, "owner": True}]


def test_the_passcode_says_whose_login_it_is(home):
    logins.add("Alex", "123456")
    logins.add("Sam", "sam-secret")
    assert logins.check("123456") == "Alex" and logins.check("sam-secret") == "Sam"
    assert logins.check("654321") is None and logins.check("") is None


def test_a_passcode_checked_once_is_remembered_until_it_changes(home, monkeypatch):
    logins.add("Alex", "123456")
    checked, real = [], logins.matches
    monkeypatch.setattr(logins, "matches", lambda stored, passcode: checked.append(passcode) or real(stored, passcode))
    assert logins.check("123456") == logins.check("123456") == "Alex" and checked == ["123456"]
    logins.set_passcode("alex", "abcdef")
    assert logins.check("123456") is None and logins.check("abcdef") == "Alex"


def test_no_two_logins_share_a_passcode_or_a_name(home):
    logins.add("Alex", "123456")
    with pytest.raises(ValueError, match="another passcode"):
        logins.add("Sam", "123456")
    with pytest.raises(ValueError, match="already"):
        logins.add("alex", "999999")
    logins.add("Sam", "999999")
    with pytest.raises(ValueError, match="another passcode"):
        logins.set_passcode("Sam", "123456")
    logins.set_passcode("Alex", "123456")  # its own passcode again is fine


@pytest.mark.parametrize("name, passcode, problem", [("", "123456", "name"), ("1st", "123456", "name"),
                                                     ("Sam", "12345", "6 to 128"), ("Sam", " 123456", "6 to 128")])
def test_names_and_passcodes_are_checked(home, name, passcode, problem):
    with pytest.raises(ValueError, match=problem):
        logins.add(name, passcode)


def test_the_last_login_cant_be_removed(home):
    logins.add("Alex", "123456")
    with pytest.raises(ValueError, match="only login"):
        logins.remove("Alex")
    logins.add("Sam", "999999")
    logins.remove("sam")
    assert [x["name"] for x in logins.listed()] == ["Alex"] and logins.check("999999") is None
    with pytest.raises(ValueError, match="no login called Sam"):
        logins.remove("Sam")


def test_the_first_login_is_the_owners_and_only_its_owner_changes_it(home):
    logins.add("Alex", "123456")
    logins.add("Sam", "999999")
    assert logins.owner() == "Alex" and [x["owner"] for x in logins.listed()] == [True, False]
    with pytest.raises(PermissionError, match="Only Alex"):
        logins.set_passcode("Alex", "taken-over", by="Sam")
    with pytest.raises(PermissionError, match="Only Alex"):
        logins.remove("Alex", by="Sam")
    logins.set_passcode("Sam", "sam-new-1", by="Alex")  # the owner may change anyone's
    logins.set_passcode("Alex", "alex-new-1", by="alex")
    logins.remove("Alex", by="Alex")  # the owner leaving hands it on to the oldest login left
    assert logins.owner() == "Sam" and logins.check("sam-new-1") == "Sam"
    with pytest.raises(PermissionError, match="Only Sam's login"):
        logins.owner_only("Alex", "pair machines")


def test_logins_from_before_the_owner_mark_take_the_oldest_as_the_owners(home):
    logins.add("Alex", "123456")
    logins.add("Sam", "999999")
    rows = json.loads(logins.FILE.read_text())["logins"]
    logins._write([{k: v for k, v in r.items() if k != "owner"} for r in reversed(rows)])
    assert logins.owner() == "Alex"


def test_with_no_login_the_first_setup_passcode_is_checked_as_the_users(home, monkeypatch):
    assert not logins.ready() and logins.check("123456") is None
    monkeypatch.setenv(logins.VAR, "123456")
    assert logins.ready() and logins.check("123456") == "Alex" and logins.check("000000") is None


def test_the_shared_passcode_becomes_the_users_login_and_leaves_env(home, monkeypatch):
    vault.ENV_FILE.write_text(f"JARVIS_USER_NAME=Alex\n{logins.VAR}=4321\n")  # shorter than a new passcode may be
    monkeypatch.setenv(logins.VAR, "4321")
    assert logins.adopt() == "Alex"
    assert logins.check("4321") == "Alex" and "4321" not in logins.FILE.read_text()
    assert vault.ENV_FILE.read_text() == "JARVIS_USER_NAME=Alex\n" and logins.VAR not in os.environ
    assert logins.adopt() == ""  # once


def test_a_login_with_no_usable_name_is_the_owners(home, monkeypatch):
    named(monkeypatch, "the user")
    monkeypatch.setenv(logins.VAR, "123456")
    assert logins.adopt() == "Owner" and logins.check("123456") == "Owner"


def test_last_use_is_written_now_and_then(home, monkeypatch):
    logins.add("Alex", "123456")
    clock = iter([1000.0, 1100.0, 1400.0])
    monkeypatch.setattr(logins.time, "time", lambda: next(clock))
    logins.used("Alex")
    logins.used("Alex")  # too soon after
    assert logins.listed()[0]["used"] == 1000.0
    logins.used("Alex")
    assert logins.listed()[0]["used"] == 1400.0


# ── Behind the web server ──────────────────────────────────────


def test_each_login_reaches_the_panel_with_its_own_passcode(home, monkeypatch):
    logins.add("Alex", "123456")
    logins.add("Sam", "sam-secret")
    monkeypatch.setenv(logins.VAR, "999999")  # left in .env: not accepted once logins exist
    assert reply("access", {"passcode": "999999"})[0] == 401
    status, body = reply("access", {"passcode": "sam-secret", "login": "Alex"})  # the passcode decides who it is
    assert status == 200 and [(x["name"], x["this"]) for x in body["result"]["logins"]] == [("Alex", False), ("Sam", True)]


def test_a_call_is_in_the_logins_name(home, monkeypatch, tmp_path):
    logins.add("Sam Lee", "sam-secret")
    monkeypatch.setattr(server, "WEB_DIR", tmp_path)
    monkeypatch.setenv("LIVEKIT_API_KEY", "key")
    monkeypatch.setenv("LIVEKIT_API_SECRET", "a-secret-long-enough-for-the-jwt-signer-0123456789")
    r = asyncio.run(server.token(TokenRequest({"passcode": "sam-secret", "device": "iPhone"})))
    claims = json.loads(base64.urlsafe_b64decode(json.loads(r.text)["token"].split(".")[1] + "=="))
    assert claims["name"] == "Sam Lee" and claims["sub"].startswith("samlee-iphone-")
    assert logins.listed()[0]["used"]


def test_the_panel_manages_logins_and_never_shows_a_passcode(home):
    logins.add("Alex", "123456")
    me = {"login": "Alex"}
    got = asyncio.run(panel.call("login_add", me | {"name": "Sam", "passcode_new": "sam-secret"}))
    assert [x["name"] for x in got["logins"]] == ["Alex", "Sam"] and "sam-secret" not in json.dumps(got)
    asyncio.run(panel.call("login_passcode", me | {"name": "Sam", "passcode_new": "new-secret"}))
    assert logins.check("new-secret") == "Sam"
    with pytest.raises(ValueError, match="another passcode"):
        asyncio.run(panel.call("login_passcode", me | {"name": "Sam", "passcode_new": "123456"}))
    got = asyncio.run(panel.call("login_remove", me | {"name": "Sam"}))
    assert [x["name"] for x in got["logins"]] == ["Alex"]
    asyncio.run(panel.call("login_add", me | {"name": "Sam", "passcode_new": "sam-secret"}))
    with pytest.raises(PermissionError, match="Only Alex"):  # another login can't take the owner's over
        asyncio.run(panel.call("login_passcode", {"login": "Sam", "name": "Alex", "passcode_new": "mine-now"}))


# ── make passcode ───────────────────────────────────────────────


def test_make_passcode_shows_each_login_from_an_encrypted_copy(home, key, capsys):
    logins.add("Alex", "123456")
    logins.add("Sam", "654321")
    logins.set_passcode("sam", "777777")  # names are case-insensitive
    assert logins.shown() == [("Alex", "123456"), ("Sam", "777777")]
    assert "123456" not in logins.FILE.read_text()  # the copy lives in the vault, encrypted
    logins.remove("Sam")
    assert logins.shown() == [("Alex", "123456")] and len(vault.names()) == 1
    assert logins.main() == 0 and capsys.readouterr().out == "Alex: 123456\n"


def test_without_a_vault_key_a_login_still_works_and_make_passcode_says_how_to_reset(home, capsys):
    logins.add("Alex", "123456")  # no master key in tests by default
    assert logins.check("123456") == "Alex" and logins.shown() == [("Alex", None)]
    assert logins.main() == 0 and "not kept" in capsys.readouterr().out


def test_adopting_the_first_setup_passcode_keeps_a_copy(home, key, monkeypatch):
    monkeypatch.setenv(logins.VAR, "246810")
    assert logins.adopt() == "Alex" and logins.shown() == [("Alex", "246810")]


def test_no_panel_answer_carries_a_passcode(home, key):
    logins.add("Alex", "123456")
    assert "123456" not in json.dumps(asyncio.run(panel.call("access", {"login": "Alex"})))
