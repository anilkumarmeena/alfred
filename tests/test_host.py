"""Tests for host jobs: the login shell, the environment without Alfred's keys, jobs, and which processes are whose
(libproc on macOS, /proc on Linux)."""

import asyncio
import json
import os
import signal
import subprocess
import time
from types import SimpleNamespace

import pytest

from alfred.safety import host

MAC = SimpleNamespace(MAC=True, LINUX=False)
LINUX = SimpleNamespace(MAC=False, LINUX=True)


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


# ── The environment ─────────────────────────────────────────────


def test_jobs_get_none_of_alfred_keys_but_keep_the_ssh_agent(monkeypatch):
    monkeypatch.setattr(host, "dotenv_values", lambda path: {"ALFRED_PASSCODE": "x", "LIVEKIT_API_SECRET": "y"})
    for name, value in {"ALFRED_PASSCODE": "hunter2", "LIVEKIT_API_SECRET": "s", "SSH_AUTH_SOCK": "/tmp/agent",
                        "HOME_MADE": "ok"}.items():
        monkeypatch.setenv(name, value)
    env = host.scrubbed_env()
    assert env["HOME_MADE"] == "ok" and env["SSH_AUTH_SOCK"] == "/tmp/agent" and "PATH" in env
    assert "ALFRED_PASSCODE" not in env and "LIVEKIT_API_SECRET" not in env


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
    assert host.started_at(10) == 1005.0 and host.started_at(99) is None
    assert host._unchanged(10, 1004.0) and not host._unchanged(10, 900.0)  # a reused pid is never taken as its own


def test_without_a_boot_time_no_pid_is_ever_taken_as_its_own(fake_proc):
    (fake_proc / "stat").write_text("cpu 1 2 3\n")
    assert not host._unchanged(10, 1005.0)


def test_sessions_on_this_computer():
    """Real sessions (libproc on macOS, /proc on Linux): a shell leads one, and what it starts stays in it."""
    shell = subprocess.Popen(["/bin/sh", "-c", "sleep 30 & sleep 30 & wait"], start_new_session=True)
    at = time.time()
    try:
        until = time.monotonic() + 5
        while len(members := host.session_members(shell.pid)) < 3 and time.monotonic() < until:
            time.sleep(0.05)
        assert len(members) == 3 and shell.pid in members
        assert abs(host.started_at(shell.pid) - at) <= 2
    finally:
        os.killpg(shell.pid, signal.SIGKILL)
        shell.wait()
