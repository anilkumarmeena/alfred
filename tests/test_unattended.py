"""Tests for scheduled resume and todo jobs (jarvis.tasks.schedules, scheduler, unattended): resuming stopped tasks and
starting tasks for to-do items at a set time, with no conversation open."""

import asyncio
import json
from datetime import datetime, timedelta

import pytest
from test_approvals import pushes  # noqa: F401  (fixture)
from test_goal import checks, verdict  # noqa: F401  (checks is a fixture)
from test_in_place import env  # noqa: F401  (fixture)
from test_nudges import fake_run, finishes, settle
from test_scheduler import FakeRunner, call

from jarvis.core import profile
from jarvis.state import todo
from jarvis.tasks import runner, schedules, unattended
from jarvis.tasks.goal import COMPLETE
from jarvis.tasks.scheduler import Scheduler


@pytest.fixture
def listed(env):
    """Three open to-dos and one done."""
    for item in ("Load MCP connection tools only when needed", "Call the dentist", "Write the weekly report",
                 "Old thing"):
        todo.add(item)
    todo.done("Old thing")
    return env


@pytest.fixture
def planner(monkeypatch):
    """Fake sdk.ask_json for the planner: answers `planner.reply` (None: the call failed), records what it read."""
    fake = type("Planner", (), {"reply": None, "asked": []})()

    async def ask_json(prompt, schema, system_prompt, model, **options):
        fake.asked.append((prompt, system_prompt))
        return fake.reply, 0.0

    monkeypatch.setattr("jarvis.brain.sdk.ask_json", ask_json)
    return fake


def later() -> datetime:
    return datetime.now(profile.tz()) + timedelta(minutes=5)


def run_job(r, job: dict) -> list[tuple[str, str]]:
    """Tick a scheduler for `r` once `job` is due, wait for its work and the tasks it began; return notifications."""
    notified: list[tuple[str, str]] = []

    async def notify(title, text):
        notified.append((title, text))

    async def main():
        s = Scheduler(r, notify, quiet_hours="", clock=later)
        s.pruned = float("inf")
        await s.tick()
        await s.drain()
        if isinstance(r, runner.Runner):
            await settle(r)

    asyncio.run(main())
    return notified


def ended(store, title: str, status: str, kind: str = "research", **fields) -> dict:
    task = store.create(kind, title, f"Do {title}", None, job=fields.pop("job", None))
    store.set(task["id"], status=status, session_id="s1", **fields)
    return store.get(task["id"])


# ── Adding them ─────────────────────────────────────────────────


def test_a_resume_job_takes_named_tasks_or_every_stopped_one(env):
    store = runner.Runner().store
    one, gone_by = ended(store, "mcp tools", "stopped"), ended(store, "render", "stopped", kind="command")
    every = schedules.add("resume", "resume overnight", "", when="03:00", repeat="daily")
    assert every["text"] == "" and schedules.picks(every) == {"failed": False}
    assert schedules.work_text(every) == "resumes every stopped task"
    both = schedules.add("resume", "", "", when="03:00", failed=True)
    assert schedules.work_text(both) == "resumes every stopped or failed task"
    assert both["title"] == "resumes every stopped or failed task"  # no title: what it does
    named = schedules.add("resume", "resume mcp", "Use option B.", when="03:00", tasks=[one["id"], one["id"]])
    assert schedules.picks(named) == {"tasks": [one["id"]]} and schedules.work_text(named) == f"resumes task {one['id']}"
    with pytest.raises(ValueError, match="There's no task 99"):
        schedules.add("resume", "x", "", when="03:00", tasks=[99])
    with pytest.raises(ValueError, match="is a command task, which can't be resumed"):
        schedules.add("resume", "x", "", when="03:00", tasks=[gone_by["id"]])
    with pytest.raises(ValueError, match="takes no task_kind or project"):
        schedules.add("resume", "x", "", when="03:00", task_kind="research")
    with pytest.raises(ValueError, match="todos and count are for todo jobs"):
        schedules.add("resume", "x", "", when="03:00", count=2)


