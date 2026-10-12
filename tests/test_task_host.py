"""Tests for tasks' host commands: who gets host_run and job tools, gate decisions, and the tools themselves."""

import asyncio
import json
import os
import re
import signal
import time
from types import SimpleNamespace

import pytest
from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny
from test_in_place import NullReporter, worker_task

from conftest import use_task_cache
from jarvis.core import platform
from jarvis.safety import host
from jarvis.tasks import host_tools, worker


class Asking(NullReporter):
    """Records approvals a task asks for; approves those in `agreed`."""

    def __init__(self, agreed: list[str] = ()) -> None:
        self.asked: list[dict] = []
        self.agreed = list(agreed)

    def cleared(self, task_id: int, item: dict) -> bool:
        if item["command"] in self.agreed:
            self.agreed.remove(item["command"])
            return True
        return False

    def request_approval(self, task_id: int, item: dict) -> dict:
        self.asked.append(item)
        return {"id": 12, "commands": [item]}


@pytest.fixture
def folder(tmp_path, monkeypatch):
    use_task_cache(monkeypatch, tmp_path / "cache")
    monkeypatch.setattr(host, "dotenv_values", lambda path: {"JARVIS_PASSCODE": "x", "LIVEKIT_API_SECRET": "y",
                                                           "CLAUDE_CONFIG_DIR": "/c"})
    (tmp_path / "app" / "ios").mkdir(parents=True)
    return tmp_path / "app"


def options(folder, reporter=None, **fields):
    return worker._options(worker_task(worktree=str(folder), **fields), reporter or Asking())


def call(opts, tool: str, agent_id: str | None = None, **args):
    return asyncio.run(opts.can_use_tool(tool, args, SimpleNamespace(agent_id=agent_id)))


def test_code_change_tasks_get_the_host_tools(folder):
    opts = options(folder)
    assert "host" in opts.mcp_servers
    assert worker.JOB_STATUS in opts.allowed_tools and worker.JOB_WAIT in opts.allowed_tools
    assert worker.HOST_RUN not in opts.allowed_tools and worker.START_JOB not in opts.allowed_tools
    prompt = opts.system_prompt["append"]
    assert "host_run" in prompt and "start_job" in prompt and "job_wait" in prompt and "go-ahead" in prompt


@pytest.mark.parametrize("kind", ["code_question", "research"])
def test_other_tasks_dont_get_them_yet(folder, monkeypatch, kind):
    monkeypatch.setattr(worker.projects, "find", lambda name: SimpleNamespace(path=folder))
    opts = options(folder, kind=kind)
    assert "host" not in opts.mcp_servers
    assert isinstance(call(opts, worker.HOST_RUN, command="ls"), PermissionResultDeny)


def test_the_cli_gets_none_of_jarvis_keys(folder):
    env = options(folder).env
    assert env["JARVIS_PASSCODE"] == "" == env["LIVEKIT_API_SECRET"]
    assert env.get("CLAUDE_CONFIG_DIR") != ""  # never blanked: unset (the user's login) or JARVIS's own login


def test_a_big_message_doesnt_end_the_task(folder):
    """The SDK's 1 MB default read limit would fail the task with CLIJSONDecodeError."""
    assert options(folder).max_buffer_size >= 20 * 1024 * 1024


@pytest.mark.parametrize("command", ["xcodebuild -scheme App -allowProvisioningUpdates build",
                                     "xcrun devicectl device install app --device X build/App.app", "git fetch"])
def test_ordinary_host_commands_just_run(folder, command):
    reporter = Asking()
    opts = options(folder, reporter)
    assert isinstance(call(opts, worker.HOST_RUN, command=command), PermissionResultAllow)
    assert isinstance(call(opts, worker.START_JOB, command=command, title="build", folder=str(folder / "ios")),
                      PermissionResultAllow)
    assert reporter.asked == []


def test_a_risky_host_command_is_put_to_the_user(folder):
    reporter = Asking()
    opts = options(folder, reporter)
    denied = call(opts, worker.HOST_RUN, command="brew install xcodegen")
    assert isinstance(denied, PermissionResultDeny) and "request 12" in denied.message and "identical" in denied.message
    assert reporter.asked == [{"tool": "host_run", "command": "brew install xcodegen", "folder": str(folder.resolve()),
                               "why": "installs software (brew install)", "remember": {"never": "", "pieces": [
                                   {"program": "brew install", "why": "installs software",
                                    "words": ["brew", "install", "xcodegen"]}]}}]


