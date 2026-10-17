"""Tests for jarvis.tasks.goal: verdict parsing, the finish-check rounds and announcement, scheduled runs."""

import asyncio
import os
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_approvals import drained, events, pushes  # noqa: F401  (pushes is a fixture)
from test_in_place import env, git  # noqa: F401  (env is a fixture)
from test_nudges import fake_run, finishes, settle, until
from test_scheduler import flags, with_scheduler  # noqa: F401  (flags is a fixture)

from jarvis.storage import profile
from jarvis.tasks import goal, runner, schedules
from jarvis.tasks.goal import BLOCKED, COMPLETE, INCOMPLETE, Budget, Check
from jarvis.tasks.worker import Outcome


@pytest.fixture
def checks(monkeypatch):
    """Enable the goal loop with a faked check: each answers the next of `checks.replies` (exception raised,
    event awaited), costing 0.002. `checks.asked` records what each check read.
    """
    fake = SimpleNamespace(replies=[], asked=[])

    async def ask_model(text: str) -> tuple[dict | None, float]:
        fake.asked.append(text)
        reply = fake.replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        if isinstance(reply, asyncio.Event):
            await reply.wait()  # a check that takes its time
            reply = verdict(COMPLETE, "done.")
        return reply, 0.002

    monkeypatch.setattr(goal, "GOAL_LOOP", True)
    monkeypatch.setattr(goal, "GOAL_ROUNDS", 2)
    monkeypatch.setattr(goal, "GOAL_MAX_COST", 0.0)
    monkeypatch.setattr(goal, "ask_model", ask_model)
    fake_run(monkeypatch, *[finishes()] * 10)  # each run says "Did it." unless the test overrides it
    return fake


def verdict(kind: str, said: str, *to_finish: str, todos: dict[int, str] | None = None) -> dict:
    """A check's JSON reply; `todos` maps to-do number to what's left ("" if finished)."""
    found = {"verdict": kind, "said": said} | ({"to_finish": list(to_finish)} if to_finish else {})
    if todos is not None:
        found["todos"] = [{"id": n, "done": not left} | ({"left": left} if left else {}) for n, left in todos.items()]
    return found


def logged(r: runner.Runner, task_id: int, kind: str = goal.KIND) -> list[str]:
    return [e["text"] for e in r.store.history(task_id, 200) if e["kind"] == kind]


def statuses(said: list[dict]) -> list[str]:
    return [e["text"] for e in said if e["type"] == "status"]


def run_task(*replies, kind: str = "research", project: str | None = None, checks, then=None):
    """Start a task with checks answering `replies`, let it settle; return (runner, task, announced).

    `then(r, task)` runs once it settles, then it settles again.
    """
    checks.replies = list(replies)

    async def main():
        r = runner.Runner()
        q = events(r)
        task = r.start(kind, "a thing", "Find X and Y", project)
        await settle(r)
        if then:
            await then(r, task)
            await settle(r)
        await asyncio.sleep(0.02)
        return r, r.store.get(task["id"]), drained(q)

    return asyncio.run(main())


# ── Reading the verdict ─────────────────────────────────────────


@pytest.mark.parametrize("kind", [COMPLETE, INCOMPLETE, BLOCKED])
def test_a_check_reply_gives_its_verdict(kind):
    check = goal.parse(verdict(kind, " it found both. "), 0.01)
    assert (check.verdict, check.said, check.details, check.cost, check.todos) == (kind, "it found both.", "", 0.01, {})


def test_the_list_of_what_to_finish_follows_the_verdict():
    check = goal.parse(verdict(INCOMPLETE, "tests weren't run.", "run make test", "fix what fails"))
    assert check.details == "- run make test\n- fix what fails"


def test_a_check_that_gave_no_usable_reply_is_none():
    assert goal.parse(None) is None


