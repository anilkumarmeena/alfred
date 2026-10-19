"""Tests for installs from before the rename to Alfred: alfred.config.legacy (variables, .env, home), setup_local's
migrate_legacy and legacy_services (the home and the services), and the old entry points in the jarvis package."""

import importlib.util
import os
import plistlib
import sys
from pathlib import Path

import pytest

import jarvis
from alfred import config, setup_local
from alfred.config import ROOT, files, legacy

GUI = f"gui/{os.getuid()}"


def test_old_variables_are_renamed_and_a_new_one_set_already_wins():
    env = {"JARVIS_USER_NAME": "Sam", "JARVIS_TIMEZONE": "UTC", "ALFRED_TIMEZONE": "Asia/Kolkata", "PATH": "/bin"}
    assert legacy.adopt_env(env) == ["ALFRED_USER_NAME"]
    assert env == {"ALFRED_USER_NAME": "Sam", "ALFRED_TIMEZONE": "Asia/Kolkata", "PATH": "/bin"}  # no old name left


def test_an_old_env_file_is_rewritten_in_place_keeping_its_mode(tmp_path):
    env = tmp_path / ".env"
    env.write_text("# keys\nJARVIS_MASTER_KEY=abc\nexport JARVIS_PUSH=off\nALFRED_TIMEZONE=UTC\n"
                   "JARVIS_TIMEZONE=Europe/London\nLIVEKIT_API_KEY=jarvis1a2b\n")
    env.chmod(0o600)
    assert legacy.upgrade_env_file(env)
    assert env.read_text() == ("# keys\nALFRED_MASTER_KEY=abc\nexport ALFRED_PUSH=off\nALFRED_TIMEZONE=UTC\n"
                               "JARVIS_TIMEZONE=Europe/London\nLIVEKIT_API_KEY=jarvis1a2b\n")  # the new one stays
    assert env.stat().st_mode & 0o777 == 0o600
    assert not legacy.upgrade_env_file(env) and not legacy.upgrade_env_file(tmp_path / "missing")


def test_home_falls_back_to_an_old_install_s(tmp_path):
    user = tmp_path / "user"
    (user / ".jarvis/data").mkdir(parents=True)
    assert files.home({}, user) == user / ".jarvis"
    (user / ".alfred/data").mkdir(parents=True)
    assert files.home({}, user) == user / ".alfred"
    assert files.home({"JARVIS_HOME": str(tmp_path / "disk")}, user) == tmp_path / "disk"
    assert files.home({"ALFRED_HOME": "/new", "JARVIS_HOME": "/old"}, user) == Path("/new")


# ── Services and home ───────────────────────────────────────────


@pytest.fixture
def agents(tmp_path, monkeypatch):
    """Isolated LaunchAgents folder, default labels; launchctl and git calls recorded, not made."""
    folder = tmp_path / "LaunchAgents"
    folder.mkdir()
    monkeypatch.setattr(config, "SERVICE_PREFIX", config.DEFAULT_SERVICE_PREFIX)
    monkeypatch.setattr(setup_local, "MANAGER", "launchd")
    monkeypatch.setattr(setup_local, "LAUNCH_AGENTS", folder)
    monkeypatch.setattr(setup_local, "_link_log", lambda name: None)
    calls = []

    def run(cmd, **kwargs):
        calls.append(tuple(cmd[1:]))
        return setup_local.subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(setup_local.subprocess, "run", run)
    return folder, calls


def old_plist(folder: Path, name: str, root: Path = ROOT) -> Path:
    """A plist as an install from before the rename wrote it."""
    plist = folder / f"com.jarvis.{name}.plist"
    module = {"agent": "jarvis.voice.agent", "web": "jarvis.server", "tasks": "jarvis.tasks.runner"}.get(name, name)
    plist.write_bytes(plistlib.dumps({"Label": f"com.jarvis.{name}", "StandardOutPath": "/Users/a/.jarvis/logs/a.log",
                                      "ProgramArguments": [f"{root}/.venv/bin/python", "-m", module]}))
    return plist


def test_services_from_before_the_rename_come_back_under_their_new_labels(agents, capsys):
    folder, calls = agents
    old = [old_plist(folder, name) for name in setup_local.SERVICES]
    node = old_plist(folder, "node")
    assert setup_local.legacy_services() == old and setup_local.stale() == old
    setup_local.start_services()
    assert calls[:4] == [("bootout", GUI, str(p)) for p in old]
    assert not any(p.exists() for p in old) and node.exists()  # a node's is node-install's to move
    for name in setup_local.SERVICES:
        assert plistlib.loads(setup_local.installed(name).read_bytes())["Label"] == f"com.alfred.{name}"
    assert "removed com.jarvis.agent, now com.alfred.agent" in capsys.readouterr().out


