"""Tests for tasks.command_jobs: job tasks, stop/limits/restart, failure triage and fixes, schedules, voice shell."""

import asyncio
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny
from test_approvals import drained, events
from test_in_place import env as repos  # noqa: F401  (a fixture)
from test_nudges import fake_run, finishes
from test_scheduler import NOW, Clock, at

from jarvis import tools
from jarvis.brain import sdk, usage
from jarvis.core import claude_logins, config
from jarvis.core.config import COMMAND_SUMMARY_MODEL, COMMAND_TRIAGE_MODEL
from jarvis.core.database import COMMAND
from jarvis.safety import host, shell
from jarvis.safety.shell import ShellGate
from jarvis.tasks import client, command_jobs, events as task_events, runner, schedules, stall
from jarvis.tasks.command_jobs import Triage, parse_triage
from jarvis.tasks.scheduler import Scheduler
from jarvis.tasks.worker import Outcome
from jarvis.tools import schedules as schedule_tools, tasks as task_tools

EIGHT = at(days=1, hours=-1, minutes=-15)  # tomorrow at 08:00, when a daily 08:00 job first runs
SUMMARY = "Made two videos and queued them for upload."


def triage(said: str, action: str, needs: str = "") -> str:
    """The failure call's reply, as JSON."""
    return json.dumps({"said": said, "action": action} | ({"needs": needs} if needs else {}))


@pytest.fixture
def env(repos, monkeypatch):  # noqa: F811
    """Runner on test projects; pushes kept; `triage`/`rerun` script the failure and rerun model replies."""
    monkeypatch.setattr(command_jobs, "COMMANDS_DIR", repos["tmp"] / "commands")
    pushed: list[dict] = []

    async def send_push(title, body, url="/", tag="", **extra):
        pushed.append({"title": title, "body": body})
        return 1

    monkeypatch.setattr(task_events, "PUSH_ENABLED", True)
    monkeypatch.setattr(task_events, "send_push", send_push)
    asked: list[tuple[str, str, str]] = []
    env = repos | {"pushed": pushed, "asked": asked, "triage": triage("ffmpeg isn't installed.", "fix"),
                   "rerun": False, "project": repos["video"].resolve()}

    async def ask(prompt, system, model, **options):
        asked.append((prompt, system, model))
        if system.startswith(command_jobs.rerun_system()):
            return json.dumps({"rerun": env["rerun"]}), 0.0
        return (SUMMARY if system == command_jobs.summary_system() else env["triage"]), 0.0

    monkeypatch.setattr(sdk, "ask", ask)
    return env


def make() -> runner.Runner:
    r = runner.Runner()
    r.commands = command_jobs.Runs(shell=("/bin/sh", "-c"))  # no login shell to start
    return r


async def ended(r: runner.Runner, task_id: int, seconds: float = 10) -> dict:
    """Pump commands until task `task_id` has ended and been announced; return the task."""
    for _ in range(int(seconds * 20)):
        r.check_commands()
        await r.drain_commands()
        if (task := r.store.get(task_id))["status"] != "running":
            return task
        await asyncio.sleep(0.05)
    raise AssertionError("the command didn't end")


async def settled(r: runner.Runner, seconds: float = 10) -> None:
    """Wait until no task run, command, or command announcement is in flight."""
    for _ in range(int(seconds * 20)):
        r.check_commands()
        await r.drain_commands()
        if r.jobs:
            await asyncio.gather(*list(r.jobs.values()), return_exceptions=True)
        if not r.jobs and not r.ending and not any(t["status"] in ("queued", "running") for t in r.store.active()):
            return
        await asyncio.sleep(0.05)
    raise AssertionError("still running")


def triaged(env: dict) -> list[str]:
    return [prompt for prompt, system, _ in env["asked"] if system.startswith(command_jobs.triage_system())]


def reread(env: dict) -> list[str]:
    """Prompts that asked whether a fix summary means rerun."""
    return [prompt for prompt, system, _ in env["asked"] if system.startswith(command_jobs.rerun_system())]


def statuses(q: asyncio.Queue) -> list[str]:
    return [e["text"] for e in drained(q) if e["type"] == "status"]


# ── A job is a task ─────────────────────────────────────────────


def test_a_job_is_a_command_task_in_the_list_said_when_it_ends(env):
    async def main():
        r = make()
        q = events(r)
        task = r.start_command("echo 'rendered: 2 videos'", "the launch render", str(env["project"]))
        assert (task["kind"], task["status"], task["project"], task["instruction"]) == \
               (COMMAND, "running", "video-agent", "echo 'rendered: 2 videos'")
        assert [t["id"] for t in r.store.recent()] == [task["id"]]  # in the list, beside every other task
        assert r.view(task)["stalled"] is None  # no model, so no stall pulse
        done = await ended(r, task["id"])
        return r, done, statuses(q)

    r, done, said = asyncio.run(main())
    assert done["status"] == "done" and done["summary"] == SUMMARY
    assert said == [f"Task {done['id']}, the launch render, is done, in 0 seconds. {SUMMARY}"]
    assert env["pushed"] == [{"title": f"Task {done['id']} is done", "body": said[0]}]
    (prompt, _, model), = env["asked"]
    assert model == COMMAND_SUMMARY_MODEL and "rendered: 2 videos" in prompt and "echo" in prompt
    view = r.view(done)["command"]
    assert "rendered: 2 videos" in view["output_tail"] and view["outcome"] == "done" and view["code"] == 0
    assert view["folder"] == str(env["project"]) and view["log_path"].endswith(f"commands/{done['id']}.log")
    assert [e["kind"] for e in r.store.history(done["id"])] == ["start", "done"]


def test_without_summarize_a_success_says_its_last_lines_and_a_report_its_last_line(env):
    async def main():
        r = make()
        plain = r.start_command("echo one; echo two; echo done", "the build", str(env["project"]), summarize=False)
        report = r.start_command("echo working; echo 'Saved as Brief.m4a in Downloads.'; echo", "the audio file Brief",
                                 str(env["tmp"]), summarize=False, auto_fix=False, report=True)
        return await ended(r, plain["id"]), await ended(r, report["id"])

    plain, report = asyncio.run(main())
    assert env["asked"] == []  # no model at all
    assert plain["spoken"] == f"Task {plain['id']}, the build, is done, in 0 seconds."
    assert plain["summary"] == "Finished in 0 seconds. Last lines: one / two / done"
    assert report["spoken"].endswith("is done, in 0 seconds. Saved as Brief.m4a in Downloads.")
    assert report["project"] is None  # nothing to fix in, so no project looked up