@pytest.mark.parametrize("reply, found", [
    ('{"verdict": "complete", "said": "done."}', verdict(COMPLETE, "done.")),
    ('```json\n{"verdict": "blocked", "said": "needs Xcode.", "to_finish": []}\n```',
     verdict(BLOCKED, "needs Xcode.") | {"to_finish": []}),
    ('Here it is: {"verdict": "incomplete", "said": "Y.", "todos": [{"id": 4, "done": false, "left": "Y"}]}',
     verdict(INCOMPLETE, "Y.", todos={4: "Y"})),
    ("COMPLETE: done.", None),  # keyword form, not JSON: no verdict
    ('{"verdict": "finished", "said": "done."}', None),  # not one of the three
    ('{"verdict": "complete"}', None),  # nothing said
    ('{"verdict": "complete", "said": "done.", "todos": [{"id": "4", "done": true}]}', None),  # an id not a number
])
def test_the_check_reply_must_match_its_schema(reply, found):
    from jarvis.llm.claude import json_reply

    assert json_reply(reply, goal.SCHEMA) == found


# ── Deciding ────────────────────────────────────────────────────


def incomplete(said: str = "Y isn't answered.", details: str = "- answer Y") -> Check:
    return Check(INCOMPLETE, said, details, 0.0)


def test_incomplete_work_is_sent_back_with_what_to_finish(checks):
    said, more = goal.next_step(incomplete(), 0, 1.0)
    assert said == "checked, round 1 of 2: incomplete, so it was sent back to finish: Y isn't answered.\n- answer Y"
    assert "round 1 of 2" in more and "- answer Y" in more and "summary of the whole task" in more
    assert "last time" not in more


def test_the_last_round_asks_for_an_honest_summary(checks):
    said, more = goal.next_step(incomplete(), 1, 1.0)
    assert said.startswith("checked, round 2 of 2")
    assert "last time JARVIS sends it back" in more and "what is done, what isn't" in more


def test_nothing_is_sent_back_past_the_cap_or_a_cost_limit_when_one_is_set(checks, monkeypatch):
    said, more = goal.next_step(incomplete(), 2, 1.0)
    assert more is None and said.startswith("checked: incomplete, but it was sent back 2 times already")
    monkeypatch.setattr(goal, "GOAL_MAX_COST", 20.0)
    said, more = goal.next_step(incomplete(), 0, 20.5)
    assert more is None and "$20.50, over the $20 limit" in said


def test_with_no_cost_limit_any_cost_is_sent_back(checks):
    said, more = goal.next_step(incomplete(), 0, 5000.0)
    assert more is not None and said.startswith("checked, round 1 of 2")


@pytest.mark.parametrize("setting, model", [("", "claude-task-model"), ("claude-sonnet-5", "claude-sonnet-5")])
def test_the_checks_model_is_a_setting_else_the_tasks_own(tmp_path, setting, model):
    # Fresh process: config is read at import. Set but empty means the default, whatever .env says
    env = os.environ | {"JARVIS_GOAL_MODEL": setting, "JARVIS_TASK_MODEL": "claude-task-model",
                        "JARVIS_GOAL_MAX_COST": "", "JARVIS_DB_FILE": str(tmp_path / "t.db")}
    code = "from jarvis import config; print(config.MODELS.role('goal'), config.GOAL_MAX_COST)"
    out = subprocess.run([sys.executable, "-c", code],
                         env=env, cwd=Path(__file__).parents[1], capture_output=True, text=True, check=True).stdout
    assert out.split() == [model, "0.0"]


def test_the_check_asks_the_configured_model(monkeypatch):
    import claude_agent_sdk
    from claude_agent_sdk import ResultMessage

    used = []

    async def query(prompt, options):
        used.append((options.model, options.tools, options.max_turns))
        assert '"verdict"' in options.system_prompt  # the schema is put to it
        yield ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False, num_turns=1,
                            session_id="s", total_cost_usd=0.03, result='{"verdict": "complete", "said": "done."}')

    monkeypatch.setenv("JARVIS_GOAL_MODEL", "claude-opus-5-5")
    monkeypatch.setattr(claude_agent_sdk, "query", query)
    assert asyncio.run(goal.ask_model("material")) == (verdict(COMPLETE, "done."), 0.03)
    assert used == [("claude-opus-5-5", [], 1)]


def test_rounds_of_zero_check_and_report_but_never_send_back(checks, monkeypatch):
    monkeypatch.setattr(goal, "GOAL_ROUNDS", 0)
    said, more = goal.next_step(incomplete(), 0, 1.0)
    assert more is None and "sending back is off" in said


