"""Tests for jarvis.setup_local: launchd plists from deploy/, moving to new labels, and where projects live."""

import os
import plistlib
from pathlib import Path

import pytest

from jarvis import setup_local
from jarvis.core import config
from jarvis.core.config import ROOT

GUI = f"gui/{os.getuid()}"


@pytest.fixture(autouse=True)
def default_prefix(monkeypatch):
    monkeypatch.setattr(config, "SERVICE_PREFIX", "com.jarvis")  # regardless of this Mac's .env


@pytest.mark.parametrize("name", setup_local.SERVICES)
def test_each_plist_gets_its_label_and_this_mac_s_folders(name, monkeypatch):
    monkeypatch.setattr(setup_local.shutil, "which", lambda cmd: "/Users/alex/.nvm/versions/node/v22/bin/node")
    monkeypatch.setattr(setup_local.Path, "home", lambda: Path("/Users/alex"))
    text = setup_local.service_plist(name)
    plist = plistlib.loads(text.encode())
    assert "$" not in text and plist["Label"] == f"com.jarvis.{name}" and plist["WorkingDirectory"] == str(ROOT)
    assert plist["StandardOutPath"].startswith(f"{ROOT}/logs/")
    assert "/Users/alex/.local/bin:" in plist["EnvironmentVariables"]["PATH"]
    if name in ("tasks", "agent"):  # tasks build with node; the agent runs the GitLab server with npx
        assert "/Users/alex/.nvm/versions/node/v22/bin:/usr/local/bin" in plist["EnvironmentVariables"]["PATH"]


def test_the_label_prefix_is_a_setting(monkeypatch):
    monkeypatch.setattr(config, "SERVICE_PREFIX", "org.example.jarvis")
    assert plistlib.loads(setup_local.service_plist("agent").encode())["Label"] == "org.example.jarvis.agent"
    assert setup_local.installed("web").name == "org.example.jarvis.web.plist"


def test_the_voice_comes_before_background_tasks():
    """Under task load Kokoro rendered slower than speech, leaving pauses between sentences."""
    agent = plistlib.loads(setup_local.service_plist("agent").encode())
    tasks = plistlib.loads(setup_local.service_plist("tasks").encode())
    assert agent["ProcessType"] == "Interactive" and "Nice" not in agent
    assert tasks["Nice"] == 10 and tasks["LowPriorityIO"] is True
    assert "ProcessType" not in tasks  # Background would keep tasks on the efficiency cores


def test_no_plist_names_a_home_folder_or_a_label():
    for name in setup_local.SERVICES:
        text = (ROOT / "deploy" / f"{name}.plist").read_text()
        assert "/Users/" not in text and "<string>${LABEL}</string>" in text


def test_without_node_the_tasks_path_just_skips_it(monkeypatch):
    monkeypatch.setattr(setup_local.shutil, "which", lambda cmd: None)
    path = plistlib.loads(setup_local.service_plist(setup_local.SERVICES[-1]).encode())["EnvironmentVariables"]["PATH"]
    assert "/usr/local/go/bin:/usr/local/bin:" in path and "::" not in path


# ── Moving to new labels ───────────────────────────────────────


@pytest.fixture
def agents(tmp_path, monkeypatch):
    """Isolated LaunchAgents folder; launchctl calls recorded, not made."""
    folder = tmp_path / "LaunchAgents"
    folder.mkdir()
    monkeypatch.setattr(setup_local, "LAUNCH_AGENTS", folder)
    calls = []

    def run(cmd, **kwargs):
        calls.append(tuple(cmd[1:]))
        return setup_local.subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(setup_local.subprocess, "run", run)
    return folder, calls


def installed_as(folder: Path, label: str, root: Path = ROOT) -> Path:
    """Write a plist as an older install did: under `label`, logging into `root`'s logs."""
    plist = folder / f"{label}.plist"
    log = f"{root}/logs/{label.rpartition('.')[2]}.log"
    plist.write_bytes(plistlib.dumps({"Label": label, "StandardOutPath": log}))
    return plist


def test_services_under_an_old_prefix_are_stale_but_only_this_checkout_s(agents):
    folder, _ = agents
    old = [installed_as(folder, f"com.someone.jarvis.{name}") for name in setup_local.SERVICES]
    installed_as(folder, "com.other.jarvis.agent", Path("/Users/alex/other-jarvis"))  # another checkout's
    installed_as(folder, "com.someone.backup", ROOT)  # not named like a service
    (folder / "com.broken.jarvis.web.plist").write_text("not a plist")
    for name in setup_local.SERVICES:
        installed_as(folder, config.service_label(name))  # the current ones
    assert setup_local.stale() == sorted(old)


def test_start_moves_an_install_to_the_new_labels(agents, capsys):
    folder, calls = agents
    old = [installed_as(folder, f"com.someone.jarvis.{name}") for name in setup_local.SERVICES]
    other = installed_as(folder, "com.other.jarvis.agent", Path("/Users/alex/other-jarvis"))
    setup_local.start_services()
    assert calls[:4] == [("bootout", GUI, str(p)) for p in sorted(old)]  # old ones stop before the new ones start
    assert calls[4:] == [("bootstrap", GUI, str(setup_local.installed(name))) for name in setup_local.SERVICES]
    assert not any(p.exists() for p in old) and other.exists()
    for name in setup_local.SERVICES:
        assert plistlib.loads(setup_local.installed(name).read_bytes())["Label"] == f"com.jarvis.{name}"
    assert "removed com.someone.jarvis.agent, now com.jarvis.agent" in capsys.readouterr().out