def test_a_command_writing_its_own_log_is_summed_up_from_it(env):
    (env["project"] / "logs").mkdir()
    (env["project"] / "logs" / "old.log").write_text("from an earlier run\n")
    os.utime(env["project"] / "logs" / "old.log", (1, 1))

    async def main():
        r = make()
        task = r.start_command("echo 'uploaded 2 of 2' > logs/2026-10-07-upload.log", "upload", str(env["project"]),
                               log_file="logs/*.log")
        return await ended(r, task["id"])

    asyncio.run(main())
    prompt = env["asked"][0][0]
    assert "uploaded 2 of 2" in prompt and "2026-10-07-upload.log" in prompt and "earlier run" not in prompt


@pytest.mark.parametrize("folder, error", [
    ("relative/path", "full path"),
    ("~/.ssh", "secrets"),
    ("/no/such/folder", "isn't a folder"),
])
def test_a_job_that_cant_run_is_refused_saying_why(env, folder, error):
    r = make()
    with pytest.raises(ValueError, match=error):
        r.start_command("ls", "x", folder)
    with pytest.raises(ValueError, match="empty"):
        r.start_command("  ", "x", str(env["tmp"]))
    with pytest.raises(ValueError, match="start_job"):
        r.start(COMMAND, "x", "ls", "video-agent")  # never as a model's task
    assert r.store.recent() == []


def test_a_job_runs_in_a_session_of_its_own_without_jarviss_keys(env, monkeypatch):
    monkeypatch.setattr(host, "dotenv_values", lambda path: {"JARVIS_TEST_SECRET": "x"})
    monkeypatch.setenv("JARVIS_TEST_SECRET", "hunter2")

    async def main():
        r = make()
        task = r.start_command('echo "secret=$JARVIS_TEST_SECRET"; pwd; sleep 0.2', "the env check",
                               str(env["project"]), summarize=False)
        pid = r.commands.get(task["id"])["pid"]
        session = os.getsid(pid)
        return session, pid, await ended(r, task["id"])

    session, pid, done = asyncio.run(main())
    assert session == pid != os.getsid(0)
    assert "secret=" in done["summary"] and "hunter2" not in done["summary"] and str(env["project"]) in done["summary"]


# ── Stopping, time limits, restarts ─────────────────────────────

# Escapes its process group (setpgid) and ignores SIGTERM, which its sleep inherits
STUBBORN = (f"sleep 30 & {sys.executable} -c 'import os, time; os.setpgid(0, 0); time.sleep(30)' & "
            "sh -c 'trap \"\" TERM; sleep 30' & wait")


def test_stopping_a_stubborn_job_stops_every_process_it_started(env):
    async def main():
        r = make()
        r.commands.grace = 0.5
        task = r.start_command(STUBBORN, "the launch video render", str(env["project"]))
        pid = r.commands.get(task["id"])["pid"]
        for _ in range(200):  # the leader, its sleep, python, the sh ignoring SIGTERM and its sleep
            if len(members := host.session_members(pid)) >= 5:
                break
            await asyncio.sleep(0.02)
        return pid, members, await r.halt(task["id"])

    pid, members, stopped = asyncio.run(main())
    assert stopped["status"] == "stopped" and len(members) >= 5 and host.session_members(pid) == []
    for p in members:
        with pytest.raises(ProcessLookupError):
            os.kill(p, 0)


def test_stopping_never_signals_a_process_that_isnt_the_jobs(env):
    """Pid reuse: the job's old pid now belongs to an unrelated process."""
    other = subprocess.Popen(["sleep", "30"], start_new_session=True)
    try:
        r = make()
        task = r.store.create(COMMAND, "an old render", "render", None, command={"folder": str(env["tmp"])})
        r.store.set(task["id"], status="running")
        r.commands.folder.mkdir(parents=True)
        (r.commands.folder / f"{task['id']}.json").write_text(json.dumps(
            {"id": task["id"], "title": "an old render", "command": "render", "folder": str(env["tmp"]),
             "pid": other.pid, "started": time.time() - 3600}))
        with pytest.raises(ValueError, match="had already ended"):
            asyncio.run(r.halt(task["id"]))
        assert other.poll() is None
    finally:
        other.kill()
        other.wait()


def test_stopping_a_job_stops_all_of_it_and_isnt_looked_into(env):
    async def main():
        r = make()
        q = events(r)
        task = r.start_command("sleep 30 & sleep 30; wait", "the long render", str(env["project"]))
        with pytest.raises(ValueError, match="halt"):
            r.stop(task["id"])
        stopped = await r.halt(task["id"])
        with pytest.raises(ValueError, match="isn't running"):
            await r.stop_command(task["id"])
        record = r.commands.get(task["id"])
        return stopped, statuses(q), r.commands.how(record), r.commands._left(record)

    stopped, said, how, left = asyncio.run(main())
    # a busy Mac may use the whole grace period, so run time isn't asserted
    assert stopped["status"] == "stopped" and len(said) == 1
    assert said[0].startswith(f"Task {stopped['id']}, the long render, stopped after ") and said[0].endswith("seconds.")
    assert how == ("stopped", None) and left == []  # the sleep it started went too
    assert env["asked"] == [] and env["pushed"] == []  # stopped on purpose: no summary, nothing fixed, no push


def test_a_run_past_its_time_limit_is_stopped_and_looked_into(env):
    env["triage"] = triage("The render hung on frame 12.", "report")

    async def main():
        r = make()
        task = r.start_command("sleep 30", "the render", str(env["project"]), timeout_minutes=1)
        r.check_commands(time.time() + 30)
        assert not r.ending  # within its limit
        r.check_commands(time.time() + 120)
        await r.drain_commands()
        return r.store.get(task["id"]), r.commands.how(r.commands.get(task["id"]))

    failed, how = asyncio.run(main())
    assert failed["status"] == "failed" and how == ("stopped", None)
    assert "ran past its time limit of 1 minute" in failed["spoken"] and "hung on frame 12" in failed["spoken"]
    assert command_jobs.spec(failed)["outcome"] == "timeout"


