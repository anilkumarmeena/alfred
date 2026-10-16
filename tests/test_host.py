"""Tests for host commands: the walls as text (Seatbelt profile, bubblewrap mounts; `make host-check` runs them),
env, time limits, jobs, and Linux's /proc process tracking. Tests that need a real bwrap run on Linux only."""

import asyncio
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import time
from types import SimpleNamespace

import pytest

from jarvis.core import platform, profile as profiles
from jarvis.core.config import DATA_DIR, ROOT, TASK_CACHE_DIR, WORKTREES_DIR
from jarvis.safety import host
from jarvis.safety.commands import HOME_SECRETS, SECRET_DIRS

MAC = SimpleNamespace(MAC=True, LINUX=False)
LINUX = SimpleNamespace(MAC=False, LINUX=True)

# ── The profile ─────────────────────────────────────────────────


def rules(action: str) -> str:
    """The profile's rules for one action, e.g. 'file-read*'."""
    return "\n".join(line for line in host.profile().splitlines() if line.startswith(f"(deny {action}"))


def test_the_profile_allows_by_default_and_denies_only_what_must_stay_closed():
    profile = host.profile()
    assert profile.startswith("(version 1)\n(allow default)")
    assert profile.count("(") == profile.count(")")  # balanced, or sandbox-exec refuses it
    assert "(allow" not in profile.replace("(allow default)", "")  # nothing is opened back up


def test_the_setuid_ps_that_could_read_mcp_servers_tokens_cant_run():
    assert '(deny process-exec (literal "/bin/ps"))' in host.profile()
    assert any("ps eww" in what and not should for what, _, should in host._mac_checks())


def test_secrets_are_unreadable_but_the_voice_session_logs_are_readable():
    reads = rules("file-read*")
    home = os.path.realpath(os.path.expanduser("~"))
    for secret in [str(ROOT / "secrets"), str(ROOT / ".env"), *(p.replace("~", home, 1) for p in HOME_SECRETS)]:
        assert f'(subpath "{os.path.realpath(secret)}")' in reads, secret
    assert all(re.escape(d) in reads for d in SECRET_DIRS) and r"\.env(\.(([^est/][^/]*)?|e([^x/][^/]*)?|" in reads and "client_secret" in reads
    assert f'(require-not (require-any (subpath "{os.path.realpath(profiles.current().voice_logs)}")' in reads


def test_ssh_can_check_known_hosts_but_never_read_a_key():
    reads = rules("file-read*")
    home = os.path.realpath(os.path.expanduser("~"))
    exempt = reads[reads.index("(require-not (require-any"):]
    for name in ("known_hosts", "known_hosts2", "config"):
        assert f'(literal "{home}/.ssh/{name}")' in exempt, name
    assert f'(subpath "{home}/.ssh")' in reads  # the rest of ~/.ssh, keys included, stays unreadable
    assert "id_ed25519" not in exempt and exempt.count("(subpath") == 1  # the only subpath exempt is the voice session logs
    assert f'(subpath "{home}/.ssh")' in rules("file-write*")  # and nothing there is writable


def test_pem_certificate_bundles_stay_readable_for_tls():
    reads = rules("file-read*")
    assert r"\.pem$" in reads and "site-packages" in reads and "cacert" in reads


def test_jarvis_own_files_and_start_up_files_cant_be_written():
    writes = rules("file-write*")
    root = os.path.realpath(ROOT)
    assert f'(require-all (subpath "{root}") (require-not (require-any' in writes
    for kept_open in (WORKTREES_DIR, TASK_CACHE_DIR, DATA_DIR / "tasks"):
        assert f'(subpath "{os.path.realpath(kept_open)}")' in writes
    assert f'(subpath "{root}/.git/hooks")' in writes and f'(literal "{root}/.git/config")' in writes
    assert "Library/LaunchAgents" in writes and ".zshrc" in writes


def test_the_runner_socket_is_out_of_reach():
    sock = os.path.realpath(DATA_DIR / "runner.sock")
    assert f'(deny network-outbound (remote unix-socket (path-literal "{sock}")))' in host.profile()