def test_complete_and_blocked_are_never_sent_back(checks):
    assert goal.next_step(Check(COMPLETE, "found both.", "", 0), 0, 0) == ("checked: complete: found both.", None)
    said, more = goal.next_step(Check(BLOCKED, "it needs full Xcode.", "", 0), 0, 0)
    assert more is None and said == f"checked: needs {profile.name()}, so it's reported, not sent back: it needs full Xcode."


def test_rounds_count_from_the_users_latest_word():
    entries = [{"kind": "start", "text": "do it"}, {"kind": goal.KIND, "text": "checked, round 1 of 2: ..."},
               {"kind": goal.KIND, "text": "checked, round 2 of 2: ..."},
               {"kind": goal.KIND, "text": "checked: incomplete, but ..."}]
    assert goal.rounds(entries) == 2
    assert goal.rounds(entries + [{"kind": "reply", "text": "also do Z"}]) == 0


def test_the_daily_budget_caps_checks():
    budget = Budget(2)
    assert [budget.take(0), budget.take(10), budget.take(20)] == [True, True, False]
    assert budget.take(86_401)  # the first has aged out


def test_what_is_said_of_what_is_missing():
    assert goal.left(None) == ("", False) and goal.left(Check(COMPLETE, "all done", "", 0)) == ("", False)
    assert goal.left(Check(BLOCKED, "it needs your Apple ID", "", 0)) == ("it needs your Apple ID", True)
    assert goal.left(incomplete("Y isn't answered.")) == ("Y isn't answered.", False)
    assert goal.left(incomplete("word " * 100))[0].endswith("word…")


# ── In the runner ───────────────────────────────────────────────


def test_a_finished_task_that_passes_is_announced_once(env, pushes, checks):
    r, task, said = run_task(verdict(COMPLETE, "it found X and Y."), checks=checks)
    assert task["status"] == "done" and len(statuses(said)) == 1
    assert statuses(said)[0] == "Your task, a thing, is done. Did it."
    assert logged(r, task["id"]) == ["checked: complete: it found X and Y."]
    assert "<instruction>\nFind X and Y\n</instruction>" in checks.asked[0] and "<summary>\nDid it.\n</summary>" in checks.asked[0]
    assert task["cost"] == pytest.approx(0.01 + 0.002)  # the check's cost is the task's


def test_an_incomplete_task_is_sent_back_and_only_its_final_result_announced(env, pushes, monkeypatch, checks):
    prompts = fake_run(monkeypatch, finishes("Found X."), finishes("Found X and Y."))
    r, task, said = run_task(verdict(INCOMPLETE, "Y isn't answered.", "find Y, with a source"),
                             verdict(COMPLETE, "both found."), checks=checks)
    assert len(prompts) == 2 and prompts[1].startswith(f"JARVIS checked your result against what {profile.name()} asked")
    assert "- find Y, with a source" in prompts[1]
    assert task["status"] == "done" and task["summary"] == "Found X and Y."
    assert statuses(said) == ["Your task, a thing, is done. Found X and Y."]  # one announcement, at the end
    assert [p["title"] for p in pushes] == ["Task 1 is done"]
    assert logged(r, task["id"]) == [("checked, round 1 of 2: incomplete, so it was sent back to finish: Y isn't "
                                      "answered.\n- find Y, with a source"), "checked: complete: both found."]
    assert "It has been sent back 1 time already" in checks.asked[1]
    assert "goal: checked, round 1 of 2" in checks.asked[1]  # the second check sees what the first asked for
    assert task["cost"] == pytest.approx(0.02 + 0.004)


def test_after_the_cap_it_is_reported_honestly(env, pushes, monkeypatch, checks):
    prompts = fake_run(monkeypatch, *[finishes("Found X.")] * 3)
    r, task, said = run_task(*[verdict(INCOMPLETE, "Y isn't answered.")] * 3, checks=checks)
    assert len(prompts) == 3 and "last time JARVIS sends it back" in prompts[2]
    assert statuses(said) == ["Your task, a thing, is done. Found X. It may not be finished: Y isn't answered."]
    rounds = logged(r, task["id"])
    assert [line.split(": incomplete")[0] for line in rounds] == ["checked, round 1 of 2", "checked, round 2 of 2", "checked"]
    assert rounds[2].startswith("checked: incomplete, but it was sent back 2 times already, so it's reported as it is")


