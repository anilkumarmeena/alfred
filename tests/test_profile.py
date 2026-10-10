"""Tests for profiles (jarvis.core.profile): the owner's made from what the install already had, others added, and code
running as one of them, its data and database included. Run with: make test"""

import asyncio
import sqlite3

import pytest

from jarvis.brain import persona
from jarvis.core import config, profile
from jarvis.state import settings


@pytest.fixture
def install(tmp_path, monkeypatch):
    """An install of the test's own, its database holding the settings an install had before profiles, with no profile
    given to the process, so the owner's is read from the database."""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "DB_FILE", tmp_path / "data" / "tasks.db")
    monkeypatch.setattr(config, "SECRETS_DIR", tmp_path / "secrets")
    monkeypatch.setattr(config, "mac_name", lambda: "Lee")
    monkeypatch.setattr(config, "mac_timezone", lambda: "Europe/London")
    monkeypatch.setattr(profile, "_process", None)
    for var in ("JARVIS_USER_NAME", "JARVIS_TIMEZONE"):
        monkeypatch.delenv(var, raising=False)
    config.DATA_DIR.mkdir()
    profile.forget()
    yield tmp_path
    profile.forget()


def old_settings(**kept: str) -> None:
    with sqlite3.connect(config.DB_FILE) as db:
        db.execute("create table settings (key text primary key, value text not null)")
        db.executemany("insert into settings values (?, ?)", kept.items())


def test_the_owner_is_made_from_the_settings_an_install_already_had(install):
    old_settings(name="Priya", timezone="Asia/Kolkata", voice="bm_lewis")
    owner = profile.current()
    assert (owner.id, owner.name, owner.timezone) == ("owner", "Priya", "Asia/Kolkata")
    assert (owner.data, owner.db, owner.secrets) == (config.DATA_DIR, config.DB_FILE, config.SECRETS_DIR)  # nothing moves
    assert owner.memory_file == config.DATA_DIR / "memory.md" and owner.skills == config.DATA_DIR / "skills"
    assert profile.stored() | {"created": 0} == {"id": "owner", "name": "Priya", "timezone": "Asia/Kolkata",
                                                  "data_dir": None, "db_file": None, "created": 0}


def test_the_owner_seeds_from_env_and_env_still_wins(install, monkeypatch):
    monkeypatch.setenv("JARVIS_USER_NAME", "Sam")
    assert profile.current().name == "Sam" and profile.stored()["name"] == "Sam"
    profile.change(name="Samuel")
    assert profile.current().name == "Sam"  # .env wins, as for every preference
    monkeypatch.delenv("JARVIS_USER_NAME")
    profile.forget()
    assert profile.current().name == "Samuel"


def test_with_nothing_kept_the_mac_s_own_name_and_zone(install):
    owner = profile.current()
    assert (owner.name, owner.timezone, owner.tz.key) == ("Lee", "Europe/London", "Europe/London")


def test_a_change_applies_without_a_restart(install):
    assert profile.current().name == "Lee"
    profile.change(name="Alex", timezone="America/New_York")
    assert (profile.current().name, profile.current().timezone) == ("Alex", "America/New_York")
    profile.change(name="")  # back to the default
    assert profile.current().name == "Lee"
    with pytest.raises(ValueError):
        profile.change(colour="blue")


def test_an_unknown_zone_falls_back_rather_than_breaking(install):
    profile.change(timezone="Mars/Olympus")
    assert profile.current().timezone == "Europe/London"


def test_someone_else_gets_their_own_data_database_and_sign_ins(install):
    guest = profile.add("guest", "Robin", "Asia/Tokyo")
    assert guest.data == config.DATA_DIR / "profiles" / "guest" and guest.db == guest.data / "tasks.db"
    assert guest.secrets == config.SECRETS_DIR / "profiles" / "guest" and guest.data.is_dir() and guest.secrets.is_dir()
    assert profile.load("guest") == guest and profile.load().id == "owner"
    for bad in ("guest", "owner", "Not Ok", "../up"):
        with pytest.raises(ValueError):
            profile.add(bad)
    with pytest.raises(KeyError):
        profile.load("nobody")


def test_code_runs_as_the_profile_it_is_given_its_tasks_included(install, monkeypatch):
    monkeypatch.setenv("JARVIS_USER_NAME", "Sam")  # the owner's alone
    guest = profile.add("guest", "Robin")
    settings.set_about("The owner.")
    with profile.using(guest):
        assert profile.current().name == "Robin" and settings.about() == ""  # their own database
        settings.set_about("A guest.")

        async def inside() -> str:
            return await asyncio.create_task(asyncio.to_thread(lambda: profile.current().name))

        assert asyncio.run(inside()) == "Robin"
    assert profile.current().name == "Sam" and settings.about() == "The owner."
    with profile.using(guest):
        assert settings.about() == "A guest."


def test_an_unreadable_database_still_gives_the_owner(install, monkeypatch, caplog):
    monkeypatch.setattr(config, "DB_FILE", install)  # a folder, not a database
    assert profile.current().name == "Lee" and "can't read the profiles" in caplog.text


def test_the_panel_s_name_and_zone_change_the_profile_and_the_prompts_at_once(install):
    settings.set_preference("name", "Robin")
    settings.set_preference("timezone", "Asia/Tokyo")
    assert (profile.name(), profile.tz().key) == ("Robin", "Asia/Tokyo") and settings.get("name") == ""
    assert settings.preferences()["preferences"]["name"] == {"value": "Robin", "default": "Lee", "env": ""}
    assert persona.persona().startswith("You are JARVIS, Robin's personal AI assistant")
    settings.set_preference("name", "")
    assert profile.name() == "Lee" and "Lee's personal" in persona.persona()
