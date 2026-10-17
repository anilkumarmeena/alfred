"""Tests for the task runner's clean shutdown on SIGTERM and resuming cut-off tasks at the next start."""

import asyncio
import json
import os
import signal
from types import SimpleNamespace

import pytest
from test_in_place import env, worker_task  # noqa: F401  (fixture)
from test_nudges import fake_run, finishes, settle, until

from conftest import use_task_cache
from jarvis.safety import host
from jarvis.tasks import access, api, host_tools, runner, worker
from jarvis.tasks.store import Store
from jarvis.tasks.worker import Outcome


def logged(r: runner.Runner, task_id: int) -> list[str]:
    return [e["text"] for e in r.store.history(task_id) if e["kind"] == "interrupted"]


def cut_off(task: dict) -> dict:
    return json.loads(task["interrupted"])


def hangs(on_mac: str | None = None):
    """Fake run: starts its session, then works until stopped, running `on_mac` on the computer if given."""
    async def body(task, prompt, reporter) -> Outcome:
        reporter.session(task["id"], f"s{task['id']}")
        if on_mac:
            with reporter.host_jobs.running(task["id"], on_mac):
                await asyncio.Event().wait()
        await asyncio.Event().wait()

    return body


def interrupted(store: Store, status: str = "running", session: str | None = "s1", on_mac: list[str] = (),
                prompt: str | None = "Find flights to Goa.", **fields) -> dict:
    """A research task as a clean shutdown left it."""
    task = store.create("research", "Flights", "Find flights to Goa.", None)
    store.set(task["id"], status=status, session_id=session, **fields,
              interrupted=json.dumps({"at": 1.0, "on_mac": list(on_mac), "prompt": prompt}))
    return store.get(task["id"])


# ── Shutting down ───────────────────────────────────────────────


def test_a_shutdown_notes_tasks_as_interrupted_and_says_nothing(env, monkeypatch):
    fake_run(monkeypatch, hangs(), hangs("xcodebuild -scheme App"))

    async def main():
        r = runner.Runner(capacity=2)
        events: asyncio.Queue = asyncio.Queue()
        r.listeners.add(events)
        plain = r.start("research", "Flights", "Find flights to Goa.")
        on_mac = r.start("research", "Build", "Build the app.")
        queued = r.start("research", "Hotels", "Find a hotel.")
        await until(lambda: r.host_jobs.on_mac.get(on_mac["id"]) and r.store.get(plain["id"])["session_id"])
        await r.shutdown()
        return r, events, [r.store.get(t["id"]) for t in (plain, on_mac, queued)]

    r, events, (plain, on_mac, queued) = asyncio.run(main())
    assert r.jobs == {} and r.prompts == {} and r.host_jobs.on_mac == {}
    assert [t["status"] for t in (plain, on_mac, queued)] == ["running", "running", "queued"]  # to resume
    assert cut_off(plain)["on_mac"] == [] and cut_off(plain)["prompt"] == "Find flights to Goa."
    assert cut_off(on_mac)["on_mac"] == ["xcodebuild -scheme App"]
    assert cut_off(queued)["prompt"] == "Find a hotel."
    assert logged(r, on_mac["id"]) == ["JARVIS is shutting down, while it ran xcodebuild -scheme App on the computer"]
    assert logged(r, plain["id"]) == ["JARVIS is shutting down"]
    assert events.empty() and all(t["spoken"] is None for t in (plain, on_mac, queued))  # nothing announced


def test_a_task_cut_off_before_its_first_step_is_left_to_resume_too(env, monkeypatch):
    prompts = fake_run(monkeypatch)

    async def main():
        r = runner.Runner()
        events: asyncio.Queue = asyncio.Queue()
        r.listeners.add(events)
        task = r.start("research", "Flights", "Find flights to Goa.")
        await r.shutdown()  # before its job takes a step
        return r, events, r.store.get(task["id"])

    r, events, task = asyncio.run(main())
    assert prompts == [] and r.jobs == {} and r.in_line == set() and events.empty()
    assert task["status"] in ("queued", "running") and cut_off(task)["prompt"] == "Find flights to Goa."


def test_a_shutdown_takes_no_new_work_and_stops_the_loops_that_start_it(env, tmp_path):
    async def main():
        r = runner.Runner()
        r.service(asyncio.sleep(60))
        [loop] = r.services
        await r.shutdown()
        with pytest.raises(RuntimeError, match="shutting down"):
            r.start("research", "Flights", "Find flights.")
        with pytest.raises(RuntimeError, match="shutting down"):
            r.start_command("echo hi", "hi", str(tmp_path))
        await asyncio.sleep(0)
        return r, loop

    r, loop = asyncio.run(main())
    assert loop.cancelled()
    assert r.store.active() == []  # nothing half made