def test_paths_are_escaped_for_the_profile(tmp_path):
    odd = tmp_path / 'a "quoted" folder.x'
    odd.mkdir()
    profile = host.profile(home=odd)
    assert r'a \"quoted\" folder.x' in profile  # escaped in a string
    assert r"a \"quoted\" folder\.x" not in profile  # a regex never has a quote in it
    assert re.search(r'regex #"\^[^"]*folder\\\.x/', profile)


def test_commands_never_run_without_the_walls(monkeypatch):
    monkeypatch.setattr(host, "platform", MAC)
    monkeypatch.setattr(host, "SANDBOX_EXEC", "/nowhere/sandbox-exec")
    with pytest.raises(RuntimeError, match="can't run safely"):
        host.walled(["/bin/echo", "hi"])


def test_without_bubblewrap_linux_commands_never_run(monkeypatch):
    monkeypatch.setattr(host, "platform", LINUX)
    monkeypatch.setattr(host, "BWRAP", "/nowhere/bwrap")
    monkeypatch.setattr(host.shutil, "which", lambda name: None)
    with pytest.raises(RuntimeError, match="bubblewrap .*isn't installed.*apt install bubblewrap"):
        host.walled(["/bin/echo", "hi"])


def test_a_bwrap_that_cant_build_its_sandbox_refuses_saying_how_to_fix_it(monkeypatch, tmp_path):
    """Ubuntu 24.04's AppArmor, or user namespaces switched off."""
    bwrap = tmp_path / "bwrap"
    bwrap.write_text("#!/bin/sh\n")
    bwrap.chmod(0o755)
    monkeypatch.setattr(host, "platform", LINUX)
    monkeypatch.setattr(host, "BWRAP", str(bwrap))
    monkeypatch.setattr(host, "_bwrap_ready", "")
    monkeypatch.setattr(host.subprocess, "run", lambda argv, **kw: subprocess.CompletedProcess(
        argv, 1, "", "bwrap: setting up uid map: Permission denied"))
    with pytest.raises(RuntimeError, match="uid map: Permission denied.*apparmor_restrict_unprivileged_userns"):
        host.walled(["/bin/echo", "hi"])
    assert host._bwrap_ready == ""  # tried again next time, once it's fixed


def test_no_other_os_gets_commands_run_unwalled(monkeypatch):
    monkeypatch.setattr(host, "platform", SimpleNamespace(MAC=False, LINUX=False))
    with pytest.raises(RuntimeError, match="no walls"):
        host.walled(["/bin/echo", "hi"])


def test_the_login_shell_falls_back_to_bash_then_sh(monkeypatch):
    monkeypatch.setattr(host, "platform", LINUX)
    monkeypatch.setattr(host.pwd, "getpwuid", lambda uid: SimpleNamespace(pw_shell="/usr/bin/fish"))
    present = {"/bin/bash"}
    monkeypatch.setattr(host.os, "access", lambda path, mode: path in present)
    monkeypatch.setattr(host.shutil, "which", lambda name: None)
    assert host._login_shell() == ("/bin/bash", "-lc")  # fish isn't one to run sh-style commands in
    present.clear()
    assert host._login_shell() == ("/bin/sh", "-lc")
    present.add("/usr/bin/zsh")
    monkeypatch.setattr(host.pwd, "getpwuid", lambda uid: SimpleNamespace(pw_shell="/usr/bin/zsh"))
    assert host._login_shell() == ("/usr/bin/zsh", "-lc")  # the user's own, for their PATH
    monkeypatch.setattr(host, "platform", MAC)
    assert host._login_shell() == ("/bin/zsh", "-lc")


# ── The walls on Linux, as bubblewrap options ───────────────────


