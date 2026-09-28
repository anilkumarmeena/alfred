"""Tests for commands tasks run on the Mac itself: the Seatbelt profile around them, the environment they
get, running with a time limit, and long jobs owned by a task. The profile itself is only checked as text
here; `make host-check` runs it for real. Run with: make test"""

import asyncio
import json
import os
import re

import pytest

from jarvis.core import profile as profiles
from jarvis.core.config import DATA_DIR, ROOT, TASK_CACHE_DIR, WORKTREES_DIR
from jarvis.safety import host
from jarvis.safety.commands import HOME_SECRETS, SECRET_DIRS

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
    assert any("ps eww" in what and not should for what, _, should in host._checks())


def test_secrets_are_unreadable_but_the_voice_session_logs_are_readable():
    reads = rules("file-read*")
    home = os.path.realpath(os.path.expanduser("~"))
    for secret in [str(ROOT / "secrets"), str(ROOT / ".env"), *(p.replace("~", home, 1) for p in HOME_SECRETS)]:
        assert f'(subpath "{os.path.realpath(secret)}")' in reads, secret
    assert all(re.escape(d) in reads for d in SECRET_DIRS) and r"\.env(\.[^/]*)?" in reads and "client_secret" in reads
    assert f'(require-not (require-any (subpath "{os.path.realpath(profiles.current().voice_logs)}")' in reads


def test_ssh_can_check_known_hosts_but_never_read_a_key():
    reads = rules("file-read*")
    home = os.path.realpath(os.path.expanduser("~"))
    exempt = reads[reads.index("(require-not (require-any"):]
    for name in ("known_hosts", "known_hosts2", "config"):
        assert f'(literal "{home}/.ssh/{name}")' in exempt, name
    assert f'(subpath "{home}/.ssh")' in reads  # everything else there, the keys included
    assert "id_ed25519" not in exempt and exempt.count("(subpath") == 1  # the voice session logs, nothing else
    assert f'(subpath "{home}/.ssh")' in rules("file-write*")  # nor can anything there be changed


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
    assert r'a \"quoted\" folder.x' in profile  # in a string
    assert r"a \"quoted\" folder\.x" not in profile  # a regex never has a quote in it
    assert re.search(r'regex #"\^[^"]*folder\\\.x/', profile)


def test_commands_never_run_without_the_walls(monkeypatch):
    monkeypatch.setattr(host, "SANDBOX_EXEC", "/nowhere/sandbox-exec")
    with pytest.raises(RuntimeError, match="can't run safely"):
        host.walled(["/bin/echo", "hi"])


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
    assert "SSH_AUTH_SOCK" in host.scrubbed_env() and "JARVIS_PASSCODE" not in host.scrubbed_env()  # the voice side's


def test_host_commands_keep_the_ssh_agent(monkeypatch):
    """So an approved git push works: the agent signs with the keys, which stay unreadable."""
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
    """Jobs really run under zsh, where `status` is read-only: the wrapper must still write the exit file."""
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
        for _ in range(250):  # until its watcher sees it go
            if ended:
                break
            await asyncio.sleep(0.02)
        return stopped

    assert asyncio.run(main()) == ([1], [])
    assert ended == [-9] and jobs.status()[0]["state"] != host.RUNNING


def test_a_job_needs_a_real_folder(jobs, tmp_path):
    with pytest.raises(ValueError, match="isn't a folder"):
        jobs.launch("ls", "a listing", tmp_path / "missing")
