"""Tests for jarvis.setup_local: launchd plists and systemd units from deploy/, moving to new labels, and where projects
live."""

import os
import plistlib
from pathlib import Path

import pytest

from jarvis import config, setup_local
from jarvis.config import HOME_DIR, ROOT

GUI = f"gui/{os.getuid()}"


@pytest.fixture(autouse=True)
def default_prefix(monkeypatch):
    monkeypatch.setattr(config, "SERVICE_PREFIX", "com.jarvis")  # regardless of this computer's .env
    monkeypatch.setattr(setup_local, "MANAGER", "launchd")  # launchd tests run anywhere; systemd ones opt in


@pytest.mark.parametrize("name", setup_local.SERVICES)
def test_each_plist_gets_its_label_and_this_mac_s_folders(name, monkeypatch):
    monkeypatch.setattr(setup_local.shutil, "which", lambda cmd: "/Users/alex/.nvm/versions/node/v22/bin/node")
    monkeypatch.setattr(setup_local.Path, "home", lambda: Path("/Users/alex"))
    text = setup_local.service_plist(name)
    plist = plistlib.loads(text.encode())
    assert "$" not in text and plist["Label"] == f"com.jarvis.{name}" and plist["WorkingDirectory"] == str(ROOT)
    assert plist["StandardOutPath"] == f"{setup_local.SERVICE_LOGS}/{name}.log"
    assert "/Users/alex/.local/bin:" in plist["EnvironmentVariables"]["PATH"]
    if name in ("tasks", "agent"):  # tasks build with node; the agent runs the GitLab server with npx
        assert "/Users/alex/.nvm/versions/node/v22/bin:/usr/local/bin" in plist["EnvironmentVariables"]["PATH"]


def test_the_label_prefix_is_a_setting(monkeypatch):
    monkeypatch.setattr(config, "SERVICE_PREFIX", "org.example.jarvis")
    assert plistlib.loads(setup_local.service_plist("agent").encode())["Label"] == "org.example.jarvis.agent"
    assert setup_local.installed("web").name == "org.example.jarvis.web.plist"


def test_a_service_log_moves_out_of_the_checkout_and_is_linked_back(tmp_path, monkeypatch):
    """launchd can't open a log under ~/Desktop, so it writes in ~/Library/Logs and logs/ links there."""
    root, logs = tmp_path / "jarvis", tmp_path / "Library/Logs/com.jarvis"
    (root / "logs").mkdir(parents=True)
    (root / "logs/agent.log").write_text("history\n")
    monkeypatch.setattr(setup_local, "LOGS_DIR", root / "logs")
    monkeypatch.setattr(setup_local, "SERVICE_LOGS", logs)
    setup_local._link_log("agent")
    setup_local._link_log("agent")  # again: already linked
    setup_local._link_log("web")  # nothing yet: just the link
    assert (root / "logs/agent.log").is_symlink() and (root / "logs/agent.log").read_text() == "history\n"
    assert (logs / "agent.log").read_text() == "history\n"
    assert (root / "logs/web.log").resolve() == logs / "web.log" and not (logs / "web.log").exists()


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
    monkeypatch.setattr(setup_local, "_link_log", lambda name: None)  # leaves this checkout's logs/ alone
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


# ── systemd user units (Linux) ─────────────────────────────────


def unit(text: str) -> dict[str, list[str]]:
    """A unit file's settings, ignoring comments and sections: key -> values in order."""
    found: dict[str, list[str]] = {}
    for line in text.splitlines():
        if line and not line.startswith(("#", "[")):
            key, _, value = line.partition("=")
            found.setdefault(key, []).append(value)
    return found


@pytest.mark.parametrize("name", setup_local.SERVICES)
def test_each_unit_gets_its_label_and_this_machine_s_folders(name, monkeypatch):
    found = {"node": "/home/alex/.nvm/versions/node/v22/bin/node", "livekit-server": "/usr/local/bin/livekit-server"}
    monkeypatch.setattr(setup_local.shutil, "which", found.get)
    monkeypatch.setattr(setup_local.Path, "home", lambda: Path("/home/alex"))
    text = setup_local.service_unit(name)
    settings = unit(text)
    assert "$" not in text and f"(com.jarvis.{name})" in settings["Description"][0]
    assert settings["WorkingDirectory"] == [str(ROOT)] and settings["Restart"] == ["always"]
    assert settings["StandardOutput"] == [f"append:{HOME_DIR}/logs/{name}.log"] == settings["StandardError"]
    assert settings["WantedBy"] == ["default.target"] and settings["Type"] == ["exec"]
    assert settings["Environment"][0].startswith('"PATH=/home/alex/.local/bin:')
    if name in ("tasks", "agent"):
        assert "/home/alex/.nvm/versions/node/v22/bin:/usr/local/bin" in settings["Environment"][0]
    if name == "livekit":
        assert settings["ExecStart"] == [f'"/usr/local/bin/livekit-server" --config "{HOME_DIR}/secrets/livekit.yaml"']