def mounts(args: list[str]) -> list[tuple[str, ...]]:
    """bwrap options as (option, operands...)."""
    arity = {"--bind": 2, "--ro-bind": 2, "--dev-bind-try": 2, "--symlink": 2, "--tmpfs": 1, "--remount-ro": 1,
             "--dev": 1, "--proc": 1, "--unshare-pid": 0}
    out, i = [], 0
    while i < len(args):
        n = arity[args[i]]
        out.append(tuple(args[i:i + n + 1]))
        i += n + 1
    return out


@pytest.fixture
def linux_home(tmp_path, monkeypatch):
    """A home with logins, start-up files, a project and a runtime folder with a session bus."""
    home = tmp_path / "home"
    for path, text in {".bashrc": "x", ".ssh/id_ed25519": "KEY", ".ssh/known_hosts": "h", ".ssh/config": "c",
                       ".netrc": "n", "proj/.env": "S=1", "proj/.env.example": "S=", "proj/certs/ca.pem": "ca",
                       "proj/server.pem": "k",
                       "proj/node_modules/pkg/.env": "e", "proj/src/app.py": "", "dotfiles/zshrc": "z",
                       ".config/app/settings": "", ".config/systemd/user/a.service": "", ".ssh/agent.sock": ""}.items():
        (home / path).parent.mkdir(parents=True, exist_ok=True)
        (home / path).write_text(text)
    (home / ".zshrc").symlink_to(home / "dotfiles/zshrc")
    run = tmp_path / "run"
    for name in ("bus", "pipewire-0", "systemd/private", "gcr/ssh"):
        (run / name).parent.mkdir(parents=True, exist_ok=True)
        (run / name).write_text("")
    (tmp_path / "dbus-session").write_text("")
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(run))
    monkeypatch.setenv("SSH_AUTH_SOCK", str(home / ".ssh/agent.sock"))
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", f"unix:path={tmp_path / 'dbus-session'},guid=1;unix:abstract=/x")
    return os.path.realpath(home), os.path.realpath(run), os.path.realpath(tmp_path / "dbus-session")


def where(ops, path: str) -> list[tuple[str, ...]]:
    return [op for op in ops if op[-1] == path]


def test_bwrap_mounts_everything_then_hides_and_locks_what_seatbelt_denies(linux_home):
    home, run, bus = linux_home
    args = host.bwrap_args(home)
    ops = mounts(args)
    assert args[:8] == ["--unshare-pid", "--bind", "/", "/", "--dev", "/dev", "--proc", "/proc"]
    assert "--new-session" not in args and "--die-with-parent" not in args  # jobs keep their session and outlive it
    # Home: rebuilt read-only, so a missing start-up file can't be made; its entries come back writable
    assert where(ops, home) == [("--tmpfs", home), ("--remount-ro", home)]
    assert where(ops, f"{home}/proj") == [("--bind", f"{home}/proj", f"{home}/proj")]
    assert not any(f"{home}/.bash_profile" in op for op in ops)
    assert where(ops, f"{home}/.bashrc") == [("--ro-bind", f"{home}/.bashrc", f"{home}/.bashrc")]
    assert where(ops, f"{home}/.zshrc") == [("--symlink", f"{home}/dotfiles/zshrc", f"{home}/.zshrc")]
    assert ("--ro-bind", f"{home}/dotfiles/zshrc", f"{home}/dotfiles/zshrc") in ops  # a linked dotfile's target too
    assert where(ops, f"{home}/.config") == [("--tmpfs", f"{home}/.config"), ("--remount-ro", f"{home}/.config")]
    assert ("--bind", f"{home}/.config/app", f"{home}/.config/app") in ops
    assert ("--ro-bind", f"{home}/.config/systemd", f"{home}/.config/systemd") in ops  # JARVIS's services are there
    # Secrets: hidden, keys included, but ssh can check hosts and sign through the agent
    assert where(ops, f"{home}/.ssh") == [("--tmpfs", f"{home}/.ssh"), ("--remount-ro", f"{home}/.ssh")]
    for shown in ("known_hosts", "config", "agent.sock"):
        assert where(ops, f"{home}/.ssh/{shown}") == [("--ro-bind", f"{home}/.ssh/{shown}", f"{home}/.ssh/{shown}")]
    assert "id_ed25519" not in " ".join(args)
    for secret in (".netrc", "proj/.env", "proj/server.pem"):
        assert where(ops, f"{home}/{secret}") == [("--ro-bind", "/dev/null", f"{home}/{secret}")], secret
    assert not where(ops, f"{home}/proj/certs/ca.pem")  # CA bundles stay readable for TLS
    assert not where(ops, f"{home}/proj/.env.example")  # as do env templates
    # The session bus (Secret Service, systemd-run) is left out; the rest of the runtime folder stays
    assert where(ops, run) == [("--tmpfs", run)]  # writable: sockets get made there
    assert ("--bind", f"{run}/pipewire-0", f"{run}/pipewire-0") in ops and ("--bind", f"{run}/gcr", f"{run}/gcr") in ops
    assert not any(op[-1] in (f"{run}/bus", f"{run}/systemd") for op in ops)
    assert where(ops, bus) == [("--ro-bind", "/dev/null", bus)]