def test_a_todo_job_takes_named_items_or_the_next_few(listed):
    nxt = schedules.add("todo", "to-dos overnight", "", when="03:00")
    assert schedules.picks(nxt) == {"count": 1} and schedules.work_text(nxt) == "starts a task for the next open to-do item"
    two = schedules.add("todo", "x", "", when="03:00", count=2)
    assert schedules.work_text(two) == "starts tasks for the next 2 open to-do items"
    named = schedules.add("todo", "x", "", when="03:00", todos=["weekly report", "MCP connection"])
    assert schedules.picks(named) == {"todos": [3, 1]}
    assert schedules.work_text(named) == ("starts a task for each of these to-do items: Write the weekly report; Load "
                                          "MCP connection tools only when needed")
    with pytest.raises(ValueError, match="No open to-do matches"):
        schedules.add("todo", "x", "", when="03:00", todos=["Old thing"])  # done already
    with pytest.raises(ValueError, match="not both"):
        schedules.add("todo", "x", "", when="03:00", todos=["dentist"], count=1)
    with pytest.raises(ValueError, match="1 to 10"):
        schedules.add("todo", "x", "", when="03:00", count=11)
    with pytest.raises(ValueError, match="tasks and failed are for resume jobs"):
        schedules.add("todo", "x", "", when="03:00", tasks=[1])
    with pytest.raises(ValueError, match="tasks and failed are for resume jobs, todos and count for todo jobs"):
        schedules.add("task", "x", "Brief me.", when="03:00", count=1)
    with pytest.raises(ValueError, match="text is empty"):
        schedules.add("task", "x", "", when="03:00")  # other kinds still need text


def test_schedule_add_says_what_a_resume_or_todo_job_does(listed):
    added, error = call("schedule_add", {"kind": "todo", "title": "to-dos overnight", "when": "03:00", "count": 2,
                                         "text": "Code tasks finish with the review skill."})
    assert not error and added["does"] == "starts tasks for the next 2 open to-do items"
    assert added["text"] == "Code tasks finish with the review skill."
    added, error = call("schedule_add", {"kind": "resume", "title": "resume overnight", "when": "03:00"})
    assert not error and added["does"] == "resumes every stopped task" and "text" not in added


# ── Resuming ────────────────────────────────────────────────────


def test_a_resume_run_carries_on_stopped_tasks_and_says_which(env, monkeypatch):
    prompts = fake_run(monkeypatch, *[finishes()] * 5)
    r = runner.Runner()
    stopped = ended(r.store, "mcp tools", "stopped")
    ended(r.store, "failed one", "failed", error="Hit the usage limit.")
    ended(r.store, "finished one", "done")
    ended(r.store, "scheduled run", "stopped", job=7)  # its job retries it itself
    ended(r.store, "render", "stopped", kind="command")
    job = schedules.add("resume", "resume overnight", "Keep it short.", in_minutes=1)
    notified = run_job(r, job)
    assert notified == [("JARVIS · resume overnight", f"Resumed 1 task: {stopped['id']}, mcp tools.")]
    assert len(prompts) == 1 and "JARVIS's scheduler is carrying this task on" in prompts[0]
    assert prompts[0].endswith(f"{profile.name()}'s note for it: Keep it short.")
    assert r.store.get(stopped["id"])["status"] == "done"
    assert schedules.get(job["id"])["last_result"] == notified[0][1]


def test_failed_true_resumes_failed_tasks_too(env, monkeypatch):
    prompts = fake_run(monkeypatch, *[finishes()] * 5)
    r = runner.Runner()
    stopped, failed = ended(r.store, "mcp tools", "stopped"), ended(r.store, "failed one", "failed")
    notified = run_job(r, schedules.add("resume", "resume", "", in_minutes=1, failed=True))
    assert notified[0][1] == f"Resumed 2 tasks: {stopped['id']}, mcp tools; {failed['id']}, failed one."
    assert len(prompts) == 2