def test_the_api_refuses_changes_while_shutting_down(env):
    r = runner.Runner()
    refuse = api.closing(r)

    async def handler(request):
        return "handled"

    def call(method: str):
        return asyncio.run(refuse(SimpleNamespace(method=method), handler))

    assert call("POST") == "handled"
    r.closing = True
    refused = call("POST")
    assert refused.status == 503 and "shutting down" in refused.text
    assert call("GET") == "handled"  # tasks can still be read


def test_host_commands_are_kept_in_on_mac_while_they_run(tmp_path, monkeypatch):
    use_task_cache(monkeypatch, tmp_path / "cache")
    seen = []

    async def run(command, cwd, timeout, walls=True, **extra):
        seen.append({k: list(v) for k, v in jobs.on_mac.items()})
        return host.Ran(0, "", 0)

    monkeypatch.setattr(host, "run", run)
    jobs = worker.TaskJobs(tmp_path / "jobs", walls=False)
    tools = {t.name: t.handler for t in host_tools._host_tools(worker_task(worktree=str(tmp_path)), tmp_path, jobs)}
    asyncio.run(tools["host_run"]({"command": "make build"}))
    assert seen == [{9: ["make build"]}] and jobs.on_mac == {}


# ── Starting again ──────────────────────────────────────────────


def test_a_restart_resumes_tasks_a_shutdown_cut_off_in_their_own_sessions(env, monkeypatch):
    prompts = fake_run(monkeypatch, hangs(), finishes())

    async def main():
        r = runner.Runner()
        task = r.start("research", "Flights", "Find flights to Goa.")
        await until(lambda: r.store.get(task["id"])["session_id"])
        await r.shutdown()
        again = runner.Runner()  # the next start
        again.start_background()
        await settle(again)
        return again, again.store.get(task["id"])

    r, task = asyncio.run(main())
    assert prompts[1] == runner.RESUMED
    assert task["status"] == "done" and task["interrupted"] is None and task["error"] is None
    assert logged(r, task["id"]) == ["JARVIS is shutting down", "resumed after JARVIS restarted"]


def test_a_task_that_never_got_going_is_launched_again_as_it_was(env, monkeypatch):
    store = Store()
    queued = interrupted(store, status="queued", session="s1", prompt="Answer from Alex: option B")
    fresh = interrupted(store, status="queued", session=None)
    prompts = fake_run(monkeypatch, finishes(), finishes())

    async def main():
        r = runner.Runner()
        r.start_background()
        await settle(r)
        return r

    r = asyncio.run(main())
    assert prompts == ["Answer from Alex: option B", "Find flights to Goa."]
    assert all(r.store.get(t["id"])["status"] == "done" for t in (queued, fresh))


def test_every_kind_of_task_resumes_whatever_access_it_has(env, monkeypatch):
    monkeypatch.setattr(runner, "RESUME_TASKS", 3)
    prompts = fake_run(monkeypatch, hangs(), hangs(), hangs(), finishes(), finishes(), finishes())

    async def main():
        r = runner.Runner()
        started = [r.start("code_change", "Fix", "Fix the bug.", "video-agent"),
                   r.start("code_question", "Designs", "Design a home screen.", "other", grants=["artifacts"]),
                   r.start("research", "Flights", "Find flights to Goa.")]
        await until(lambda: all(r.store.get(t["id"])["session_id"] for t in started))
        await r.shutdown()
        again = runner.Runner()
        again.start_background()
        await settle(again)
        return again, [again.store.get(t["id"]) for t in started]

    r, tasks = asyncio.run(main())
    assert prompts[3:] == [runner.RESUMED] * 3
    assert [t["status"] for t in tasks] == ["done"] * 3
    assert "artifacts" in access.has(tasks[1])  # it keeps the access it was granted
    assert all(logged(r, t["id"]) == ["JARVIS is shutting down", runner.RESUMED_LOG] for t in tasks)


def test_a_running_task_resumes_even_without_a_clean_shutdown(env, monkeypatch):
    """A code task with extra access, left running and never marked interrupted."""
    store = Store()
    task = store.create("code_question", "Designs", "Design a home screen.", "other", grants=["artifacts"])
    store.set(task["id"], status="running", session_id="s1")
    queued = store.create("research", "Flights", "Find flights to Goa.", None)  # what it was launched with is lost
    store.set(queued["id"], status="queued")
    prompts = fake_run(monkeypatch, finishes())

    async def main():
        r = runner.Runner()
        r.start_background()
        await settle(r)
        return r

    r = asyncio.run(main())
    assert prompts == [runner.RESUMED]
    assert r.store.get(task["id"])["status"] == "done" and logged(r, task["id"]) == [runner.RESUMED_LOG]
    assert r.store.get(queued["id"])["error"] == runner.RESTARTED
    assert logged(r, queued["id"]) == ["not resumed on its own: the runner stopped without a clean shutdown"]