def test_bwrap_keeps_jarvis_own_files_read_only_and_the_runner_socket_out(linux_home):
    home, _, _ = linux_home
    ops = mounts(host.bwrap_args(home))
    root, data = os.path.realpath(ROOT), os.path.realpath(DATA_DIR)
    assert ("--ro-bind", root, root) in ops
    assert where(ops, data) == [("--tmpfs", data), ("--remount-ro", data)]  # rebuilt: a remade socket stays out
    assert not any("runner.sock" in part for op in ops for part in op)
    for kept_open in (WORKTREES_DIR, TASK_CACHE_DIR, DATA_DIR / "tasks"):
        if kept_open.exists():
            assert ("--bind", os.path.realpath(kept_open), os.path.realpath(kept_open)) in ops
    if (ROOT / ".git").is_dir():
        assert ("--bind", f"{root}/.git", f"{root}/.git") in ops
        assert ("--ro-bind", f"{root}/.git/hooks", f"{root}/.git/hooks") in ops
        assert ("--ro-bind", f"{root}/.git/config", f"{root}/.git/config") in ops
    if (ROOT / ".env").exists():
        assert ("--ro-bind", "/dev/null", f"{root}/.env") in ops
    if (ROOT / "secrets").is_dir():
        assert ("--tmpfs", f"{root}/secrets") in ops and ("--remount-ro", f"{root}/secrets") in ops
        logs = os.path.realpath(profiles.current().voice_logs)
        if os.path.exists(logs):
            assert ("--ro-bind", logs, logs) in ops  # the voice session logs stay readable


def test_bwrap_mounts_a_folder_before_anything_inside_it(linux_home):
    """A mount hides what's already mounted below it, so the shallower must go first."""
    home, _, _ = linux_home
    ops = [op for op in mounts(host.bwrap_args(home)) if op[0] not in ("--remount-ro", "--unshare-pid")]
    for i, op in enumerate(ops):
        for later in ops[i + 1:]:
            assert not op[-1].startswith(later[-1].rstrip("/") + "/"), (op, later)


def test_nothing_under_a_hidden_folder_comes_back(tmp_path, monkeypatch):
    """A project inside a folder named `secrets` stays hidden, writable worktree or not."""
    home = tmp_path / "home"
    (home / "work/secrets/app").mkdir(parents=True)
    ops = mounts(host.bwrap_args(home))
    hidden = os.path.realpath(home / "work/secrets")
    assert ("--tmpfs", hidden) in ops and not any(op[-1].startswith(hidden + "/") for op in ops)


def test_the_secret_walk_is_bounded_and_says_so(tmp_path, monkeypatch, caplog):
    for i in range(10):
        (tmp_path / f"d{i}").mkdir()
        (tmp_path / f"d{i}" / ".env").write_text("")
    (tmp_path / "node_modules/x").mkdir(parents=True)
    (tmp_path / "node_modules/x/.env").write_text("")
    assert len(host._walk([str(tmp_path)], set())) == 10  # node_modules isn't looked into
    monkeypatch.setattr(host, "_WALK_ENTRIES", 5)
    with caplog.at_level(logging.WARNING, "jarvis.safety.host"):
        assert len(host._walk([str(tmp_path)], set())) < 10
    assert "secret walk stopped" in caplog.text