def test_named_tasks_that_are_not_stopped_are_left_alone_and_said(env, monkeypatch):
    fake_run(monkeypatch, *[finishes()] * 5)
    r = runner.Runner()
    stopped, done = ended(r.store, "mcp tools", "stopped"), ended(r.store, "finished one", "done")
    waiting = ended(r.store, "asks", "waiting", question="Which one?")
    job = schedules.add("resume", "resume", "", in_minutes=1, tasks=[stopped["id"], done["id"], waiting["id"]])
    notified = run_job(r, job)
    assert notified[0][1] == (f"Resumed 1 task: {stopped['id']}, mcp tools. Left alone: task {done['id']}, finished "
                              f"one, has finished; task {waiting['id']}, asks, is waiting for your answer.")


def test_a_task_that_cannot_carry_on_is_said_and_the_rest_resume(env, monkeypatch):
    fake_run(monkeypatch, *[finishes()] * 5)
    r = runner.Runner()
    gone = ended(r.store, "old branch", "stopped", worktree=str(env["tmp"] / "removed"))
    ok = ended(r.store, "mcp tools", "stopped")
    notified = run_job(r, schedules.add("resume", "resume", "", in_minutes=1))
    assert notified[0][1] == (f"Resumed 1 task: {ok['id']}, mcp tools. Couldn't resume task {gone['id']}: Task "
                              f"{gone['id']}'s worktree has been removed, so it can't carry on.")


def test_with_nothing_to_resume_a_run_stays_silent(env):
    r = runner.Runner()
    job = schedules.add("resume", "resume", "", in_minutes=1)
    assert run_job(r, job) == []
    assert schedules.get(job["id"])["last_result"] == "No stopped tasks to resume."


def test_resume_and_todo_runs_wait_for_a_used_up_usage_limit(env, monkeypatch):
    reset = later().timestamp() + 3600
    monkeypatch.setattr("jarvis.brain.usage.hold_until", lambda now=None: reset)
    r = FakeRunner()
    for job in (schedules.add("resume", "resume", "", in_minutes=1), schedules.add("todo", "todo", "", in_minutes=1)):
        assert run_job(r, job) == []
        assert schedules.get(job["id"])["next_run"] == reset and schedules.get(job["id"])["status"] == "active"
    assert r.started == []


# ── To-do items ─────────────────────────────────────────────────


def test_a_todo_run_starts_linked_tasks_from_the_plan_and_says_what_it_left(listed, monkeypatch, planner, checks,
                                                                            pushes):
    checks.replies = [verdict(COMPLETE, "it loads them lazily now.", todos={1: ""}),
                      verdict(COMPLETE, "written.", todos={3: ""})]
    planner.reply = {"tasks": [
        {"n": 1, "kind": "research", "title": "lazy MCP tools", "instruction": "Find how to load MCP tools lazily."},
        {"n": 3, "kind": "research", "title": "weekly report", "instruction": "Write the weekly report."},
    ], "left": [{"n": 2, "why": "needs you to call"}]}
    prompts = fake_run(monkeypatch, *[finishes()] * 5)
    r = runner.Runner()
    standing = "Code tasks name the finish skill."
    notified = run_job(r, schedules.add("todo", "to-dos overnight", standing, in_minutes=1, count=2))
    tasks = [r.store.get(i) for i in (1, 2)]
    assert [(t["title"], t["todo"]) for t in tasks] == [("lazy MCP tools", 1), ("weekly report", 3)]
    assert tasks[0]["instruction"] == f"Find how to load MCP tools lazily.\n\n{standing}"  # standing text ends it
    assert prompts[0].endswith(standing)
    assert todo.get(1)["done"] and todo.get(3)["done"] and not todo.get(2)["done"]  # ticked off as each finished
    assert notified == [("JARVIS · to-dos overnight", "Started 2 tasks from your to-do list: 1, lazy MCP tools; 2, "
                                                      "weekly report. Left for you: Call the dentist (needs you to call).")]
    material, system = planner.asked[0]
    sent = json.loads(material)
    assert [i["text"] for i in sent["items"]] == ["Load MCP connection tools only when needed", "Call the dentist",
                                                  "Write the weekly report"]
    assert sent["tasks_to_start"] == 2 and sent["named"] is False and sent["standing_instructions"] == standing
    assert "<long_term_memory>" in sent["memory"] and "projects" in sent and "skills" in sent
    assert "no conversation is open" in system