def test_node_install_retires_the_old_node_service(agents):
    folder, calls = agents
    node = old_plist(folder, "node")
    setup_local.install_node()
    assert ("bootout", GUI, str(node)) in calls and not node.exists()
    assert plistlib.loads(setup_local.installed("node").read_bytes())["Label"] == "com.alfred.node"


def test_another_checkout_s_or_a_second_install_s_old_services_are_left_alone(agents, monkeypatch):
    folder, _ = agents
    old_plist(folder, "agent", Path("/Users/alex/other-jarvis"))
    assert setup_local.legacy_services() == []
    old_plist(folder, "web")
    monkeypatch.setattr(config, "SERVICE_PREFIX", "com.second")  # kept its own prefix: kept its labels
    assert setup_local.legacy_services() == []


def test_an_old_home_moves_to_dot_alfred_with_a_link_left_for_the_paths_kept_before(agents, tmp_path, monkeypatch):
    monkeypatch.delenv("ALFRED_HOME")
    folder, calls = agents
    user, root = tmp_path / "user", tmp_path / "checkout"
    (user / ".jarvis/data").mkdir(parents=True)
    (user / ".jarvis/data/tasks.db").write_text("db")
    root.mkdir()
    (root / "data").symlink_to(user / ".jarvis/data")
    agent = old_plist(folder, "agent")
    done = setup_local.migrate_legacy(user, root)
    assert done[0].startswith("moved") and len(done) == 2
    assert (user / ".alfred/data/tasks.db").read_text() == "db" and not (user / ".alfred").is_symlink()
    assert (user / ".jarvis").is_symlink() and (user / ".jarvis/data/tasks.db").read_text() == "db"
    assert os.readlink(root / "data") == str(user / ".alfred/data")
    assert ("bootout", GUI, str(agent)) in calls and agent.exists()  # stopped; start brings it back renamed
    assert setup_local.migrate_legacy(user, root) == []  # once


def test_an_old_home_that_links_elsewhere_gets_the_new_name_too(tmp_path, monkeypatch):
    monkeypatch.delenv("ALFRED_HOME")
    user, elsewhere = tmp_path / "user", tmp_path / "disk/home"
    elsewhere.mkdir(parents=True)
    user.mkdir()
    (user / ".jarvis").symlink_to(elsewhere)
    assert setup_local.migrate_legacy(user, tmp_path)[0].startswith("linked")
    assert (user / ".alfred").resolve() == elsewhere.resolve() and (user / ".jarvis").is_symlink()


def test_nothing_moves_while_alfred_home_names_the_home(tmp_path):
    (tmp_path / ".jarvis/data").mkdir(parents=True)
    assert setup_local.migrate_legacy(tmp_path, tmp_path) == [] and not (tmp_path / ".alfred").exists()


# ── Old entry points ────────────────────────────────────────────

OLD_ENTRY_POINTS = {"jarvis.voice.agent": "alfred.voice.agent", "jarvis.server": "alfred.server",
                    "jarvis.tasks.runner": "alfred.tasks.runner", "jarvis.node": "alfred.node"}


@pytest.mark.parametrize("old, new", OLD_ENTRY_POINTS.items())
def test_each_old_entry_point_hands_over_to_its_alfred_module(old, new):
    found = importlib.util.find_spec(old)
    shim = Path(found.origin if not found.submodule_search_locations else found.submodule_search_locations[0])
    shim = shim / "__main__.py" if shim.is_dir() else shim
    assert f'hand_over("{new}")' in shim.read_text() and importlib.util.find_spec(new)
    services = "".join(p.read_text() for p in (ROOT / "deploy").rglob("*") if p.is_file())
    assert new in services  # still what the services run


def test_handing_over_keeps_the_arguments(monkeypatch):
    called = []
    monkeypatch.setattr(jarvis.os, "execv", lambda exe, argv: called.append(argv))
    monkeypatch.setattr(jarvis.sys, "argv", ["/old/path", "serve"])
    jarvis.hand_over("alfred.node")
    assert called == [[sys.executable, "-m", "alfred.node", "serve"]]