def test_a_task_that_dies_again_straight_after_resuming_is_not_resumed_again(env, monkeypatch):
    store = Store()
    looping = store.create("research", "Flights", "Find flights to Goa.", None)
    store.set(looping["id"], status="running", session_id="s1")
    store.log(looping["id"], "interrupted", runner.RESUMED_LOG)  # and the runner died before anything else
    worked = store.create("research", "Hotels", "Find a hotel.", None)
    store.set(worked["id"], status="running", session_id="s2")
    store.log(worked["id"], "interrupted", runner.RESUMED_LOG)
    store.log(worked["id"], "progress", "Found three hotels.")  # it got going again before this stop
    prompts = fake_run(monkeypatch, finishes())

    async def main():
        r = runner.Runner()
        r.start_background()
        await settle(r)
        return r

    r = asyncio.run(main())
    assert prompts == [runner.RESUMED] and r.store.get(worked["id"])["status"] == "done"
    assert r.store.get(looping["id"])["status"] == "stopped" and r.store.get(looping["id"])["error"] == runner.RESTARTED
    assert logged(r, looping["id"])[-1] == ("not resumed on its own: it was resumed after the last restart, and the "
                                             "runner stopped again without a clean shutdown")


def test_tasks_cut_off_mid_command_or_waiting_on_the_user_stop_as_before(env, monkeypatch):
    store = Store()
    on_mac = interrupted(store, on_mac=["brew install jq"])
    asked = interrupted(store, question="Which airline?")
    wants = interrupted(store, wants='["mac"]')
    approval = interrupted(store)
    request, _ = store.ask_approval(approval["id"], {"tool": "host_run", "command": "git push", "folder": "/x",
                                                     "why": "pushes"}, expires=9e9, batch_seconds=5)
    store.set_approval(request["id"], announced=1)
    check = interrupted(store, hidden=1)
    prompts = fake_run(monkeypatch)

    async def main():
        r = runner.Runner()
        r.start_background()
        await settle(r)
        return r

    r = asyncio.run(main())
    assert prompts == []
    for t in (on_mac, asked, wants, approval):
        task = r.store.get(t["id"])
        assert task["status"] == "stopped" and task["error"] == runner.RESTARTED and task["interrupted"] is None
    assert r.store.get(check["id"])["status"] == "stopped" and r.store.get(check["id"])["error"] is None
    assert logged(r, on_mac["id"]) == ["not resumed on its own: it was cut off running brew install jq on the computer"]
    assert logged(r, approval["id"]) == ["not resumed on its own: it's waiting for a go-ahead"]
    assert logged(r, asked["id"]) == logged(r, wants["id"]) == ["not resumed on its own: it's waiting for an answer"]


def test_only_so_many_resume_running_ones_first(env, monkeypatch):
    monkeypatch.setattr(runner, "RESUME_TASKS", 2)
    store = Store()
    queued = interrupted(store, status="queued", session=None)
    first, second = interrupted(store), interrupted(store)
    prompts = fake_run(monkeypatch, finishes(), finishes())

    async def main():
        r = runner.Runner()
        r.start_background()
        await settle(r)
        return r

    r = asyncio.run(main())
    assert prompts == [runner.RESUMED, runner.RESUMED]
    assert [r.store.get(t["id"])["status"] for t in (first, second, queued)] == ["done", "done", "stopped"]
    assert logged(r, queued["id"]) == ["not resumed on its own: no more than 2 resume at once (JARVIS_RESUME_TASKS)"]


def test_resuming_can_be_turned_off(env, monkeypatch):
    monkeypatch.setattr(runner, "RESUME_TASKS", 0)
    task = interrupted(Store())
    assert runner.Runner().resuming == []
    assert Store().get(task["id"])["status"] == "stopped"


# ── The service as launchd runs it ──────────────────────────────


class Site:
    """Fake Unix-socket site; a sandbox may not let tests serve one."""

    def __init__(self, site_runner, path: str) -> None: ...

    async def start(self) -> None: ...


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT])
def test_a_stop_signal_shuts_the_service_down_cleanly(env, monkeypatch, tmp_path, sig):
    monkeypatch.setattr(runner, "RUNNER_SOCKET", tmp_path / "runner.sock")
    monkeypatch.setattr(runner.web, "UnixSite", Site)
    for flag in ("NOTICES_ENABLED", "SCHEDULER_ENABLED", "LEARNING_ENABLED"):
        monkeypatch.setattr(runner, flag, False)
    real, made = runner.Runner, []

    def make() -> runner.Runner:
        made.append(real())
        return made[0]

    monkeypatch.setattr(runner, "Runner", make)
    fake_run(monkeypatch, hangs())

    async def main():
        served = asyncio.create_task(runner.serve())
        await until(lambda: made)
        task = made[0].start("research", "Hotels", "Find a hotel.")
        await until(lambda: Store().get(task["id"])["session_id"])
        os.kill(os.getpid(), sig)
        await asyncio.wait_for(served, 5)
        return made[0], Store().get(task["id"])

    r, task = asyncio.run(main())
    assert r.closing and r.jobs == {}
    assert task["status"] == "running" and cut_off(task)["prompt"] == "Find a hotel."