def test_only_path_session_buses_can_be_hidden(monkeypatch):
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:path=/run/user/1000/bus;unix:abstract=/tmp/dbus-x,guid=1")
    assert host._bus_paths() == ["/run/user/1000/bus"]


def test_linux_host_commands_lose_the_session_bus_address(monkeypatch):
    monkeypatch.setattr(host, "dotenv_values", lambda path: {})
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:path=/run/user/1000/bus")
    monkeypatch.setattr(host, "platform", LINUX)
    assert "DBUS_SESSION_BUS_ADDRESS" not in host.task_env()
    monkeypatch.setattr(host, "platform", MAC)
    assert "DBUS_SESSION_BUS_ADDRESS" in host.task_env()


def test_linux_checks_cover_what_the_mac_ones_do():
    linux = {what for what, _, _ in host._linux_checks()}
    assert {"reads JARVIS's .env", "reads an SSH key file", "reaches the SSH agent", "writes tasks.db",
            "reaches the runner's socket", "reads the voice session logs", "writes the task cache"} <= linux
    assert any("environments" in what for what in linux) and any("session bus" in what for what in linux)


def test_ssh_remotes_name_where_they_sign_in_and_other_remotes_nothing():
    assert host._ssh_remote("git@git.example.com:team/app.git") == "git@git.example.com"
    assert host._ssh_remote("ssh://git@git.example.com:2222/team/app.git") == "ssh://git@git.example.com:2222"
    for other in ("https://git.example.com/team/app.git", "/srv/repos/app.git", "../app", "file:///srv/app.git"):
        assert host._ssh_remote(other) == ""


def test_the_ssh_check_signs_in_to_the_users_own_git_hosts_or_is_left_out(monkeypatch, tmp_path):
    from jarvis.tasks import projects

    repo = tmp_path / "app"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    for name, url in (("origin", "git@git.example.com:team/app.git"), ("mirror", "https://git.example.com/app.git"),
                      ("fork", "git@git.example.com:me/app.git")):
        subprocess.run(["git", "-C", str(repo), "remote", "add", name, url], check=True)
    monkeypatch.setattr(projects, "load", lambda: [])
    monkeypatch.setattr(host, "ROOT", repo)
    (what, command, should), = host._ssh_checks()
    assert what == "signs in to git@git.example.com over SSH, through the agent" and should
    assert "git@git.example.com" in command and "255" in command
    monkeypatch.setattr(host, "ROOT", tmp_path / "elsewhere")  # no repo, so no remotes
    assert host._ssh_checks() == []


# ── The environment ─────────────────────────────────────────────


def test_host_commands_get_none_of_jarvis_keys(monkeypatch):
    monkeypatch.setattr(host, "dotenv_values", lambda path: {"JARVIS_PASSCODE": "x", "LIVEKIT_API_SECRET": "y"})
    for name, value in {"JARVIS_PASSCODE": "hunter2", "LIVEKIT_API_SECRET": "s", "GITHUB_TOKEN": "t",
                        "SOME_PASSWORD": "p", "SSH_AUTH_SOCK": "/tmp/agent", "OPENAI_API_KEY": "k", "HOME_MADE": "ok"}.items():
        monkeypatch.setenv(name, value)
    env = host.task_env()
    assert env["HOME_MADE"] == "ok" and "PATH" in env
    for gone in ("JARVIS_PASSCODE", "LIVEKIT_API_SECRET", "GITHUB_TOKEN", "SOME_PASSWORD", "OPENAI_API_KEY"):
        assert gone not in env, gone
    assert "SSH_AUTH_SOCK" in host.scrubbed_env() and "JARVIS_PASSCODE" not in host.scrubbed_env()  # the voice side keeps SSH_AUTH_SOCK, not secrets