def test_work_that_needs_the_user_is_reported_not_looped(env, pushes, monkeypatch, checks):
    prompts = fake_run(monkeypatch, finishes("Built the app; signing needs full Xcode, which isn't installed."))
    _, task, said = run_task(verdict(BLOCKED, "signing the app needs full Xcode, which only Sam can install."),
                             checks=checks)
    assert len(prompts) == 1 and task["status"] == "done"
    assert statuses(said)[0].endswith("It still needs you: signing the app needs full Xcode, which only Sam can install.")


@pytest.mark.parametrize("ending", ["question", "failed", "approval"])
def test_questions_failures_and_waits_for_a_go_ahead_are_not_checked(env, pushes, monkeypatch, checks, ending):
    async def body(task, prompt, reporter) -> Outcome:
        reporter.session(task["id"], "s1")
        if ending == "question":
            reporter.ask(task["id"], "Which account?")
        elif ending == "approval":
            reporter.request_approval(task["id"], {"tool": "host_run", "command": "brew install x", "folder": "/tmp",
                                                   "why": "installs software"})
        return Outcome("Stopped here.", "it hit the Claude usage limit" if ending == "failed" else "", 0.01)

    fake_run(monkeypatch, body)
    r, task, _ = run_task(checks=checks)
    assert checks.asked == [] and logged(r, task["id"]) == []
    assert task["status"] == {"question": "waiting", "failed": "failed", "approval": "waiting"}[ending]
    assert task["wait_kind"] == {"question": "question", "failed": None, "approval": "approval"}[ending]


def test_a_check_that_fails_lets_the_result_through(env, pushes, checks):
    r, task, said = run_task(RuntimeError("the check failed: error_during_execution"), checks=checks)
    assert task["status"] == "done" and statuses(said) == ["Your task, a thing, is done. Did it."]
    assert logged(r, task["id"]) == ["couldn't check the result, so it's reported as it is"]


def test_off_means_no_check(env, pushes, checks, monkeypatch):
    monkeypatch.setattr(goal, "GOAL_LOOP", False)
    r, task, _ = run_task(checks=checks)
    assert checks.asked == [] and task["status"] == "done" and logged(r, task["id"]) == []


def test_no_more_rounds_once_the_task_has_cost_too_much(env, pushes, monkeypatch, checks):
    monkeypatch.setattr(goal, "GOAL_MAX_COST", 0.005)
    prompts = fake_run(monkeypatch, finishes("Found X.", cost=0.01))
    r, task, said = run_task(verdict(INCOMPLETE, "Y isn't answered."), checks=checks)
    assert len(prompts) == 1 and "over the $0.005 limit" in logged(r, task["id"])[0]
    assert statuses(said)[0].endswith("It may not be finished: Y isn't answered.")


def test_stopping_it_while_it_is_checked_stops_it(env, pushes, checks):
    checks.replies = [asyncio.Event()]  # never set: the check takes as long as it's let

    async def main():
        r = runner.Runner()
        q = events(r)
        task = r.start("research", "a thing", "Find X and Y", None)
        await until(lambda: r.store.get(task["id"])["activity"] == "checking its result against what it was asked")
        assert r.store.get(task["id"])["status"] == "running"
        r.stop(task["id"])
        await settle(r)
        return r.store.get(task["id"]), drained(q)

    task, said = asyncio.run(main())
    assert task["status"] == "stopped" and statuses(said) == ["Your task, a thing, stopped."]
    assert task["summary"] == "Did it."  # what it had done is kept


def test_a_message_sent_while_it_is_checked_goes_with_what_to_finish(env, pushes, monkeypatch, checks):
    gate = asyncio.Event()
    prompts = fake_run(monkeypatch, finishes("Found X."), finishes("Found X and Y, in a table."))

    async def ask_model(text):
        checks.asked.append(text)
        if len(checks.asked) == 1:
            await gate.wait()
            return verdict(INCOMPLETE, "Y isn't answered."), 0.002
        return verdict(COMPLETE, "both, in a table."), 0.002

    monkeypatch.setattr(goal, "ask_model", ask_model)

    async def main():
        r = runner.Runner()
        task = r.start("research", "a thing", "Find X and Y", None)
        await until(lambda: len(checks.asked) == 1)
        r.reply(task["id"], "put them in a table")
        gate.set()
        await settle(r)
        return r.store.get(task["id"])

    task = asyncio.run(main())
    assert len(prompts) == 2 and "put them in a table" in prompts[1] and "Y isn't answered" in prompts[1]
    assert task["status"] == "done"


