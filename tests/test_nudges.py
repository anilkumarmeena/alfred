"""Tests for nudges: task_reply messages to a queued or running task, their delivery, and late nudges."""

import asyncio

import pytest
from claude_agent_sdk import AssistantMessage, ResultMessage, ToolUseBlock
from test_in_place import NullReporter, env, worker_task  # noqa: F401  (env is a fixture)

from conftest import use_task_cache
from jarvis.storage import profile
from jarvis.tasks import runner, worker
from jarvis.tasks.store import nudges
from jarvis.tasks.worker import Outcome, nudge_text, with_nudges
from jarvis.tools import tasks as task_tools

PROJECTS = ["video-agent", "other"]  # in place, and on its own worktree


async def settle(r: runner.Runner) -> None:
    """Wait until every task is done, by status: a relaunched run may still be committing outside r.jobs."""
    while r.jobs or any(t["status"] in ("queued", "running") for t in r.store.active()):
        await (asyncio.gather(*list(r.jobs.values())) if r.jobs else asyncio.sleep(0.01))


def fake_run(monkeypatch, *bodies) -> list[str]:
    """Fake worker.run: run n calls bodies[n](task, prompt, reporter). Returns every run's prompt."""
    prompts: list[str] = []

    async def run(task: dict, prompt: str, reporter) -> Outcome:
        prompts.append(prompt)
        return await bodies[len(prompts) - 1](task, prompt, reporter)

    monkeypatch.setattr(runner, "run", run)
    return prompts


def finishes(summary: str = "Did it.", cost: float = 0.01):
    async def body(task, prompt, reporter) -> Outcome:
        reporter.session(task["id"], "s1")
        return Outcome(summary, "", cost)

    return body


async def until(check) -> None:
    for _ in range(200):
        if check():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("timed out")


def said(r: runner.Runner, task_id: int, kind: str) -> list[str]:
    return [e["text"] for e in r.store.history(task_id) if e["kind"] == kind]


# ── Queuing and delivery ────────────────────────────────────────


@pytest.mark.parametrize("project", PROJECTS)
def test_running_task_gets_nudges_in_order_at_its_next_step(env, monkeypatch, project):
    step = asyncio.Event()
    got = []

    async def body(task, prompt, reporter) -> Outcome:
        await step.wait()  # busy with a tool call
        got.append(reporter.take_nudges(task["id"]))  # the hook after it
        got.append(reporter.take_nudges(task["id"]))  # and after the next one
        return Outcome("Did it.", "", 0.01)

    prompts = fake_run(monkeypatch, body)

    async def main():
        r = runner.Runner()
        task = r.start("code_change", "a thing", "Do it", project)
        await until(lambda: prompts)  # its run has begun
        r.reply(task["id"], "also check X")
        queued = r.reply(task["id"], "skip Y")
        step.set()
        await settle(r)
        return r, queued, r.store.get(task["id"])

    r, queued, task = asyncio.run(main())
    assert queued["status"] == "running"  # nudging doesn't stop or restart it
    assert nudges(queued) == ["also check X", "skip Y"]
    assert task_tools._brief(queued)["messages_not_yet_delivered"] == ["also check X", "skip Y"]
    assert got == [["also check X", "skip Y"], []]  # delivered once, oldest first
    assert prompts == ["Do it"]  # no extra run
    assert task["status"] == "done" and nudges(task) == []
    assert "messages_not_yet_delivered" not in task_tools._brief(task)
    assert said(r, task["id"], "nudge") == ["also check X", "skip Y"]
    assert said(r, task["id"], "progress") == ["nudge received: also check X", "nudge received: skip Y"]


class Pending(NullReporter):
    """A reporter holding messages for the task."""

    def __init__(self, *messages: str) -> None:
        self.messages = list(messages)

    def take_nudges(self, task_id: int) -> list[str]:
        taken, self.messages = self.messages, []
        return taken


def test_hook_adds_waiting_nudges_to_the_next_tool_result():
    reporter = Pending("also check X", "skip Y")
    [matcher] = worker._nudge_hook(9, reporter)
    deliver = matcher.hooks[0]
    out = asyncio.run(deliver({"hook_event_name": "PostToolUse", "tool_name": "Bash"}, "t1", None))
    assert out["hookSpecificOutput"]["hookEventName"] == "PostToolUse"
    context = out["hookSpecificOutput"]["additionalContext"]
    assert context.index("also check X") < context.index("skip Y")
    assert "carry on with everything else" in context
    assert asyncio.run(deliver({"hook_event_name": "PostToolUseFailure", "tool_name": "Bash"}, "t2", None)) == {}