def test_host_commands_keep_the_ssh_agent(monkeypatch):
    """So an approved git push can sign with the keys, which stay unreadable."""
    monkeypatch.setattr(host, "dotenv_values", lambda path: {})
    monkeypatch.setenv("SSH_AUTH_SOCK", "/private/tmp/com.apple.launchd.x/Listeners")
    monkeypatch.setenv("GIT_AUTH_TOKEN", "t")
    env = host.task_env()
    assert env["SSH_AUTH_SOCK"] == "/private/tmp/com.apple.launchd.x/Listeners" and "GIT_AUTH_TOKEN" not in env


# ── Running ─────────────────────────────────────────────────────


def run(command: str, cwd, timeout: float = 10):
    return asyncio.run(host.run(command, cwd, timeout, walls=False, shell=("/bin/sh", "-c")))


def test_a_command_runs_in_its_folder_and_reports_its_exit_code(tmp_path, monkeypatch):
    monkeypatch.setattr(host, "dotenv_values", lambda path: {"JARVIS_TEST_SECRET": "x"})
    monkeypatch.setenv("JARVIS_TEST_SECRET", "hunter2")
    ran = run('pwd; echo "secret=$JARVIS_TEST_SECRET"; echo oops >&2; exit 3', tmp_path)
    assert ran.code == 3 and str(tmp_path.resolve()) in ran.output and "oops" in ran.output
    assert "hunter2" not in ran.output
    assert ran.text().startswith("[exit code 3,")


def test_a_command_past_its_time_limit_is_stopped(tmp_path):
    ran = run("echo started; sleep 30", tmp_path, timeout=0.5)
    assert ran.code is None and "started" in ran.output and ran.seconds < 10
    assert "time limit" in ran.text()


def test_long_output_keeps_its_start_and_end(tmp_path):
    ran = run("echo FIRST; yes middle | head -c 200000; echo; echo LAST", tmp_path)
    assert ran.output.startswith("FIRST") and ran.output.rstrip().endswith("LAST")
    assert "characters cut" in ran.output and len(ran.output) < host.MAX_OUTPUT + 200


# ── Jobs ────────────────────────────────────────────────────────


@pytest.fixture
def jobs(tmp_path):
    jobs = host.Jobs(tmp_path / "jobs", shell=("/bin/sh", "-c"), notify=False)
    jobs.poll = 0.02
    return jobs


def test_a_task_follows_its_own_jobs(jobs, tmp_path):
    ended = []
    jobs.ended = lambda job, code: ended.append((job["id"], code))

    async def main():
        mine = jobs.launch("echo building; sleep 0.2; echo built", "the build", tmp_path, task=5)
        jobs.launch("true", "someone else's", tmp_path, task=6)
        quick = await jobs.wait(mine["id"], 0.01, task=5)
        done = await jobs.wait(mine["id"], 5, task=5)
        await asyncio.sleep(0.1)
        return quick, done

    quick, done = asyncio.run(main())
    assert quick["state"] == "running"
    assert done["state"] == "done" and "built" in done["output_tail"]
    assert [j["title"] for j in jobs.status(task=5)] == ["the build"]
    assert sorted(ended) == [(1, 0), (2, 0)]
    assert json.loads((tmp_path / "jobs" / "1.json").read_text())["task"] == 5
    with pytest.raises(ValueError):
        asyncio.run(jobs.wait(2, 1, task=5))  # not its job


@pytest.mark.skipif(not os.path.exists("/bin/zsh"), reason="needs zsh")
def test_a_job_under_zsh_records_how_it_ended(tmp_path):
    """Under zsh `status` is read-only; the wrapper must still write the exit file."""
    jobs = host.Jobs(tmp_path / "jobs", shell=("/bin/zsh", "-c"), notify=False)
    jobs.poll = 0.02

    async def main():
        ok = jobs.launch("echo fine", "a quick one", tmp_path)
        bad = jobs.launch("echo oops; exit 3", "a failing one", tmp_path)
        return await jobs.wait(ok["id"], 5), await jobs.wait(bad["id"], 5)

    ok, bad = asyncio.run(main())
    assert ok["state"] == "done" and "fine" in ok["output_tail"]
    assert bad["state"] == "failed, exit code 3"
    assert (tmp_path / "jobs" / "1.exit").read_text().strip() == "0"
    assert "read-only" not in ok["output_tail"] + bad["output_tail"]