def test_a_job_keeps_running_while_the_runner_restarts(env):
    async def main():
        r = make()
        task = r.start_command("sleep 0.5; echo finished", "the sync", str(env["project"]), summarize=False)
        again = make()  # the runner restarted: the command didn't
        assert again.store.get(task["id"])["status"] == "running"
        return await ended(again, task["id"])

    done = asyncio.run(main())
    assert done["status"] == "done" and "finished" in done["summary"]


def test_a_command_takes_no_messages_but_can_run_again(env):
    async def main():
        r = make()
        task = r.start_command("echo hi", "greet", str(env["project"]), summarize=False)
        with pytest.raises(ValueError, match="takes no messages. Stop it"):
            r.reply(task["id"], "also say bye")
        await ended(r, task["id"])
        with pytest.raises(ValueError, match="To run it again"):
            r.reply(task["id"], "again")
        again = r.rerun(task["id"])
        with pytest.raises(ValueError, match="isn't a command"):
            r.rerun(r.store.create("research", "r", "look", None)["id"])
        return task, await ended(r, again["id"])

    task, again = asyncio.run(main())
    assert again["id"] != task["id"] and again["instruction"] == "echo hi" and again["status"] == "done"


# ── Failures: triage and fixes ──────────────────────────────────


def test_an_ordinary_failure_is_fixed_then_rerun_and_the_rerun_says_it_all(env, monkeypatch):
    command = "test -f fixed || { echo 'ffmpeg: not found' >&2; exit 3; }"

    async def fix(task, prompt, reporter) -> Outcome:
        (env["project"] / "fixed").write_text("")
        return Outcome("Pointed the script at Homebrew's ffmpeg.\nChanged scripts/run.sh.\nIt should work now.", "", 0.1)

    env["rerun"] = True
    briefs = fake_run(monkeypatch, fix)

    async def main():
        r = make()
        q = events(r)
        task = r.start_command(command, "the produce run", str(env["project"]))
        failed = await ended(r, task["id"])
        await settled(r)
        return r, failed, statuses(q)

    r, failed, said = asyncio.run(main())
    first, fixer, rerun = (r.store.get(i) for i in (failed["id"], failed["id"] + 1, failed["id"] + 2))
    assert (fixer["kind"], fixer["project"], fixer["fixes"], fixer["title"]) == \
           ("code_change", "video-agent", first["id"], "Fix the produce run")
    for part in ("exit code 3", "ffmpeg: not found", command, str(env["project"]), f"Command task {first['id']}",
                 "Never run the command yourself", "secrets are hidden from you", "JARVIS runs the command once more",
                 "with its keys", "whether the cause is now dealt with"):
        assert part in briefs[0]
    assert first["status"] == "failed" and command_jobs.spec(first)["fix_task"] == fixer["id"]
    assert fixer["summary"] == "Pointed the script at Homebrew's ffmpeg.\nChanged scripts/run.sh.\nIt should work now."
    assert reread(env) == [f"<summary>\n{fixer['summary']}\n</summary>"]  # read once, for whether to run again
    how = command_jobs.spec(rerun)
    assert (rerun["kind"], rerun["status"], rerun["instruction"], how["attempt"]) == (COMMAND, "done", command, 2)
    assert how["after"] == {"task": first["id"], "fix": fixer["id"], "failed": "ffmpeg isn't installed.",
                            "fixed": "Pointed the script at Homebrew's ffmpeg."}
    # failure said live, fix quietly, rerun with everything; only the rerun is pushed
    assert said[0] == (f"Task {first['id']}, the produce run, failed (exit code 3), after 0 seconds. ffmpeg isn't "
                       f"installed. Task {fixer['id']} is fixing it, and runs it again after.")
    assert said[1] == (f"Task {rerun['id']}, the produce run, ran again after a fix, and worked, in 0 seconds. It had "
                       f"failed before (task {first['id']}): ffmpeg isn't installed. Task {fixer['id']} fixed it: Pointed "
                       f"the script at Homebrew's ffmpeg. This time: {SUMMARY}")
    assert len(said) == 2 and fixer["seen"] == 1
    assert [p["body"] for p in env["pushed"]] == [said[1]]
    (prompt,) = triaged(env)
    assert "exit code 3" in prompt and "ffmpeg: not found" in prompt and "the action is report" not in prompt
    assert COMMAND_TRIAGE_MODEL in [m for _, _, m in env["asked"]]


def test_a_rerun_that_fails_again_is_only_said(env, monkeypatch):
    fake_run(monkeypatch, finishes("Fixed the path. It should work now."))
    env["rerun"] = True

    async def main():
        r = make()
        task = r.start_command("echo 'still broken' >&2; exit 2", "the build", str(env["project"]))
        await ended(r, task["id"])
        await settled(r)
        return r, task

    r, task = asyncio.run(main())
    rerun = r.store.get(task["id"] + 2)
    assert rerun["status"] == "failed" and r.store.get(task["id"] + 3) is None  # no second fix: it can't loop
    assert rerun["spoken"].startswith(f"Task {rerun['id']}, the build, ran again after a fix, and failed again "
                                      f"(exit code 2), after 0 seconds. It had failed before (task {task['id']})")
    assert "the action is report" in triaged(env)[1]
    assert env["pushed"][-1]["body"] == rerun["spoken"]


def test_a_fix_that_says_not_to_rerun_is_said_and_nothing_reruns(env, monkeypatch):
    fake_run(monkeypatch, finishes("The API key has expired; renew it in the dashboard."))

    async def main():
        r = make()
        task = r.start_command("exit 1", "the upload", str(env["project"]))
        await ended(r, task["id"])
        await settled(r)
        return r, task

    r, task = asyncio.run(main())
    fixer = r.store.get(task["id"] + 1)
    assert fixer["spoken"] == (f"Task {fixer['id']}, Fix the upload, is done. The API key has expired; renew it in the "
                               "dashboard. It doesn't run the command again: it would fail the same way.")
    assert r.store.get(task["id"] + 2) is None and env["pushed"][-1]["body"] == fixer["spoken"]