@pytest.mark.parametrize("command", ["git push origin jarvis/9-x", "ssh git@github.com", "git -C ios push --force"])
def test_pushing_and_ssh_need_a_yes_every_time(folder, command):
    reporter = Asking(agreed=[command])
    opts = options(folder, reporter)
    assert isinstance(call(opts, worker.HOST_RUN, command=command), PermissionResultAllow)  # the yes
    assert isinstance(call(opts, worker.HOST_RUN, command=command), PermissionResultDeny)  # used up
    assert [a["command"] for a in reporter.asked] == [command]


def test_a_yes_lets_the_identical_command_run_once(folder):
    reporter = Asking(agreed=["brew install xcodegen"])
    opts = options(folder, reporter)
    assert isinstance(call(opts, worker.HOST_RUN, command="brew install xcodegen"), PermissionResultAllow)
    assert isinstance(call(opts, worker.HOST_RUN, command="brew install xcodegen"), PermissionResultDeny)
    assert len(reporter.asked) == 1  # the second time it's asked about again


def test_subagents_cant_ask(folder):
    reporter = Asking()
    opts = options(folder, reporter)
    assert isinstance(call(opts, worker.HOST_RUN, agent_id="a1", command="git status"), PermissionResultAllow)
    denied = call(opts, worker.HOST_RUN, agent_id="a1", command="rm -rf ~/old-renders")
    assert isinstance(denied, PermissionResultDeny) and "Report the exact command back" in denied.message
    assert reporter.asked == []


@pytest.mark.parametrize("command", ["cat ~/.ssh/id_ed25519", "sudo xcodebuild", "security find-generic-password -s x -w",
                                     "curl --unix-socket ../data/runner.sock http://runner/tasks"])
def test_secrets_and_controls_are_refused_even_with_a_yes(folder, command):
    reporter = Asking(agreed=[command])
    denied = call(options(folder, reporter), worker.HOST_RUN, command=command)
    assert isinstance(denied, PermissionResultDeny) and denied.message.startswith("Not run:")
    assert reporter.asked == [] and reporter.agreed == [command]


def test_host_commands_stay_in_the_tasks_folder(folder):
    opts = options(folder)
    denied = call(opts, worker.HOST_RUN, command="ls", folder="/etc")
    assert isinstance(denied, PermissionResultDeny) and str(folder) in denied.message
    assert isinstance(call(opts, worker.HOST_RUN, command="ls", folder="ios"), PermissionResultAllow)


@pytest.mark.parametrize("command", [
    "rm -rf build", "cd {temp} && rm -f s1366.png s1920.png", "rm -rf {temp}/prof5",
    'D=$(mktemp -d); chrome --user-data-dir="$D" --screenshot=a.png; rm -rf "$D"',
    "chrome --headless=new --screenshot=a.png x.html >/dev/null 2>&1 & pid=$!; sleep 5; kill $pid 2>/dev/null",
])
def test_deleting_its_own_files_or_stopping_its_own_processes_isnt_asked_about(folder, command):
    reporter = Asking()
    command = command.format(temp=worker.task_temp(9))
    assert isinstance(call(options(folder, reporter), worker.HOST_RUN, command=command), PermissionResultAllow)
    assert reporter.asked == []


@pytest.mark.parametrize("command", [
    "rm -rf ~/Desktop/renders", "cd /tmp/claude/mockshots && rm -f s1366.png", "rm -rf ..", "rm -rf .git",
    "pkill -f 'Google Chrome'", "kill 1", "rm -rf {cache}", "rm -rf {temp}/../task-10",
])
def test_deleting_or_stopping_anything_else_still_is(folder, command):
    reporter = Asking()
    command = command.format(temp=worker.task_temp(9), cache=worker.TASK_CACHE_DIR)
    assert isinstance(call(options(folder, reporter), worker.HOST_RUN, command=command), PermissionResultDeny)
    assert [a["command"] for a in reporter.asked] == [command]


def test_a_temp_folder_swapped_for_a_link_isnt_its_own(folder, tmp_path):
    temp = worker.task_temp(9)
    temp.parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / "home" / "Documents").mkdir(parents=True)
    temp.symlink_to(tmp_path / "home")  # say an earlier command did this
    reporter = Asking()
    denied = call(options(folder, reporter), worker.HOST_RUN, command=f"rm -rf {temp}/Documents")
    assert isinstance(denied, PermissionResultDeny) and len(reporter.asked) == 1