def test_hook_holds_nudges_while_the_task_asks_a_question():
    reporter = Pending("skip Y")
    [matcher] = worker._nudge_hook(9, reporter)
    out = asyncio.run(matcher.hooks[0]({"hook_event_name": "PostToolUse", "tool_name": "mcp__task__ask_user"}, "t1", None))
    assert out == {} and reporter.messages == ["skip Y"]


def test_task_sessions_run_the_hook_after_every_tool_call(env, monkeypatch):
    use_task_cache(monkeypatch, env["tmp"] / "cache")
    for task in (worker_task(), worker_task(in_place=1), worker_task(kind="research", project=None)):
        opts = worker._options(task, NullReporter())
        assert set(opts.hooks) == {"PreToolUse", "PostToolUse", "PostToolUseFailure"}  # PreToolUse: the gate's check
        assert all(m.matcher is None for ms in opts.hooks.values() for m in ms)


# ── Finishing before delivery ───────────────────────────────────


class FakeClient:
    """Fake ClaudeSDKClient: query n answers with script[n].

    A number in a script is a silent pause in seconds. The stream ends when a query's script runs out, or
    after the last one with `end=True`.
    """

    def __init__(self, *script: list, end: bool = False) -> None:
        self.script, self.prompts, self.end = script, [], end
        self._asked = asyncio.Event()

    def __call__(self, options):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        pass

    async def query(self, prompt: str) -> None:
        self.prompts.append(prompt)
        self._asked.set()

    async def receive_messages(self):
        answered = 0
        while True:
            while len(self.prompts) <= answered:
                self._asked.clear()
                await self._asked.wait()
            if answered >= len(self.script):
                return
            for msg in self.script[answered]:
                if isinstance(msg, (int, float)):
                    await asyncio.sleep(msg)
                else:
                    yield msg
            answered += 1
            if self.end and answered == len(self.script):
                return


def result(text: str, cost: float, error: bool = False) -> ResultMessage:
    return ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=error, num_turns=1,
                         session_id="s1", total_cost_usd=cost, result=text)


def worker_run(monkeypatch, client: FakeClient, reporter) -> Outcome:
    monkeypatch.setattr(worker, "ClaudeSDKClient", client)
    monkeypatch.setattr(worker, "_options", lambda task, reporter, live=None: None)
    return asyncio.run(worker.run(worker_task(), "Do it", reporter))


def test_nudge_after_the_last_tool_call_becomes_one_more_prompt_in_the_same_run(monkeypatch):
    client = FakeClient([result("Did it.", 0.1)], [result("Did it, and Z.", 0.3)])
    reporter = Pending("and Z")
    outcome = worker_run(monkeypatch, client, reporter)
    assert client.prompts == ["Do it", nudge_text(["and Z"], finished=True)]
    assert "and Z" in client.prompts[1] and "summary of the whole task" in client.prompts[1]
    assert outcome == Outcome("Did it, and Z.", "", 0.3)  # the CLI's cost is a running total
    assert reporter.messages == []


def test_questions_and_failures_keep_late_nudges(monkeypatch):
    ask = AssistantMessage([ToolUseBlock("1", "mcp__task__ask_user", {"question": "A or B?"})], model="m")
    for script in ([ask, result("Asked.", 0.1)], [result("It broke", 0.1, error=True)]):
        client, reporter = FakeClient(script), Pending("and Z")
        worker_run(monkeypatch, client, reporter)
        assert client.prompts == ["Do it"]
        assert reporter.messages == ["and Z"]