def test_a_risky_fix_waits_for_the_users_yes(env, monkeypatch):
    env["triage"] = triage("The deploy was rejected: the branch is behind production.", "ask",
                           "force-pushing to the production branch")
    briefs = fake_run(monkeypatch, finishes("Rebased and pushed. No need to run it again."))

    async def main():
        r = make()
        first = r.start_command("./deploy.sh", "the deploy", str(env["project"]))
        waiting = await ended(r, first["id"])
        assert r.store.get(first["id"] + 1) is None  # nothing starts before the yes
        answered = r.reply(first["id"], "Yes, but don't touch the staging config.")
        await settled(r)
        second = r.start_command("./deploy.sh", "the deploy", str(env["project"]))
        await ended(r, second["id"])
        declined = r.stop(second["id"])
        return r, waiting, answered, declined

    r, waiting, answered, declined = asyncio.run(main())
    assert waiting["status"] == "waiting"
    assert waiting["question"] == ("The deploy was rejected: the branch is behind production. Fixing it would mean "
                                   "force-pushing to the production branch. Shall I start a task to fix it?")
    assert env["pushed"][0]["title"] == f"Task {waiting['id']} has a question" and waiting["wait_kind"] == "question"
    assert answered["status"] == "failed" and answered["question"] is None and answered["wait_kind"] is None
    assert command_jobs.spec(answered)["fix_task"] == waiting["id"] + 1
    assert "Yes, but don't touch the staging config." in briefs[0]
    assert r.store.get(waiting["id"] + 1)["fixes"] == waiting["id"]
    assert declined["status"] == "failed" and "no fix started" in declined["spoken"]
    assert r.store.get(declined["id"] + 1) is None
    brief = task_tools._brief(r.view(r.store.get(waiting["id"])))
    assert brief["command"] == "./deploy.sh" and brief["fix_task"] == waiting["id"] + 1


def test_a_failure_with_nothing_to_fix_is_only_said(env):
    env["triage"] = triage("The site was down when it checked.", "report")

    async def main():
        r = make()
        task = r.start_command("curl -fsS https://example.invalid", "the uptime check", str(env["project"]))
        return r, await ended(r, task["id"])

    r, failed = asyncio.run(main())
    assert failed["status"] == "failed" and failed["error"].startswith("exit code")
    assert failed["spoken"].endswith("The site was down when it checked.")
    assert r.store.get(failed["id"] + 1) is None and env["pushed"][0]["body"] == failed["spoken"]


def test_a_job_outside_any_project_or_with_fixing_off_is_never_fixed(env):
    (env["tmp"] / "elsewhere").mkdir()

    async def main():
        r = make()
        shell.scratch_dir().mkdir(parents=True, exist_ok=True)
        loose = r.start_command("exit 4", "loose", str(env["tmp"] / "elsewhere"))
        off = r.start_command("exit 4", "off", str(env["project"]), auto_fix=False)
        scratch = r.start_command("exit 4", "scratch", str(shell.scratch_dir()))
        return r, [await ended(r, t["id"]) for t in (loose, off, scratch)]

    r, tasks = asyncio.run(main())
    assert [t["status"] for t in tasks] == ["failed"] * 3 and len(r.store.recent()) == 3  # no fix task
    assert tasks[0]["project"] is None and tasks[2]["project"] is None  # JARVIS's own data is no project
    assert all("the action is report" in p for p in triaged(env))  # the model is told; FIX it says is taken as REPORT


def test_with_no_model_to_read_it_a_failure_is_fixed_and_rerun_once(env, monkeypatch):
    async def down(prompt, system, model, **options):
        raise RuntimeError("Claude is overloaded")

    monkeypatch.setattr(sdk, "ask", down)
    fake_run(monkeypatch, finishes("Fixed."))

    async def main():
        r = make()
        task = r.start_command("echo 'missing module' >&2; exit 1", "tests", str(env["project"]))
        failed = await ended(r, task["id"])
        await settled(r)
        return r, failed

    r, failed = asyncio.run(main())
    assert "Last lines: missing module" in failed["spoken"]
    fixer, rerun = r.store.get(failed["id"] + 1), r.store.get(failed["id"] + 2)
    assert fixer["fixes"] == failed["id"]  # the fix finished, so it reruns even with no model to read it
    assert (rerun["kind"], rerun["status"], command_jobs.spec(rerun)["attempt"]) == (COMMAND, "failed", 2)
    assert r.store.get(failed["id"] + 3) is None  # failed again: only said


def test_the_triage_reply_is_read_strictly():
    def read(reply: str, can_fix: bool = True) -> Triage | None:
        return parse_triage(sdk.json_reply(reply, command_jobs.TRIAGE_SCHEMA), can_fix)

    assert read(triage(" It  broke. ", "fix", "ignored")) == Triage("fix", "It broke.")
    assert read(triage("It broke.", "ask", "a push")) == Triage("ask", "It broke.", "a push")
    assert read(triage("It broke.", "fix"), can_fix=False) == Triage("report", "It broke.")
    assert read(triage("It broke.", "delete everything")) is None  # not an action: no usable reply
    assert read(triage("", "fix")) is None and read("SAID: It broke.\nACTION: FIX") is None
    assert read('```json\n{"said": "It broke.", "action": "report"}\n```') == Triage("report", "It broke.")


def test_a_fix_is_rerun_unless_its_summary_is_read_as_saying_the_cause_remains(monkeypatch):
    async def flag(prompt, field, system_prompt, model, **options):
        assert (field, system_prompt, model) == ("rerun", command_jobs.rerun_system(), command_jobs.VERDICT_MODEL)
        return answer

    monkeypatch.setattr(sdk, "ask_flag", flag)
    for answer, again in ((True, True), (False, False), (None, True)):  # None: the call failed
        assert asyncio.run(command_jobs.wants_rerun("Fixed the path.")) is again
    # the verdict knows the fix task couldn't run it for want of hidden keys, which the rerun has
    assert "secrets are hidden from it" in command_jobs.rerun_system()