def test_the_next_items_skip_ones_a_task_has_taken_and_extra_plans_start_nothing(listed, monkeypatch, planner):
    fake_run(monkeypatch, *[finishes()] * 5)
    r = runner.Runner()
    ended(r.store, "mcp tools", "stopped", todo=1)  # stopped work on item 1: resume's to carry on, not a new task
    planner.reply = {"tasks": [
        {"n": 1, "kind": "research", "title": "dentist hours", "instruction": "Find the dentist's hours."},
        {"n": 2, "kind": "research", "title": "weekly report", "instruction": "Write the weekly report."},
    ]}
    notified = run_job(r, schedules.add("todo", "next to-do", "", in_minutes=1))
    assert [i["text"] for i in json.loads(planner.asked[0][0])["items"]] == ["Call the dentist",
                                                                              "Write the weekly report"]
    assert notified[0][1] == "Started 1 task from your to-do list: 2, dentist hours."  # count 1: one task only
    assert r.store.get(2)["todo"] == 2 and r.store.get(3) is None


def test_named_items_since_ticked_off_are_said_and_a_bad_project_is_reported(listed, monkeypatch, planner):
    fake_run(monkeypatch, *[finishes()] * 5)
    r = runner.Runner()
    job = schedules.add("todo", "named to-dos", "", in_minutes=1, todos=["weekly report", "MCP connection"])
    todo.done("weekly report")
    planner.reply = {"tasks": [{"n": 1, "kind": "code_change", "project": "nowhere", "title": "lazy MCP tools",
                                "instruction": "Load MCP tools lazily."}]}
    notified = run_job(r, job)
    assert json.loads(planner.asked[0][0])["named"] is True
    said = notified[0][1]
    assert said.startswith("Started no tasks from your to-do list. Couldn't start Load MCP connection tools only when "
                           "needed (")
    assert said.endswith("Left for you: a to-do since ticked off: Write the weekly report.")
    assert r.store.get(1) is None


def test_a_failed_plan_starts_nothing_and_says_so(listed, planner):
    r = runner.Runner()
    notified = run_job(r, schedules.add("todo", "next to-do", "", in_minutes=1))
    assert notified == [("JARVIS · next to-do", "Couldn't start tasks for your to-do items: planning them failed. "
                                                "They're left as they were.")]
    assert r.store.get(1) is None


def test_with_no_open_items_a_todo_run_stays_silent(env, planner):
    job = schedules.add("todo", "next to-do", "", in_minutes=1)
    assert run_job(runner.Runner(), job) == [] and planner.asked == []
    assert schedules.get(job["id"])["last_result"] == "No open to-do items to start tasks for."


def test_a_to_do_code_task_always_names_the_finish_skill(listed, monkeypatch, planner):
    fake_run(monkeypatch, *[finishes()] * 5)
    r = runner.Runner()
    skill = f"jarvis-skills:{unattended.FINISH_SKILL}"
    planner.reply = {"tasks": [
        {"n": 1, "kind": "code_change", "project": "video-agent", "title": "lazy MCP tools",
         "instruction": "Load MCP tools only when needed."},
        {"n": 3, "kind": "research", "title": "weekly report", "instruction": "Write the weekly report."},
    ]}
    run_job(r, schedules.add("todo", "to-dos overnight", "", in_minutes=1, count=2))  # no note naming the skill
    code, research = r.store.get(1), r.store.get(2)
    assert code["kind"] == "code_change" and code["instruction"].count(skill) == 1
    assert code["instruction"].startswith("Load MCP tools only when needed.") and skill not in research["instruction"]
    named = {"kind": "code_change", "instruction": f"Fix it, then run the {skill} skill."}
    assert unattended.instruction(named, {"text": ""}).count(skill) == 1  # a plan that names it isn't told twice