def test_start_leaves_installed_plists_as_they_are(agents):
    folder, calls = agents
    for name in setup_local.SERVICES:
        setup_local.installed(name).write_text("as installed")
    setup_local.start_services()
    assert calls == [("bootstrap", GUI, str(setup_local.installed(name))) for name in setup_local.SERVICES]
    assert all(setup_local.installed(name).read_text() == "as installed" for name in setup_local.SERVICES)


def test_stop_stops_the_old_labels_too_and_keeps_them_for_start(agents):
    folder, calls = agents
    old = installed_as(folder, "com.someone.jarvis.agent")
    setup_local.installed("agent").write_text("current")
    setup_local.stop_services()
    assert calls == [("bootout", GUI, str(setup_local.installed("agent"))), ("bootout", GUI, str(old))]
    assert old.exists()


def test_install_retires_old_labels_first(agents):
    folder, calls = agents
    old = installed_as(folder, "com.someone.jarvis.tasks")
    setup_local.install_services()
    assert calls[0] == ("bootout", GUI, str(old)) and not old.exists()
    assert ("bootstrap", GUI, str(setup_local.installed("tasks"))) in calls


def test_start_retries_a_service_still_shutting_down(agents, monkeypatch):
    folder, calls = agents
    for name in setup_local.SERVICES:
        setup_local.installed(name).write_text("as installed")
    monkeypatch.setattr(setup_local.time, "sleep", lambda s: None)
    failures = iter([5, 1])  # bootstrap fails, print finds it not running, then bootstrap works

    def run(cmd, **kwargs):
        calls.append(tuple(cmd[1:]))
        code = next(failures, 0) if cmd[1:3] == ["bootstrap", GUI] or cmd[1] == "print" else 0
        return setup_local.subprocess.CompletedProcess(cmd, code, "", "Bootstrap failed: 5: Input/output error")

    monkeypatch.setattr(setup_local.subprocess, "run", run)
    setup_local.start_services()
    first = str(setup_local.installed(setup_local.SERVICES[0]))
    assert calls[:3] == [("bootstrap", GUI, first), ("print", f"{GUI}/{config.service_label(setup_local.SERVICES[0])}"),
                         ("bootstrap", GUI, first)]


def test_start_says_why_and_fails_when_a_service_wont_start(agents, monkeypatch, capsys):
    for name in setup_local.SERVICES:
        setup_local.installed(name).write_text("as installed")
    monkeypatch.setattr(setup_local.time, "sleep", lambda s: None)
    monkeypatch.setattr(setup_local.subprocess, "run", lambda cmd, **kw: setup_local.subprocess.CompletedProcess(
        cmd, 5, "", "Bootstrap failed: 5: Input/output error\n"))
    with pytest.raises(SystemExit) as exit:
        setup_local.start_services()
    assert exit.value.code == 1
    out = capsys.readouterr()
    assert "started" not in out.out
    assert f"couldn't start {config.service_label('agent')}: Bootstrap failed: 5: Input/output error" in out.err


# ── Where projects are ─────────────────────────────────────────


@pytest.fixture
def no_folder_env(monkeypatch):
    monkeypatch.delenv("JARVIS_NEW_PROJECTS_DIR", raising=False)
    monkeypatch.delenv("JARVIS_TASK_READ_DIRS", raising=False)
    monkeypatch.setattr(config, "projects_home", lambda: Path.home() / "code")


def test_setup_asks_where_projects_are_once(no_folder_env, monkeypatch):
    monkeypatch.setattr(setup_local.sys.stdin, "isatty", lambda: True, raising=False)
    asked = []
    text = setup_local.ask_projects_folder("A=1\n", ask=lambda q: asked.append(q) or "~/work")
    assert "[~/code]" in asked[0]
    assert "JARVIS_NEW_PROJECTS_DIR=~/work\n" in text and "JARVIS_TASK_READ_DIRS=~/work\n" in text
    assert setup_local.ask_projects_folder(text, ask=lambda q: pytest.fail("asked again")) == text


def test_without_a_terminal_setup_takes_the_default(no_folder_env, monkeypatch):
    monkeypatch.setattr(setup_local.sys.stdin, "isatty", lambda: False, raising=False)
    text = setup_local.ask_projects_folder("JARVIS_TASK_READ_DIRS=~/a,~/b\n", ask=lambda q: pytest.fail("asked"))
    assert "JARVIS_NEW_PROJECTS_DIR=~/code\n" in text and "JARVIS_TASK_READ_DIRS=~/a,~/b\n" in text


def test_a_folder_already_set_is_kept(monkeypatch):
    monkeypatch.setenv("JARVIS_NEW_PROJECTS_DIR", "/srv/projects")
    assert setup_local.ask_projects_folder("A=1\n", ask=lambda q: pytest.fail("asked")) == "A=1\n"


def test_projects_default_to_the_folder_jarvis_is_in(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "ROOT", tmp_path / "code" / "jarvis")
    assert config.projects_home() == tmp_path / "code"
    monkeypatch.setattr(config, "ROOT", tmp_path / "code" / "jarvis" / "data" / "worktrees" / "jarvis-7")
    assert config.projects_home() == tmp_path / "code"  # a task's worktree goes by its main checkout
    monkeypatch.setattr(config, "ROOT", Path.home() / "jarvis")
    assert config.projects_home() == Path.home() / "Projects"  # never the whole home folder