# ── Scheduled commands ──────────────────────────────────────────


def scheduled(r: runner.Runner, now=EIGHT) -> tuple[Scheduler, dict]:
    said: dict[str, list] = {"notified": [], "events": []}

    async def notify(title, text):
        said["notified"].append((title, text))

    s = Scheduler(r, notify, said["events"].append, quiet_hours="", clock=Clock(now))
    s.pruned = float("inf")  # no tidying
    r.scheduler = s
    return s, said


def daily(command: str = "echo hi", **kwargs) -> dict:
    return schedules.add("command", "Produce", command, when="08:00", repeat="daily", project="video-agent", now=NOW,
                         **kwargs)


def test_a_command_job_runs_in_its_project_at_its_times(env):
    job = daily("scripts/autopilot/run.sh main produce")
    assert schedules.describe(job, NOW) == "every day at 08:00"
    assert job["next_run"] == EIGHT.timestamp()  # today's 08:00 has passed
    assert (job["folder"], job["project"], job["auto_fix"], job["summarize"], job["timeout_minutes"]) == \
           (str(env["project"]), "video-agent", 1, 1, None)
    (env["project"] / "scripts").mkdir()
    inside = schedules.add("command", "Upload", "./upload.sh", when="20:00", repeat="daily", folder="scripts",
                           project="video-agent", timeout_minutes=90, summarize=False, now=NOW)
    assert (inside["folder"], inside["timeout_minutes"], inside["summarize"]) == (str(env["project"] / "scripts"), 90, 0)
    # A full path finds its project by itself; one in no project has nothing to fix in
    assert schedules.add("command", "x", "ls", every_minutes=30, folder=str(env["project"] / "scripts"),
                         now=NOW)["project"] == "video-agent"
    (env["tmp"] / "elsewhere").mkdir()
    loose = schedules.add("command", "x", "ls", every_minutes=30, folder=str(env["tmp"] / "elsewhere"), now=NOW)
    assert (loose["project"], loose["auto_fix"]) == (None, 0)


@pytest.mark.parametrize("kwargs, error", [
    ({"folder": ""}, "needs folder"),
    ({"folder": "~/.ssh"}, "secrets"),
    ({"folder": "/no/such/folder"}, "isn't a folder"),
    ({"folder": "relative/path"}, "full path"),
    ({"folder": "{elsewhere}", "auto_fix": True}, "auto_fix needs the project"),
    ({"project": "video-agent", "every_minutes": 5}, "Every 10 minutes"),
    ({"project": "video-agent", "log_file": "/var/log/system.log"}, "path in the command's folder"),
    ({"project": "video-agent", "log_file": "../secrets/x.log"}, "path in the command's folder"),
    ({"project": "video-agent", "log_file": "config/.env"}, "secret"),
])
def test_bad_command_jobs_are_refused_saying_why(env, kwargs, error):
    (env["tmp"] / "elsewhere").mkdir()
    kwargs = {"when": "08:00", "repeat": "daily"} | {k: v.format(elsewhere=env["tmp"] / "elsewhere")
                                                     if isinstance(v, str) else v for k, v in kwargs.items()}
    if "every_minutes" in kwargs:
        kwargs.pop("when"), kwargs.pop("repeat")
    with pytest.raises(ValueError, match=error):
        schedules.add("command", "t", "ls", now=NOW, **kwargs)
    with pytest.raises(ValueError, match="for command jobs"):
        schedules.add("reminder", "t", "x", when="17:00", folder=str(env["project"]), now=NOW)


def test_a_command_jobs_settings_change(env):
    job = daily()
    (env["project"] / "scripts").mkdir()
    changed = schedules.change(job["id"], text="echo bye", folder="scripts", timeout_minutes=60, auto_fix=False,
                               log_file="logs/*.log", now=NOW)
    assert (changed["text"], changed["folder"], changed["timeout_minutes"], changed["auto_fix"], changed["log_file"]) \
        == ("echo bye", str(env["project"] / "scripts"), 60, 0, "logs/*.log")
    cleared = schedules.change(job["id"], timeout_minutes=0, log_file="", now=NOW)
    assert (cleared["timeout_minutes"], cleared["log_file"]) == (None, None)
    with pytest.raises(ValueError, match="for command jobs"):
        schedules.change(schedules.add("reminder", "r", "x", when="17:00", now=NOW)["id"], timeout_minutes=5)


def test_each_scheduled_run_is_a_command_task_said_under_the_jobs_title(env):
    job = daily("echo 'rendered: 2 videos'", timeout_minutes=90)

    async def main():
        r = make()
        s, said = scheduled(r)
        assert await s.tick() == [job["id"]]
        run = schedules.run_of(schedules.get(job["id"]))
        task = r.store.get(run["task"])
        assert (task["kind"], task["job"], task["title"], task["project"]) == (COMMAND, job["id"], "Produce", "video-agent")
        assert command_jobs.spec(task)["timeout_minutes"] == 90
        assert schedule_tools.schedule_list()[0]["running"]["task"] == task["id"]
        return r, await ended(r, task["id"]), said

    r, done, said = asyncio.run(main())
    assert done["status"] == "done" and env["pushed"] == [{"title": "Produce", "body": done["spoken"]}]
    assert said["events"] == []  # said as a task's update, not the scheduler's
    job = schedules.get(job["id"])
    assert job["last_task"] == done["id"] and job["last_result"].startswith(f"Succeeded in 0 seconds: {SUMMARY}")
    run = schedules.run_of(job)
    assert (run["task"], run["outcome"], run["code"], run["summary"]) == (done["id"], "done", 0, SUMMARY)
    listed = schedule_tools.schedule_list()[0]
    assert listed["latest_run"]["task"] == done["id"] and listed["latest_run"]["summary"] == SUMMARY