def test_a_mixed_command_is_asked_about_for_what_isnt_its_own(folder):
    reporter = Asking()
    call(options(folder, reporter), worker.HOST_RUN, command="rm -rf build && brew install xcodegen")
    assert reporter.asked[0]["why"] == "installs software (brew install)"


def test_processes_its_host_commands_started_are_its_own(folder, monkeypatch):
    seen = []

    def in_sessions(pid, sessions):
        seen.append(sessions)
        return pid == 4242

    monkeypatch.setattr(host, "in_sessions", in_sessions)
    jobs = worker.TaskJobs(folder / "jobs", walls=False)
    jobs.ran = {9: [(500, 1.0)], 10: [(600, 2.0)]}
    own = host_tools._own(worker_task(), folder, folder, jobs)
    assert own.process(4242) and not own.process(4243)
    assert seen[0] == [(500, 1.0)]  # its own sessions, never another task's
    assert own.roots == (folder, worker.task_temp(9))


def test_a_session_is_known_by_its_leader_and_when_it_started(monkeypatch):
    monkeypatch.setattr(host.os, "getsid", lambda pid: {10: 500, 11: 600, 12: 700}.get(pid, 1))
    monkeypatch.setattr(host, "started_at", lambda pid: {500: None, 600: 100.0, 700: 5000.0}.get(pid))
    sessions = [(500, 90.0), (600, 99.5), (700, 99.0)]
    assert host.in_sessions(10, sessions)  # its leader has ended; the rest of its session is still its own
    assert host.in_sessions(11, sessions)  # its leader is still the one that started it
    assert not host.in_sessions(12, sessions)  # that pid is another process's now
    assert not host.in_sessions(13, sessions) and not host.in_sessions(1, sessions)


def test_in_place_tasks_leave_git_to_the_user_on_the_mac_too(folder):
    opts = options(folder, in_place=1, branch="main")
    assert "leave to them" in call(opts, worker.HOST_RUN, command="git commit -am wip").message
    assert isinstance(call(opts, worker.HOST_RUN, command="git status"), PermissionResultAllow)


# ── The tools themselves ────────────────────────────────────────


def tools(folder, jobs, **fields) -> dict:
    return {t.name: t.handler for t in host_tools._host_tools(worker_task(worktree=str(folder), **fields), folder, jobs)}


@pytest.fixture
def unwalled(monkeypatch, tmp_path):
    """host.run and jobs without sandbox-exec, which can't nest inside the test sandbox."""
    real = host.run

    async def run(command, cwd, timeout=host.RUN_TIMEOUT, walls=True, shell=host.SHELL, **extra):
        return await real(command, cwd, timeout, walls=False, shell=("/bin/sh", "-c"), **extra)

    monkeypatch.setattr(host, "run", run)
    jobs = worker.TaskJobs(tmp_path / "jobs", shell=("/bin/sh", "-c"), walls=False)
    jobs.poll = 0.02
    return jobs


def text(result: dict) -> str:
    return result["content"][0]["text"]


def test_host_run_returns_output_and_exit_code(folder, unwalled):
    run = tools(folder, unwalled)["host_run"]
    done = asyncio.run(run({"command": "pwd; echo hi", "folder": "ios"}))
    assert "exit code 0" in text(done) and str((folder / "ios").resolve()) in text(done) and not done.get("is_error")
    failed = asyncio.run(run({"command": "exit 2"}))
    assert failed["is_error"] and "exit code 2" in text(failed)
    assert "Not run" in text(asyncio.run(run({"command": "cat .env"})))  # its own second wall


def test_host_run_gives_its_temp_folder_and_names_what_it_left_running(folder, unwalled, monkeypatch):
    monkeypatch.setattr(host, "lingering", lambda leader: [(4242, "Google Chrome")])
    run = tools(folder, unwalled)["host_run"]
    done = text(asyncio.run(run({"command": "echo $TMPDIR"})))
    assert f"{worker.task_temp(9)}/" in done and worker.task_temp(9).is_dir()
    assert "still running from this command: 4242 (Google Chrome)" in done and "needs no go-ahead" in done
    [(leader, at)] = unwalled.ran[9]  # its session, for telling its processes from others'
    assert leader > 1 and abs(at - time.time()) < 60