def test_stopping_a_task_stops_its_jobs(jobs, tmp_path):
    ended = []
    jobs.ended = lambda job, code: ended.append(code)

    async def main():
        jobs.launch("sleep 30", "a long build", tmp_path, task=5)
        stopped = jobs.stop_all(5), jobs.stop_all(6)
        for _ in range(250):  # until its watcher sees it end
            if ended:
                break
            await asyncio.sleep(0.02)
        return stopped

    assert asyncio.run(main()) == ([1], [])
    assert ended == [-9] and jobs.status()[0]["state"] != host.RUNNING


def test_a_job_needs_a_real_folder(jobs, tmp_path):
    with pytest.raises(ValueError, match="isn't a folder"):
        jobs.launch("ls", "a listing", tmp_path / "missing")


# ── Which processes are whose, on Linux (/proc) ─────────────────


def stat_line(pid: int, name: str, state: str, sid: int, ticks: int) -> str:
    """A /proc/<pid>/stat line: fields 3 (state), 6 (session) and 22 (start, in clock ticks since boot) matter."""
    return f"{pid} ({name}) {state} 1 {sid} {sid} " + " ".join(["0"] * 15) + f" {ticks} 0 0\n"


@pytest.fixture
def fake_proc(tmp_path, monkeypatch):
    proc = tmp_path / "proc"
    proc.mkdir()
    (proc / "stat").write_text("cpu 1 2 3\nbtime 1000\nprocesses 9\n")
    for pid, name, state, sid, ticks in [(10, "sh", "S", 10, 500), (11, "sleep", "S", 10, 700),
                                         (12, "odd) (name", "R", 10, 600), (13, "gone", "Z", 10, 650),
                                         (20, "other", "S", 20, 100)]:
        (proc / str(pid)).mkdir()
        (proc / str(pid) / "stat").write_text(stat_line(pid, name, state, sid, ticks))
    (proc / "self").mkdir()
    monkeypatch.setattr(host, "_PROC", proc)
    monkeypatch.setattr(host, "platform", LINUX)
    monkeypatch.setattr(host, "_clock_ticks", lambda: 100)
    return proc


def test_linux_sessions_come_from_proc(fake_proc):
    assert sorted(host._pids()) == [10, 11, 12, 13, 20]
    assert sorted(host.session_members(10)) == [10, 11, 12]  # not the zombie, not another session's
    assert host.lingering(10) == [(12, "odd) (name"), (13, "gone"), (11, "sleep")]  # oldest first
    assert host.started_at(10) == 1005.0 and host.started_at(99) is None
    assert host._unchanged(10, 1004.0) and not host._unchanged(10, 900.0)  # a reused pid is never taken as its own


def test_without_a_boot_time_no_pid_is_ever_taken_as_its_own(fake_proc):
    (fake_proc / "stat").write_text("cpu 1 2 3\n")
    assert not host._unchanged(10, 1005.0)


def test_sessions_on_this_computer():
    """Real sessions (libproc on macOS, /proc on Linux): a shell leads one, and what it leaves behind stays in it."""
    shell = subprocess.Popen(["/bin/sh", "-c", "sleep 30 & sleep 30 & wait"], start_new_session=True)
    at = time.time()
    try:
        until = time.monotonic() + 5
        while len(left := host.lingering(shell.pid)) < 2 and time.monotonic() < until:
            time.sleep(0.05)
        assert [name for _, name in left] == ["sleep", "sleep"]
        assert abs(host.started_at(shell.pid) - at) <= 2
        assert sorted(host.session_members(shell.pid)) == sorted([shell.pid, *(pid for pid, _ in left)])
        assert all(host.in_sessions(pid, [(shell.pid, at)]) for pid, _ in left)
        assert not host.in_sessions(left[0][0], [(shell.pid, at - 3600)])  # a leader that started another time
    finally:
        os.killpg(shell.pid, signal.SIGKILL)
        shell.wait()