def test_a_monitor_script_stays_silent_with_no_model_until_it_prints_news(env):
    news = env["project"] / "news.txt"
    job = schedules.add("command", "Price watch", f"cat {news} 2>/dev/null; true", every_minutes=10,
                        project="video-agent", only_changes=True, now=NOW)
    assert job["only_changes"] == 1

    async def run_once(r, s, minutes):
        s.clock.now = at(minutes=minutes)
        assert await s.tick() == [job["id"]]
        return await ended(r, schedules.get(job["id"])["last_task"])

    async def main():
        r = make()
        s, _ = scheduled(r, NOW)
        quiet = await run_once(r, s, 10)
        news.write_text("The price fell to 40.\n")
        told = await run_once(r, s, 20)
        return r, quiet, told

    r, quiet, told = asyncio.run(main())
    assert (quiet["status"], quiet["hidden"], quiet["seen"]) == ("done", 1, 1)
    assert quiet["id"] not in [t["id"] for t in r.store.recent()]  # kept out of the task list
    assert len(env["asked"]) == 1 and "The price fell to 40." in env["asked"][0][0]  # the model read only the news
    assert told["hidden"] == 0 and env["pushed"] == [{"title": "Price watch", "body": told["spoken"]}]
    assert SUMMARY in told["spoken"]


def test_no_scheduled_run_starts_while_the_last_is_still_going(env):
    job = schedules.add("command", "Sync", "sleep 30", every_minutes=10, folder=str(env["project"]), now=NOW)

    async def main():
        r = make()
        s, _ = scheduled(r, NOW)
        s.clock.now = at(minutes=10)
        assert await s.tick() == [job["id"]]
        first = schedules.get(job["id"])["last_task"]
        s.clock.now = at(minutes=20)
        assert await s.tick() == []
        skipped = schedules.get(job["id"])
        await r.halt(first)
        return skipped, first

    skipped, first = asyncio.run(main())
    assert skipped["last_result"] == "Skipped a run: the previous one was still going."
    assert skipped["next_run"] == at(minutes=30).timestamp() and skipped["last_task"] == first


def test_a_scheduled_run_that_fails_is_fixed_and_rerun_as_the_jobs_latest(env, monkeypatch):
    fake_run(monkeypatch, finishes("Fixed the path. It should work now."))
    env["rerun"] = True
    job = daily("exit 3", timeout_minutes=30, log_file="logs/*.log", summarize=False)

    async def main():
        r = make()
        s, _ = scheduled(r)
        await s.tick()
        first = schedules.get(job["id"])["last_task"]
        await ended(r, first)
        fixing = schedules.get(job["id"])
        assert fixing["last_task"] == first + 1 and r.store.get(first + 1)["job"] == job["id"]
        assert s._busy(fixing)  # the next run waits for the fix
        await settled(r)
        return r, first

    r, first = asyncio.run(main())
    job = schedules.get(job["id"])
    rerun = r.store.get(first + 2)
    assert (rerun["job"], job["last_task"], command_jobs.spec(rerun)["attempt"]) == (job["id"], rerun["id"], 2)
    assert schedules.run_of(job)["outcome"] == "failed" and job["last_result"].startswith("Failed after 0 seconds")
    assert [t["id"] for t in r.store.runs(job["id"])] == [rerun["id"], first + 1, first]
    # JARVIS reruns it itself, exactly as the schedule runs it: a command task with the job's own settings
    ran, again = command_jobs.spec(r.store.get(first)), command_jobs.spec(rerun)
    same = ("folder", "timeout_minutes", "log_file", "summarize", "auto_fix", "report")
    assert [again[k] for k in same] == [ran[k] for k in same] == [str(env["project"]), 30, "logs/*.log", False, True,
                                                                   False]
    assert (rerun["kind"], rerun["instruction"], rerun["project"]) == (COMMAND, "exit 3", "video-agent")


def test_a_scheduled_run_waiting_for_a_yes_to_fix_holds_nothing_back(env):
    env["triage"] = triage("It needs a new API key.", "ask", "changing the stored key")
    job = daily("exit 1")

    async def main():
        r = make()
        s, _ = scheduled(r)
        await s.tick()
        waiting = await ended(r, schedules.get(job["id"])["last_task"])
        return s, waiting

    s, waiting = asyncio.run(main())
    assert waiting["status"] == "waiting" and not s._busy(schedules.get(job["id"]))
    assert schedules.get(job["id"])["last_result"].endswith("Waiting for your yes to fix it.")


def test_a_folder_gone_is_said_once_and_the_job_waits_for_its_next_time(env):
    (env["tmp"] / "gone").mkdir()
    job = schedules.add("command", "x", "ls", when="08:00", repeat="daily", folder=str(env["tmp"] / "gone"), now=NOW)
    (env["tmp"] / "gone").rmdir()
    r = make()
    s, said = scheduled(r)
    assert asyncio.run(s.tick()) == []
    assert "isn't a folder" in schedules.get(job["id"])["last_result"] and len(said["events"]) == 1
    assert r.store.recent() == []


# ── Claude logins (core.claude_logins) ──────────────────────────


@pytest.fixture
def logins(env, tmp_path, monkeypatch):
    """Two Claude logins, this computer's own first, with their checks and usage kept in tmp_path."""
    monkeypatch.setattr(config, "CLAUDE_LOGINS_DIR", tmp_path / "claude-logins")
    monkeypatch.setattr(config, "CLAUDE_OWN_CONFIG_DIR", "")
    monkeypatch.setattr(usage, "USAGE_FILE", tmp_path / "usage.json")
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    return claude_logins.make("work")


def test_a_command_runs_on_the_claude_login_jarvis_runs_on(env, logins):
    async def main():
        r = make()
        own = r.start_command('echo "on=$CLAUDE_CONFIG_DIR."', "own", str(env["project"]), summarize=False)
        claude_logins.set_order(["work", ""])
        work = r.start_command('echo "on=$CLAUDE_CONFIG_DIR."', "work", str(env["project"]), summarize=False)
        return r, await ended(r, own["id"]), await ended(r, work["id"])

    r, own, work = asyncio.run(main())
    assert own["summary"].endswith("on=.") and command_jobs.spec(own)["login"] == ""  # this computer's own sign-in
    assert work["summary"].endswith(f"on={logins}.") and command_jobs.spec(work)["login"] == "work"
    assert r.view(work)["command"]["login"] == "work"


