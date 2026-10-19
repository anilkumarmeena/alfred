"""Tests for core.profile: the owner's profile, adding profiles, running as one."""

import asyncio
import sqlite3

import pytest

from alfred import config
from alfred.brain import persona
from alfred.state import settings
from alfred.storage import profile


@pytest.fixture
def install(tmp_path, monkeypatch):
    """A fresh install with no process profile: the owner's is read from the DB."""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "DB_FILE", tmp_path / "data" / "tasks.db")
    monkeypatch.setattr(config, "SECRETS_DIR", tmp_path / "secrets")
    monkeypatch.setattr(config, "mac_name", lambda: "Lee")
    monkeypatch.setattr(config, "mac_timezone", lambda: "Europe/London")
    monkeypatch.setattr(profile, "_process", None)
    for var in ("ALFRED_USER_NAME", "ALFRED_TIMEZONE", "ALFRED_ASSISTANT_NAME"):
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
    assert profile.stored() | {"created": 0} == {"id": "owner", "name": "", "assistant": "", "timezone": "",
                                                  "data_dir": None, "db_file": None, "created": 0}


def test_the_owner_seeds_from_env_and_env_still_wins(install, monkeypatch):
    monkeypatch.setenv("ALFRED_USER_NAME", "Sam")
    assert profile.current().name == "Sam" and profile.stored()["name"] == "Sam"
    profile.change(name="Samuel")
    assert profile.current().name == "Sam"  # .env wins, as for every preference
    monkeypatch.delenv("ALFRED_USER_NAME")
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
    monkeypatch.setenv("ALFRED_USER_NAME", "Sam")  # the owner's alone
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
    assert persona.persona().startswith("You are Alfred, Robin's personal AI assistant")
    settings.set_preference("name", "")
    assert profile.name() == "Lee" and "Lee's personal" in persona.persona()


# ── The assistant's name ────────────────────────────────────────


def test_the_owner_names_their_assistant_and_every_brain_goes_by_it(install, monkeypatch):
    assert profile.assistant() == "Alfred"
    said = persona.persona()
    assert said.startswith("You are Alfred, Lee's personal AI assistant") and "built on" not in said
    settings.set_preference("assistant", "  Friday ")
    assert profile.assistant() == "Friday" and profile.stored()["assistant"] == "Friday"
    said = persona.persona()
    assert said.startswith("You are Friday, Lee's personal AI assistant") and "say Alfred, they mean you" in said
    assert settings.preferences()["preferences"]["assistant"] == {"value": "Friday", "default": "Alfred", "env": ""}
    monkeypatch.setenv("ALFRED_ASSISTANT_NAME", "Jeeves")  # .env wins
    profile.forget()
    assert profile.assistant() == "Jeeves"
    monkeypatch.delenv("ALFRED_ASSISTANT_NAME")
    settings.set_preference("assistant", "")
    assert profile.assistant() == "Alfred"


def test_a_name_that_wouldnt_work_as_one_is_refused(install):
    assert settings.assistant_name("  mister   bean ") == "mister bean"
    assert settings.assistant_name("जार्विस") == "जार्विस" and settings.assistant_name("R2-D2") == "R2-D2"
    for bad, why in (("2pac", "letters"), ("Hi {{os}}", "letters"), ("Bob<b>", "letters"), ("A" * 25, "24")):
        with pytest.raises(ValueError, match=why):
            settings.set_preference("assistant", bad)
    assert profile.assistant() == "Alfred"


def test_each_person_names_their_own_assistant(install):
    profile.add("kid", name="Mia", assistant="Robo")
    assert profile.load("kid").assistant == "Robo" and profile.assistant() == "Alfred"
    with profile.using(profile.load("kid")):
        assert profile.assistant() == "Robo" and persona.persona().startswith("You are Robo, Mia's")


def test_profiles_saved_before_the_assistant_had_a_name_get_the_default(install):
    with sqlite3.connect(config.DB_FILE) as db:
        db.execute(profile._SCHEMA)
        db.execute("insert into profiles (id, name, timezone, created) values ('owner', 'Sam', '', 0)")
    profile.forget()
    assert (profile.load().name, profile.load().assistant) == ("Sam", "Alfred")