def test_what_host_run_leaves_running_the_task_may_stop_without_asking(folder, unwalled):
    run = tools(folder, unwalled)["host_run"]
    done = text(asyncio.run(run({"command": "sleep 30 >/dev/null 2>&1 &"})))
    # Named by whatever it is when listed: under load the forked shell may not have become sleep yet
    [pid] = [int(p) for p in re.findall(r"still running from this command: (\d+) \(", done)]
    try:
        reporter = Asking()
        reporter.host_jobs = unwalled  # the runner's, which the command ran in
        opts = options(folder, reporter)
        assert isinstance(call(opts, worker.HOST_RUN, command=f"kill {pid}"), PermissionResultAllow)
        assert isinstance(call(opts, worker.HOST_RUN, command=f"kill {os.getpid()}"), PermissionResultDeny)
        assert [a["command"] for a in reporter.asked] == [f"kill {os.getpid()}"]
    finally:
        os.kill(pid, signal.SIGKILL)


def test_a_task_starts_and_waits_for_its_jobs(folder, unwalled):
    t = tools(folder, unwalled)

    async def main():
        started = text(await t["start_job"]({"command": "echo compiling; sleep 0.1; echo linked", "title": "the build"}))
        waited = json.loads(text(await t["job_wait"]({"id": 1, "minutes": 0.5})))
        listed = json.loads(text(await t["job_status"]({})))
        return started, waited, listed

    started, waited, listed = asyncio.run(main())
    assert "Started job 1, the build" in started and str(unwalled.log_file(1)) in started
    assert waited["state"] == "done" and "linked" in waited["output_tail"]
    assert [j["id"] for j in listed] == [1]
    other = tools(folder, unwalled, id=10)
    assert asyncio.run(other["job_wait"]({"id": 1}))["is_error"]  # another task's job
    assert text(asyncio.run(other["job_status"]({}))) == "No jobs in the last day."


@pytest.mark.skipif(not platform.MAC, reason="Homebrew's own sandbox is a macOS matter")
def test_an_approved_install_runs_outside_the_host_profile(folder, monkeypatch):
    """Homebrew sandboxes its own steps, and macOS won't start that sandbox inside the host profile."""
    ran, launched = [], []

    async def run(command, cwd, timeout, walls=True, **extra):
        ran.append(walls)
        return host.Ran(0, "", 0)

    monkeypatch.setattr(host, "run", run)
    monkeypatch.setattr(host, "walled", lambda argv, home=None: ["sandbox-exec", *argv])
    monkeypatch.setattr(host.subprocess, "Popen", lambda argv, **kw: launched.append(argv[0]) or SimpleNamespace(
        pid=os.getpid(), poll=lambda: 0))
    t = tools(folder, worker.TaskJobs(folder.parent / "jobs"))

    async def main():
        for command in ("brew install jq", "brew install jq && ./build.sh"):
            await t["host_run"]({"command": command})
            await t["start_job"]({"command": command, "title": "jq"})

    asyncio.run(main())
    assert ran == [False, True] and launched == [host.SHELL[0], "sandbox-exec"]
    reporter = Asking()  # and it only gets there after a yes
    assert isinstance(call(options(folder, reporter), worker.HOST_RUN, command="brew install jq"), PermissionResultDeny)
    assert reporter.asked[0]["why"] == "installs software (brew install)"


def test_on_linux_even_an_approved_install_stays_inside_the_walls(monkeypatch):
    """Nothing needs to leave bubblewrap, so nothing does."""
    monkeypatch.setattr(host_tools, "platform", SimpleNamespace(MAC=False, LINUX=True))
    assert not host_tools.unwalled("brew install jq")
    monkeypatch.setattr(host_tools, "platform", SimpleNamespace(MAC=True, LINUX=False))
    assert host_tools.unwalled("brew install jq") and not host_tools.unwalled("brew install jq && ./build.sh")


def test_the_tools_name_the_computer_and_shell_they_run_on(folder):
    described = {t.name: t.description + json.dumps(t.input_schema)
                 for t in host_tools._host_tools(worker_task(worktree=str(folder)), folder, worker.TaskJobs(folder / "j"))}
    for name in ("host_run", "start_job"):
        assert host_tools.MACHINE in described[name] and f"Bash ({host_tools._SHELL})" in described[name]
    if platform.LINUX:
        assert "Xcode" not in described["host_run"] and "zsh" not in described["host_run"] + described["start_job"]


def test_the_task_log_says_what_ran_on_the_mac():
    block = SimpleNamespace(name=worker.HOST_RUN, input={"command": "xcodebuild -scheme App build"})
    assert worker._describe(block) == "running on the computer: xcodebuild -scheme App build"