def test_a_run_whose_claude_login_is_signed_out_runs_again_on_the_next_one(env, logins):
    signed_out = 'if [ -z "$CLAUDE_CONFIG_DIR" ]; then echo "Not logged in · Please run /login"; exit 4; fi; echo uploaded'

    async def main():
        r = make()
        task = r.start_command(signed_out, "the upload", str(env["project"]), summarize=False)
        failed = await ended(r, task["id"])
        await settled(r)
        return r, failed

    r, failed = asyncio.run(main())
    rerun = r.store.get(failed["id"] + 1)
    assert failed["status"] == "failed" and failed["spoken"] == (
        f"Task {failed['id']}, the upload, failed (exit code 4), after 0 seconds: the default Claude login isn't "
        f"signed in. Running it again on the work one, as task {rerun['id']}.")
    assert claude_logins.checks()[""]["ok"] is False and claude_logins.active() == "work"  # passed over from now on
    assert rerun["status"] == "done" and rerun["summary"].endswith("uploaded")
    assert (command_jobs.spec(rerun)["login"], command_jobs.spec(rerun)["tried"]) == ("work", [""])
    assert triaged(env) == [] and [p["body"] for p in env["pushed"]] == [rerun["spoken"]]  # said once, by the rerun


def test_a_scheduled_run_at_its_logins_limit_moves_on_until_every_login_is_used_up(env, logins):
    job = daily("echo \"You've hit your session limit · resets 8am (Asia/Calcutta)\"; exit 4", summarize=False)

    async def main():
        r = make()
        s, _ = scheduled(r)
        await s.tick()
        first = schedules.get(job["id"])["last_task"]
        await ended(r, first)
        await settled(r)
        return r, first

    r, first = asyncio.run(main())
    rerun = r.store.get(first + 1)
    assert "the default Claude login's five-hour limit is used up. Running it again on the work one" in \
           r.store.get(first)["spoken"]
    assert schedules.get(job["id"])["last_task"] == rerun["id"] and command_jobs.spec(rerun)["login"] == "work"
    # The work login is used up too: with no login left to try, it's only said, as nothing a fix could change
    assert rerun["status"] == "failed" and rerun["spoken"].endswith(
        "The work Claude login's five-hour limit is used up, and no other Claude login is free to run it on.")
    assert r.store.get(first + 2) is None and triaged(env) == []
    for login in ("", "work"):
        window = usage.of(login)["five_hour"]
        assert window["status"] == "rejected" and time.time() < window["resets_at"] <= time.time() + 86400
    assert claude_logins.skipped("work").startswith("its five-hour limit is used up until")


def test_only_claudes_own_words_pass_a_login_over(logins):
    for other in ("YouTube: quota exceeded, rate limit reached", "gh: not logged in to any hosts", "exit code 1"):
        assert command_jobs.pass_over("work", other) == ""
    assert usage.of("work") == {} and claude_logins.checks() == {}
    now = datetime(2026, 10, 15, 9, 30, tzinfo=ZoneInfo("Asia/Calcutta")).timestamp()
    said = command_jobs.pass_over("work", "■ You've hit your weekly limit · resets 8am (Asia/Calcutta)\n", now)
    assert said == "the work Claude login's weekly limit is used up"
    assert usage.of("work")["seven_day"]["resets_at"] == now + 22.5 * 3600  # 8 AM tomorrow, in the zone it names
    assert command_jobs.pass_over("", "Failed to authenticate: OAuth token revoked. (authentication_failed)") == \
           "the default Claude login isn't signed in"


# ── Voice shell: start_job, job_status, stop_job ────────────────


@pytest.fixture
def voice(env, monkeypatch):
    """Voice shell job tools, wired to an in-process runner as the socket client would be."""
    r = make()

    async def start_command(command, title, folder, timeout_minutes=None, summarize=True, auto_fix=True, report=False,
                            placeable=False, machine="", approved=""):
        task = r.start_command(command, title, folder, timeout_minutes=timeout_minutes, summarize=summarize,
                               auto_fix=auto_fix, report=report, placeable=placeable, machine=machine,
                               approved=approved)
        return await r.where(task["id"])

    async def tasks(unseen=False):
        return [r.view(t) for t in r.store.recent()]

    async def task(task_id):
        return r.view(r._get(task_id))

    async def stop(task_id):
        return r.view(await r.halt(task_id))

    for name, fn in (("start_command", start_command), ("tasks", tasks), ("task", task), ("stop", stop)):
        monkeypatch.setattr(client, name, fn)
    return r


def test_start_job_starts_a_command_task_and_stop_job_stops_it(env, voice):
    jobs = shell.Jobs()

    async def main():
        started = await jobs.start("sleep 30", "the iOS build", str(env["project"]), timeout_minutes=45)
        other = await jobs.start("sleep 30", "the android build", str(env["project"]), fix=False)
        listed = await jobs.status()
        outcomes = [await jobs.stop_job(title=" "), await jobs.stop_job(title="build"),
                    await jobs.stop_job(title="deploy"), await jobs.stop_job(99),
                    await jobs.stop_job(title="the  ios BUILD"), await jobs.stop_job(1),
                    await jobs.stop_job(title="build")]
        return started, other, listed, outcomes

    started, other, listed, outcomes = asyncio.run(main())
    assert started.startswith("Started job 1, the iOS build: task 1 in the task list.") and "fails" in started
    assert "fails" not in other
    assert [(j["id"], j["title"], j["state"], j["command"]) for j in listed] == \
           [(1, "the iOS build", "running", "sleep 30"), (2, "the android build", "running", "sleep 30")]
    assert command_jobs.spec(voice.store.get(1))["timeout_minutes"] == 45
    assert command_jobs.spec(voice.store.get(2))["auto_fix"] is False
    assert outcomes == [
        "Not stopped: say which job, by its id or its title.",
        "Not stopped: more than one running job matches (job 2, the android build, job 1, the iOS build). Ask which, "
        "then stop it by its id.",
        "No running job is called deploy. Running: job 2, the android build, job 1, the iOS build.",
        "Not stopped: there's no job 99.",
        "Stopped job 1, the iOS build.",
        "Not stopped: job 1, the iOS build, isn't running (stopped).",
        "Stopped job 2, the android build.",
    ]


