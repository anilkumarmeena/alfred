"""Tests for the persona's owner details (jarvis.brain): the "about you" setting and Google accounts."""

import io
import sqlite3
import threading
from pathlib import Path

import pytest

import jarvis
from conftest import use_profile
from jarvis.brain import persona
from jarvis.integrations import accounts
from jarvis.state import settings
from jarvis.storage import profile
from jarvis.tools import google

JARVIS = Path(jarvis.__file__).parent


@pytest.fixture
def db(tmp_path, monkeypatch):
    use_profile(monkeypatch, tmp_path)
    return tmp_path


def test_about_you_is_kept_tidy_and_can_be_cleared(db):
    assert settings.about() == ""
    assert settings.set_about("  Works at Acme.\n\n  - Runs a podcast on the side.  \n") == \
        "Works at Acme.\n- Runs a podcast on the side."
    assert settings.about() == "Works at Acme.\n- Runs a podcast on the side."
    settings.set_about("  ")
    assert settings.about() == "" and sqlite3.connect(db / "tasks.db").execute("select * from settings").fetchall() == []


def test_a_fresh_database_starts_with_no_about_you(db):
    assert settings.about() == ""


def test_two_processes_starting_at_once_make_the_table_once(db):
    errors = []

    def start() -> None:
        try:
            settings.about()
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=start) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == [] and settings.about() == ""
    assert settings.set_about("Works at Acme.") == settings.about() == "Works at Acme."


def test_a_database_that_cant_be_opened_means_no_about_you_rather_than_a_crash(db, monkeypatch, caplog):
    use_profile(monkeypatch, db, db=db)  # a folder, not a database
    assert settings.about() == "" and "can't read \"about you\"" in caplog.text


def test_the_command_shows_and_sets_about_you(db, capsys, monkeypatch, tmp_path):
    assert settings.main(["about"]) == 0
    assert capsys.readouterr().out.startswith('No "about you" yet. Set it with')
    assert settings.main(["about", "--set", "Works at Acme.\nHas two cats."]) == 0
    assert capsys.readouterr().out == ('"About you" is now:\nWorks at Acme.\nHas two cats.\n'
                                       "Restart the services for JARVIS to see the change: make services-restart\n")
    (tmp_path / "me.txt").write_text("From a file.\n")
    settings.main(["about", "--from", str(tmp_path / "me.txt")])
    assert settings.about() == "From a file."
    monkeypatch.setattr("sys.stdin", io.StringIO("From stdin.\n"))
    settings.main(["about", "--from", "-"])
    settings.main(["about"])
    assert capsys.readouterr().out.endswith("From stdin.\n")
    settings.main(["about", "--clear"])
    assert settings.about() == ""
    with pytest.raises(SystemExit, match="Couldn't read"):
        settings.main(["about", "--from", str(tmp_path / "missing.txt")])
    with pytest.raises(SystemExit):
        settings.main(["about", "--set", "x", "--clear"])  # one at a time


def test_the_persona_has_about_you_then_the_accounts_under_about_the_user():
    about = persona.persona().split(f"About {profile.name()}:\n", 1)[1].split("\n\n", 1)[0]
    assert about.splitlines() == ["- Works at Acme on its video platform.",
                                  "- Also runs their own ventures, kept apart from the day job.",
                                  f"- {accounts.describe(google.ACCOUNTS)}"]
    assert '"work" (me@acme.example): for work; "personal" (me@mail.example): their own.' in about
    assert 'without naming an account goes to "work"' in about


def test_no_owners_details_or_fixed_accounts_are_left_in_the_code():
    persona_code = Path(persona.__file__).read_text() + (JARVIS / "prompts" / "persona.md").read_text()
    assert "Works at" not in persona_code and "day job" not in persona_code and "portfolio" not in persona_code
    for path in JARVIS.rglob("*.py"):
        code = path.read_text()
        assert "WORK_ACCOUNT" not in code and "PERSONAL_ACCOUNT" not in code and "GOOGLE_ACCOUNTS" not in code, path
        assert "GOOGLE_CHAT_ACCOUNTS" not in code and "USER_EMPLOYER" not in code, path
        assert "make google-" not in code, path