# ── The walls for real, on Linux ────────────────────────────────


def _bwrap_works() -> bool:
    if not platform.LINUX or not shutil.which("bwrap"):
        return False
    try:
        host._bwrap()
    except RuntimeError:
        return False
    return True


needs_bwrap = pytest.mark.skipif(not _bwrap_works(), reason="needs Linux with a working bubblewrap")


def walled_run(command: str, home, cwd) -> subprocess.CompletedProcess:
    return subprocess.run(host.walled(["/bin/sh", "-c", command], home=home), cwd=cwd, env=host.task_env(),
                          capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL, check=False)


@needs_bwrap
def test_bwrap_walls_hold_for_real(linux_home, tmp_path):
    home, _, _ = linux_home
    works = lambda command: walled_run(command, home, tmp_path).returncode == 0
    assert not works(f"cat {home}/.ssh/id_ed25519") and works(f"cat {home}/.ssh/known_hosts")
    assert not works(f"ls -A {home}/.ssh | grep -q id_ed25519")
    assert not works(f"grep -q . {home}/.netrc") and not works(f"grep -q S= {home}/proj/.env")
    assert works(f"cat {home}/proj/certs/ca.pem")
    assert not works(f"touch {home}/.bash_profile") and not os.path.exists(f"{home}/.bash_profile")
    assert not works(f"echo x >> {home}/.bashrc") and not works(f"echo x >> {home}/dotfiles/zshrc")
    assert not works(f"mkdir {home}/.config/autostart")
    assert works(f"touch {home}/proj/new") and os.path.exists(f"{home}/proj/new")  # the rest of home stays open
    assert not works(f"touch {ROOT}/.host-test")
    assert works(f"touch {TASK_CACHE_DIR}/.host-test && rm {TASK_CACHE_DIR}/.host-test")
    assert not works(f"head -c1 /proc/{os.getpid()}/environ")  # other processes are out of sight
    assert int(walled_run("ls /proc | grep -c '^[0-9]'", home, tmp_path).stdout) < 10
    assert not works(f"test -e {os.environ['XDG_RUNTIME_DIR']}/bus")
    assert not works("sudo -n true")


@needs_bwrap
def test_a_walled_job_is_stopped_whole_even_what_left_its_session(tmp_path):
    """Killing the pid namespace's init ends a process that called setsid, which session tracking alone would miss."""
    jobs = host.Jobs(tmp_path / "jobs", shell=("/bin/sh", "-c"), notify=False, walls=True)
    jobs.poll, jobs.grace = 0.02, 0.5
    marker = "31.4159"

    def leftovers() -> list[str]:
        found = []
        for cmdline in host._PROC.glob("[0-9]*/cmdline"):
            try:
                if marker in cmdline.read_bytes().decode(errors="replace"):
                    found.append(cmdline.parent.name)
            except OSError:
                pass
        return found

    async def main():
        job = jobs.launch(f"setsid sleep {marker} & sh -c 'trap \"\" TERM; sleep {marker}' & wait", "stubborn",
                          tmp_path)
        for _ in range(250):
            if len(leftovers()) >= 2:
                break
            await asyncio.sleep(0.02)
        assert len(leftovers()) >= 2
        await jobs.stop(job["id"])
        await asyncio.sleep(0.5)

    asyncio.run(main())
    assert leftovers() == []


@needs_bwrap
def test_a_walled_command_that_leaves_something_running_ends_at_its_time_limit(tmp_path):
    ran = asyncio.run(host.run("echo started; sleep 30 >/dev/null 2>&1 &", tmp_path, timeout=2,
                               shell=("/bin/sh", "-c")))
    assert ran.code is None and "started" in ran.output and ran.seconds < 15