def test_a_job_with_no_folder_runs_in_the_scratch_folder_and_bad_ones_say_why(env, voice):
    jobs = shell.Jobs()
    shell.scratch_dir().mkdir(parents=True, exist_ok=True)

    async def main():
        return (await jobs.start("echo hi", "hello"), await jobs.start("ls", "x", "/no/such/folder"),
                await jobs.stop_job(voice.store.create("research", "r", "look", None)["id"]))

    started, bad, not_a_job = asyncio.run(main())
    assert started.startswith("Started job 1") and command_jobs.spec(voice.store.get(1))["folder"] == str(shell.scratch_dir())
    assert bad == "Not started: /no/such/folder isn't a folder."
    assert not_a_job.endswith("isn't a job; a task stops with task_stop.")


def test_stop_job_and_job_status_reach_a_scheduled_run(env, voice):
    job = daily("sleep 30")
    jobs = shell.Jobs()

    async def main():
        s, _ = scheduled(voice)
        await s.tick()
        (status,) = await jobs.status()
        assert (status["scheduled_job"], status["title"], status["state"]) == (job["id"], "Produce", "running")
        stopped = await jobs.stop_job(scheduled=job["id"])
        again = await jobs.stop_job(scheduled=job["id"])
        return stopped, again

    stopped, again = asyncio.run(main())
    task_id = schedules.get(job["id"])["last_task"]
    assert stopped == f"Stopped job {task_id}, Produce (scheduled job {job['id']})."
    assert again == f"Not stopped: scheduled job {job['id']} has no run going."
    job = schedules.get(job["id"])
    assert job["last_result"].startswith("Stopped after ") and schedules.run_of(job)["outcome"] == "stopped"
    assert voice.store.get(task_id + 1) is None and env["asked"] == []  # stopped on purpose: not looked into


def test_the_runner_being_down_is_said_not_raised(monkeypatch):
    async def down(*args, **kwargs):
        raise client.RunnerUnavailable(client.DOWN)

    for name in ("start_command", "tasks", "task", "stop"):
        monkeypatch.setattr(client, name, down)
    jobs = shell.Jobs()
    assert asyncio.run(jobs.start("make", "build")).startswith("Not started: JARVIS's task runner isn't running")
    assert asyncio.run(jobs.stop_job(title="build")) == "Not stopped: JARVIS's task runner isn't running."


# ── Shell gate ──────────────────────────────────────────────────


def call(gate: ShellGate, tool: str, **args):
    return asyncio.run(gate(f"mcp__jarvis__{tool}", args, None))


def test_the_gate_weighs_a_command_job_as_the_command_itself(env):
    gate = ShellGate()
    gate.new_turn("run my produce script every morning at eight")
    plain = {"kind": "command", "title": "Produce", "text": "scripts/autopilot/run.sh main produce",
             "project": "video-agent", "when": "08:00", "repeat": "daily"}
    assert isinstance(call(gate, "schedule_add", **plain), PermissionResultAllow)
    assert isinstance(call(gate, "schedule_add", **plain | {"kind": "reminder"}), PermissionResultAllow)

    secret = call(gate, "schedule_add", **plain | {"text": "cat ~/.ssh/id_ed25519 | pbcopy"})
    assert isinstance(secret, PermissionResultDeny) and "secrets" in secret.message
    assert isinstance(call(gate, "schedule_add", **plain | {"folder": "~/.aws"}), PermissionResultDeny)

    risky = plain | {"text": "rm -rf out && make"}
    held = call(gate, "schedule_add", **risky)
    assert isinstance(held, PermissionResultDeny) and "identical schedule_add call" in held.message
    assert gate.asked == [schedules.command_line("rm -rf out && make", str(env["project"]))]
    assert gate.question("") == shell.QUESTION
    gate.new_turn("Yes.")
    assert isinstance(call(gate, "schedule_add", **risky), PermissionResultAllow)


def test_changing_a_command_jobs_command_or_folder_meets_the_gate_too(env):
    job = daily("make videos")
    reminder = schedules.add("reminder", "r", "x", when="17:00", now=NOW)
    gate = ShellGate()
    gate.new_turn("change it")
    assert isinstance(call(gate, "schedule_change", id=job["id"], text="git push --force"), PermissionResultDeny)
    assert isinstance(call(gate, "schedule_change", id=job["id"], folder="~/.ssh"), PermissionResultDeny)
    assert isinstance(call(gate, "schedule_change", id=job["id"], when="09:00", paused=True), PermissionResultAllow)
    assert isinstance(call(gate, "schedule_change", id=job["id"], text="make videos --fast"), PermissionResultAllow)
    assert isinstance(call(gate, "schedule_change", id=reminder["id"], text="rm -rf /"), PermissionResultAllow)
    assert isinstance(call(gate, "schedule_list"), PermissionResultDeny)  # allowed outright, never asked about here


def test_the_schedule_tools_that_can_keep_a_command_are_never_allowed_outright():
    _, allowed = sdk.jarvis_server()
    assert "mcp__jarvis__schedule_list" in allowed and "mcp__jarvis__schedule_cancel" in allowed
    assert not {"mcp__jarvis__schedule_add", "mcp__jarvis__schedule_change"} & set(allowed)
    assert set(schedules.COMMAND_TOOLS) == {t.name for t in tools.all_tools() if t.shell}


# ── Bits ────────────────────────────────────────────────────────


def test_a_command_task_is_never_stalled():
    task = {"id": 1, "kind": COMMAND, "status": "running", "updated": 0}
    assert stall.check(task, None, stall.Pulse(), None, now=10 ** 9) is None


def test_durations():
    assert [command_jobs.took(s) for s in (1, 59, 60, 3600, 3 * 3600 + 5 * 60 + 20)] == \
           ["1 second", "59 seconds", "1 minute", "1 hour", "3 hours 5 minutes"]
    assert command_jobs.tail(Path("/no/such/file"), 100) == ""
    assert command_jobs.spec({"command": json.dumps({"attempt": 2})}) == {"attempt": 2}
    assert command_jobs.spec({"command": None}) == {} and command_jobs.spec({"command": "{bad"}) == {}
