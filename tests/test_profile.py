"""Tests for core.profile: the owner's profile, adding profiles, running as one."""

import asyncio

import pytest

from jarvis import config
from jarvis.brain import persona
from jarvis.state import settings
from jarvis.storage import profile


@pytest.fixture
def install(tmp_path, monkeypatch):
    """A fresh install with no process profile: the owner's is read from the DB."""
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


def test_the_owner_uses_the_install_s_own_folders(install):
    owner = profile.current()
    assert owner.id == "owner"
    assert (owner.data, owner.db, owner.secrets) == (config.DATA_DIR, config.DB_FILE, config.SECRETS_DIR)
    assert owner.memory_file == config.DATA_DIR / "memory.md" and owner.skills == config.DATA_DIR / "skills"
    assert profile.stored() | {"created": 0} == {"id": "owner", "name": "", "timezone": "",
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