@pytest.mark.parametrize("project", PROJECTS)
def test_nudge_arriving_as_the_run_ends_carries_on_as_a_follow_up(env, monkeypatch, project):
    async def body(task, prompt, reporter) -> Outcome:
        reporter.session(task["id"], "s1")
        reporter.reply(task["id"], "and Z")  # after the session's last check
        return Outcome("Did it.", "", 0.01)

    prompts = fake_run(monkeypatch, body, finishes("Did it, and Z.", 0.02))

    async def main():
        r = runner.Runner()
        task = r.start("code_change", "a thing", "Do it", project)
        await settle(r)
        return r, r.store.get(task["id"])

    r, task = asyncio.run(main())
    assert prompts == ["Do it", nudge_text(["and Z"], finished=True)]
    assert task["status"] == "done" and task["summary"] == "Did it, and Z." and task["cost"] == pytest.approx(0.03)
    assert len(said(r, task["id"], "done")) == 1  # announced once, when it really finished
    assert said(r, task["id"], "progress") == ["nudge received: and Z"]
    assert nudges(task) == []


@pytest.mark.parametrize("ending", ["failed", "stopped"])
def test_nudge_not_delivered_before_a_failure_or_stop_is_reported_and_goes_with_the_next_reply(env, monkeypatch, ending):
    gate = asyncio.Event()

    async def body(task, prompt, reporter) -> Outcome:
        reporter.session(task["id"], "s1")
        if ending == "stopped":
            await gate.wait()  # stopped while it works
        reporter.reply(task["id"], "and Z")
        return Outcome("", "it broke", 0.01)

    prompts = fake_run(monkeypatch, body, finishes())

    async def main():
        r = runner.Runner()
        task = r.start("code_change", "a thing", "Do it", "other")
        if ending == "stopped":
            await until(lambda: prompts)  # its run has begun
            r.reply(task["id"], "and Z")
            r.stop(task["id"])
        await settle(r)
        ended = r.store.get(task["id"])
        r.reply(task["id"], "try again")
        await settle(r)
        return ended, r.store.get(task["id"])

    ended, task = asyncio.run(main())
    assert ended["status"] == ending
    assert "wasn't delivered" in ended["spoken"]
    assert nudges(ended) == ["and Z"]
    assert prompts[1] == with_nudges(f"Follow-up from {profile.name()}: try again", ["and Z"], started=True)
    assert task["status"] == "done" and nudges(task) == []


def test_nudge_sent_while_a_task_asks_goes_with_the_answer(env, monkeypatch):
    async def body(task, prompt, reporter) -> Outcome:
        reporter.session(task["id"], "s1")
        reporter.ask(task["id"], "A or B?")
        reporter.reply(task["id"], "and Z")
        return Outcome("Asked.", "", 0.01)

    prompts = fake_run(monkeypatch, body, finishes())

    async def main():
        r = runner.Runner()
        task = r.start("code_change", "a thing", "Do it", "video-agent")
        await settle(r)
        asking = r.store.get(task["id"])
        r.reply(task["id"], "B")
        await settle(r)
        return asking, r.store.get(task["id"])

    asking, task = asyncio.run(main())
    assert asking["status"] == "waiting" and nudges(asking) == ["and Z"]
    assert "wasn't delivered" not in asking["spoken"]
    assert prompts[1] == f"Answer from {profile.name()}: B\n\nAlso from {profile.name()}, sent while you were busy:\n- and Z"
    assert task["status"] == "done"


# ── Queued tasks ────────────────────────────────────────────────


@pytest.mark.parametrize("project", PROJECTS)
def test_nudging_a_queued_task_adds_to_its_instruction_before_it_starts(env, monkeypatch, project):
    gate = asyncio.Event()

    async def blocker(task, prompt, reporter) -> Outcome:
        await gate.wait()
        return Outcome("First done.", "", 0.01)

    prompts = fake_run(monkeypatch, blocker, finishes())

    async def main():
        r = runner.Runner(capacity=1)  # so the worktree task queues too
        r.start("code_change", "first", "One", project)
        await until(lambda: len(prompts) == 1)
        second = r.start("code_change", "second", "Two", project)
        r.reply(second["id"], "use option A")
        queued = r.reply(second["id"], "and skip the docs")
        gate.set()
        await settle(r)
        return r, queued, r.store.get(second["id"])

    r, queued, task = asyncio.run(main())
    assert queued["status"] == "queued"
    brief = with_nudges("Two", ["use option A", "and skip the docs"], started=False)
    assert "before you started:\n- use option A\n- and skip the docs" in brief
    assert prompts[1] == brief and task["instruction"] == brief
    assert task["status"] == "done" and nudges(task) == []
    assert said(r, task["id"], "progress") == ["nudge received: use option A", "nudge received: and skip the docs"]