def test_a_reply_after_the_cap_gets_fresh_rounds(env, pushes, monkeypatch, checks):
    prompts = fake_run(monkeypatch, *[finishes("Found X.")] * 5)

    async def follow_up(r, task):
        r.reply(task["id"], "try the other site for Y")

    run_task(*[verdict(INCOMPLETE, "Y isn't answered.")] * 3, verdict(INCOMPLETE, "Y still isn't answered."),
             verdict(COMPLETE, "done."), checks=checks, then=follow_up)
    assert len(prompts) == 5 and prompts[3].startswith(f"Follow-up from {profile.name()}")
    assert "round 1 of 2" in prompts[4]  # counted afresh from the reply


def test_a_code_change_check_reads_its_diff_but_never_secrets(env, pushes, monkeypatch, checks):
    (env["video"] / "deploy.key").write_text("OLD-KEY\n")  # a tracked secret
    git(env["video"], "add", "-A")
    git(env["video"], "commit", "-q", "-m", "key")

    async def body(task, prompt, reporter) -> Outcome:
        reporter.session(task["id"], "s1")
        reporter.activity(task["id"], "running make test")
        folder = task["worktree"]
        (env["video"] / "app.py").write_text("print('hello')\n")
        (env["video"] / "secret.pem").write_text("KEY-MATERIAL\n")
        (env["video"] / "deploy.key").write_text("NEW-KEY\n")
        assert folder == str(env["video"])
        return Outcome("Changed the greeting; tests pass.", "", 0.01)

    fake_run(monkeypatch, body)
    run_task(verdict(COMPLETE, "greeting changed, tested."), kind="code_change", project="video-agent", checks=checks)
    asked = checks.asked[0]
    assert "+print('hello')" in asked and "app.py" in asked
    assert "KEY-MATERIAL" not in asked and "secret.pem" not in asked
    assert "NEW-KEY" not in asked and "OLD-KEY" not in asked and "deploy.key" not in asked
    assert "step: running make test" in asked  # the evidence that it tested


# ── Scheduled runs ──────────────────────────────────────────────


def test_a_scheduled_run_is_checked_and_announced_once_under_its_job(env, pushes, monkeypatch, checks):
    prompts = fake_run(monkeypatch, finishes("One build failed."), finishes("One build failed: the iOS one, on a signing error."))
    checks.replies = [verdict(INCOMPLETE, "it doesn't say which build or why."),
                      verdict(COMPLETE, "names the build and the cause.")]
    job = schedules.add("task", "Build check", "Tell me which builds failed and why.", every_minutes=60,
                        now=datetime.now(profile.tz()) - timedelta(minutes=61))

    async def main():
        r = runner.Runner()
        q = events(r)
        await with_scheduler(r).tick()
        await settle(r)
        await asyncio.sleep(0.02)
        return drained(q)

    said = asyncio.run(main())
    assert len(prompts) == 2 and "round 1 of 2" in prompts[1]
    assert statuses(said) == ["Build check: One build failed: the iOS one, on a signing error."]
    assert schedules.get(job["id"])["last_result"] == "One build failed: the iOS one, on a signing error."


def test_watch_checks_and_runs_with_nothing_to_report_are_not_checked(env, pushes, monkeypatch, checks, flags):
    async def body(task, prompt, reporter) -> Outcome:
        return Outcome("No reply yet." if "watch" in prompt else "All green.", "", 0.01)

    flags.answers = {"met": [False], "nothing_new": [True]}
    prompts = fake_run(monkeypatch, body, body)
    schedules.add("watch", "Priya's reply", "Priya replied", now=datetime.now(profile.tz()))
    schedules.add("task", "Build check", "Tell me only if a build failed.", every_minutes=60, only_changes=True,
                  now=datetime.now(profile.tz()) - timedelta(minutes=61))

    async def main():
        r = runner.Runner()
        await with_scheduler(r).tick()
        await settle(r)

    asyncio.run(main())
    assert len(prompts) == 2 and checks.asked == []
    assert sorted(field for field, _ in flags.asked) == ["met", "nothing_new"]  # one reading each, shared with the end