def test_livekit_falls_back_to_where_make_install_puts_it(monkeypatch):
    monkeypatch.setattr(setup_local.shutil, "which", lambda cmd: None)
    monkeypatch.setattr(setup_local.Path, "home", lambda: Path("/home/alex"))
    assert unit(setup_local.service_unit("livekit"))["ExecStart"][0].startswith('"/home/alex/.local/bin/livekit-server"')


def test_the_voice_comes_before_background_tasks_on_linux_too():
    agent, tasks = unit(setup_local.service_unit("agent")), unit(setup_local.service_unit("tasks"))
    assert tasks["Nice"] == ["10"] and tasks["IOSchedulingClass"] == ["idle"]
    assert "Nice" not in agent and "IOSchedulingClass" not in agent


def test_the_agent_keeps_the_machine_awake_when_it_may_and_starts_anyway_when_not():
    start = unit(setup_local.service_unit("agent"))["ExecStart"][0]
    assert start.startswith("/bin/sh -c ") and "systemd-inhibit --what=sleep:idle" in start
    assert start.count("-m jarvis.voice.agent start") == 2  # with the inhibitor, and without


def test_no_unit_names_a_home_folder_or_a_label():
    for name in setup_local.SERVICES:
        text = (ROOT / "deploy" / "systemd" / f"{name}.service").read_text()
        assert "/Users/" not in text and "/home/" not in text and "Description=" in text and "${LABEL}" in text


@pytest.fixture
def units(tmp_path, monkeypatch):
    """systemd backend with an isolated unit folder; systemctl and loginctl calls recorded, not made."""
    folder = tmp_path / "systemd" / "user"
    monkeypatch.setattr(setup_local, "MANAGER", "systemd")
    monkeypatch.setattr(setup_local, "SYSTEMD_USER", folder)
    monkeypatch.setattr(setup_local.shutil, "which", lambda cmd: f"/usr/bin/{cmd}" if cmd == "systemctl" else None)
    monkeypatch.setenv("USER", "alex")
    for var in setup_local.DESKTOP_ENV:
        monkeypatch.delenv(var, raising=False)
    calls, linger = [], {"value": "yes"}

    def run(cmd, **kwargs):
        calls.append(tuple(cmd))
        out = linger["value"] + "\n" if cmd[0] == "loginctl" else ""
        return setup_local.subprocess.CompletedProcess(cmd, 0, out, "")

    monkeypatch.setattr(setup_local.subprocess, "run", run)
    return folder, calls, linger


def systemctl(calls: list[tuple]) -> list[tuple]:
    """The systemctl --user calls, without the readiness check."""
    return [c[2:] for c in calls if c[:2] == ("systemctl", "--user") and c[2:] != ("show-environment",)]


def units_of(names=setup_local.SERVICES) -> list[str]:
    return [f"com.jarvis.{name}.service" for name in names]


def test_on_linux_services_are_user_units(units):
    folder, _, _ = units
    assert setup_local.installed("agent") == folder / "com.jarvis.agent.service"


def test_install_writes_every_unit_reloads_and_restarts_them(units, capsys):
    folder, calls, linger = units
    linger["value"] = "no"
    setup_local.install_services()
    assert sorted(p.name for p in folder.iterdir()) == sorted(units_of())
    assert systemctl(calls) == [("daemon-reload",)] + [
        call for u in units_of() for call in (("enable", u), ("restart", u))]
    out = capsys.readouterr().out
    assert "started com.jarvis.agent" in out and "sudo loginctl enable-linger alex" in out


def test_install_hands_the_services_the_desktop_session(units, monkeypatch):
    folder, calls, _ = units
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1000")
    setup_local.install_services()
    found = systemctl(calls)
    assert ("import-environment", "WAYLAND_DISPLAY", "XDG_RUNTIME_DIR") in found
    assert found.index(("import-environment", "WAYLAND_DISPLAY", "XDG_RUNTIME_DIR")) < found.index(("restart", units_of()[0]))


def test_with_lingering_on_there_is_no_hint(units, capsys):
    setup_local.install_services()
    assert "linger" not in capsys.readouterr().out


def test_start_installs_missing_units_and_leaves_the_rest(units):
    folder, calls, _ = units
    folder.mkdir(parents=True)
    (folder / "com.jarvis.agent.service").write_text("as installed")
    setup_local.start_services()
    assert (folder / "com.jarvis.agent.service").read_text() == "as installed"
    assert "Description=JARVIS web app" in (folder / "com.jarvis.web.service").read_text()
    assert systemctl(calls) == [("daemon-reload",)] + [
        call for u in units_of() for call in (("enable", u), ("start", u))]


def test_stop_stops_installed_units_only(units):
    folder, calls, _ = units
    folder.mkdir(parents=True)
    (folder / "com.jarvis.agent.service").write_text("current")
    setup_local.stop_services()
    assert systemctl(calls) == [("stop", "com.jarvis.agent.service")]


def test_units_under_an_old_prefix_are_retired(units, capsys):
    folder, calls, _ = units
    folder.mkdir(parents=True)
    old = folder / "com.someone.jarvis.tasks.service"
    old.write_text(f"[Service]\nStandardOutput=append:{ROOT}/logs/tasks.log\n")
    other = folder / "com.other.jarvis.tasks.service"
    other.write_text("[Service]\nStandardOutput=append:/home/alex/other-jarvis/logs/tasks.log\n")
    assert setup_local.stale() == [old]
    setup_local.start_services()
    assert systemctl(calls)[0] == ("disable", "--now", old.name) and not old.exists() and other.exists()
    assert "removed com.someone.jarvis.tasks, now com.jarvis.tasks" in capsys.readouterr().out


def test_without_systemd_it_says_how_to_turn_it_on(units, monkeypatch):
    def run(cmd, **kwargs):
        return setup_local.subprocess.CompletedProcess(cmd, 1, "", "Failed to connect to bus: No medium found")

    monkeypatch.setattr(setup_local.subprocess, "run", run)
    with pytest.raises(SystemExit) as exit:
        setup_local.start_services()
    said = str(exit.value.code)
    assert "systemd=true" in said and "/etc/wsl.conf" in said  # WSL2
    assert "No medium found" in said and "dbus-user-session" in said  # the actual error, and the SSH/su case


def test_a_unit_that_wont_start_fails_the_start(units, monkeypatch, capsys):
    _, calls, _ = units

    def run(cmd, **kwargs):
        calls.append(tuple(cmd))
        failed = cmd[2:3] == ["start"] and "agent" in cmd[3]
        return setup_local.subprocess.CompletedProcess(cmd, int(failed), "", "Job for agent failed.\n" * failed)

    monkeypatch.setattr(setup_local.subprocess, "run", run)
    with pytest.raises(SystemExit) as exit:
        setup_local.start_services()
    assert exit.value.code == 1
    assert "couldn't start com.jarvis.agent: Job for agent failed." in capsys.readouterr().err


def test_status_reads_systemd(units, monkeypatch):
    shown = {"livekit": "LoadState=loaded\nActiveState=active\nMainPID=42\nExecMainStatus=0\n",
             "agent": "LoadState=loaded\nActiveState=activating\nMainPID=0\nExecMainStatus=1\n",
             "web": "LoadState=not-found\nActiveState=inactive\nMainPID=0\nExecMainStatus=0\n"}

    def run(cmd, **kwargs):
        name = cmd[-1].removesuffix(".service").rpartition(".")[2]
        return setup_local.subprocess.CompletedProcess(cmd, 0 if name in shown else 1, shown.get(name, ""), "")

    monkeypatch.setattr(setup_local.subprocess, "run", run)
    assert setup_local.status() == [("com.jarvis.livekit", "running (pid 42)"),
                                    ("com.jarvis.agent", "stopped (exit 1)"),
                                    ("com.jarvis.web", "not loaded"), ("com.jarvis.tasks", "not loaded")]


def test_status_reads_launchd(monkeypatch):
    listed = "PID\tStatus\tLabel\n123\t0\tcom.jarvis.agent\n-\t78\tcom.jarvis.web\n-\t0\tcom.apple.Other\n"
    monkeypatch.setattr(setup_local.subprocess, "run",
                        lambda cmd, **kw: setup_local.subprocess.CompletedProcess(cmd, 0, listed, ""))
    assert setup_local.status() == [("com.jarvis.livekit", "not loaded"), ("com.jarvis.agent", "running (pid 123)"),
                                    ("com.jarvis.web", "stopped (exit 78)"), ("com.jarvis.tasks", "not loaded")]


def test_help_prints_usage_without_setting_anything_up(monkeypatch, capsys):
    monkeypatch.setattr(setup_local.sys, "argv", ["setup_local", "--help"])
    monkeypatch.setattr(setup_local, "write_atomic", lambda *a, **kw: pytest.fail("wrote .env"))
    setup_local.main()
    assert "--install" in capsys.readouterr().out


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
    tree = tmp_path / "home" / "data" / "worktrees" / "jarvis-7"  # JARVIS_HOME may be outside the checkout
    tree.mkdir(parents=True)
    (tree / ".git").write_text(f"gitdir: {tmp_path}/code/jarvis/.git/worktrees/jarvis-7\n")
    monkeypatch.setattr(config, "ROOT", tree)
    assert config.projects_home() == tmp_path / "code"  # a task's worktree goes by its main checkout
    monkeypatch.setattr(config, "ROOT", Path.home() / "jarvis")
    assert config.projects_home() == Path.home() / "Projects"  # never the whole home folder
