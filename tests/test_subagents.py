"""Tests for subagents: which helpers tasks get, progress log, waiting for reports, the voice brain."""

import asyncio

import pytest
from claude_agent_sdk import (
    AssistantMessage,
    PermissionResultDeny,
    TaskNotificationMessage,
    TaskStartedMessage,
    TaskUpdatedMessage,
    ToolPermissionContext,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)
from test_in_place import NullReporter, env, worker_task  # noqa: F401  (env is a fixture)
from test_nudges import FakeClient, Pending, result, said, worker_run

from alfred import config
from alfred.brain import cli
from alfred.gates import mcp_gate, shell
from alfred.llm import roles
from alfred.llm.roles import Role
from alfred.state import memory
from alfred.tasks import runner, worker
from conftest import use_profile

EDITS = {"Edit", "Write", "NotebookEdit"}
MAIN = ToolPermissionContext(tool_use_id="t1")
HELPER = ToolPermissionContext(tool_use_id="t2", agent_id="a1")

TASKS = {
    "code_change": worker_task(),
    "in place": worker_task(in_place=1),
    "code_question": worker_task(kind="code_question"),
    "research": worker_task(kind="research", project=None),
}


def task_options(env, monkeypatch, name: str):
    monkeypatch.setattr(worker, "DATA_DIR", env["tmp"] / "data")
    task = TASKS[name] | {"worktree": str(env["video"] if name == "in place" else env["tmp"] / "worktree")}
    return worker._options(task, NullReporter())


# ── Which subagents each task gets ──────────────────────────────


@pytest.mark.parametrize("name, agents", [
    ("code_change", {"code-searcher", "test-runner"}),
    ("in place", {"code-searcher", "test-runner"}),
    ("code_question", {"code-searcher"}),
    ("research", {"web-researcher"}),
])
def test_each_kind_of_task_gets_its_own_subagents(env, monkeypatch, name, agents):
    opts = task_options(env, monkeypatch, name)
    assert set(opts.agents) == agents  # beside Claude Code's own (Explore, Plan, general-purpose), which aren't denied
    assert not any(d.startswith(("Agent", "Task")) for d in opts.disallowed_tools)
    for agent in opts.agents.values():
        assert not (EDITS | {"Agent"}) & set(agent.tools)  # none edits or starts its own subagents
        assert agent.model != roles.model(Role.TASK) and agent.maxTurns


def test_in_place_tasks_keep_refusing_git_writes(env, monkeypatch):
    opts = task_options(env, monkeypatch, "in place")
    assert "Bash(git push:*)" in opts.disallowed_tools


def test_only_the_test_runner_gets_a_shell_and_the_searcher_is_read_only():
    assert worker.SEARCHER.tools == ["Read", "Glob", "Grep"]
    assert worker.RESEARCHER.tools == ["WebSearch", "WebFetch"]
    assert "Bash" in worker.TESTER.tools and not EDITS & set(worker.TESTER.tools)
    assert worker.SEARCHER.model == roles.model("fast")
    for agent in (worker.SEARCHER, worker.TESTER, worker.RESEARCHER):
        assert "Don't edit" in agent.prompt


def test_the_brief_tells_the_task_how_to_use_its_subagents(env, monkeypatch):
    for name in TASKS:
        prompt = task_options(env, monkeypatch, name).system_prompt
        text = prompt["append"] if isinstance(prompt, dict) else prompt
        assert "Agent tool" in text and "Only you edit files" in text


def test_nudges_wait_for_the_task_s_own_session_not_a_subagent():
    reporter = Pending("skip Y")
    [matcher] = worker._nudge_hook(9, reporter)
    deliver = matcher.hooks[0]
    inside = {"hook_event_name": "PostToolUse", "tool_name": "Grep", "agent_id": "a1", "agent_type": "code-searcher"}
    assert asyncio.run(deliver(inside, "t1", None)) == {} and reporter.messages == ["skip Y"]
    out = asyncio.run(deliver({"hook_event_name": "PostToolUse", "tool_name": "Agent"}, "t2", None))
    assert "skip Y" in out["hookSpecificOutput"]["additionalContext"] and reporter.messages == []


# ── Progress log ────────────────────────────────────────────────


class Recorder(NullReporter):
    def __init__(self) -> None:
        self.log: list[str] = []
        self.activities: list[str] = []

    def activity(self, task_id: int, text: str) -> None:
        self.activities.append(text)

    def subagent(self, task_id: int, text: str) -> None:
        self.log.append(text)

    def take_nudges(self, task_id: int) -> list[str]:
        return []


def said_by(parent: str | None, *blocks) -> AssistantMessage:
    return AssistantMessage(content=list(blocks), model="m", parent_tool_use_id=parent)


def agent_call(id: str, kind: str, description: str, tool: str = "Task", **extra) -> ToolUseBlock:
    """Subagent tool call; the bundled CLI names the tool "Task", newer CLIs "Agent"."""
    return ToolUseBlock(id=id, name=tool, input={"subagent_type": kind, "description": description, "prompt": "..."} | extra)


def tool_result(id: str, error: bool = False) -> UserMessage:
    return UserMessage(content=[ToolResultBlock(tool_use_id=id, content="report", is_error=error)])


def test_subagent_starts_and_ends_go_in_the_log_and_their_steps_show_as_activity(monkeypatch):
    client = FakeClient([
        said_by(None, agent_call("a1", "code-searcher", "find the retry logic"),
                agent_call("a2", "test-runner", "run the tests", tool="Agent")),
        said_by("a1", ToolUseBlock(id="g1", name="Grep", input={"pattern": "retry"})),
        said_by("a2", ToolUseBlock(id="b1", name="Bash", input={"command": "pytest -q"})),
        UserMessage(content=[ToolResultBlock(tool_use_id="g1", content="x.py")], parent_tool_use_id="a1"),  # inside it: ignored
        tool_result("a1"),
        tool_result("a2", error=True),
        said_by(None, ToolUseBlock(id="e1", name="Edit", input={"file_path": "/repo/x.py"})),
        result("Fixed it.", 0.2),
    ])
    reporter = Recorder()
    outcome = worker_run(monkeypatch, client, reporter)
    assert outcome.result == "Fixed it." and not outcome.error
    assert reporter.log == [
        "code searcher started: find the retry logic",
        "test runner started: run the tests",
        "code searcher finished",
        "test runner failed",
    ]
    assert reporter.activities == ["code searcher: searching the code", "test runner: running pytest -q", "editing x.py"]


def test_a_background_subagent_ends_with_its_notification(monkeypatch):
    client = FakeClient([
        said_by(None, agent_call("a1", "web-researcher", "prices", run_in_background=True)),
        tool_result("a1"),  # only says it launched
        TaskNotificationMessage(subtype="task_notification", data={}, task_id="t1", status="completed",
                                output_file="", summary="done", uuid="u", session_id="s1", tool_use_id="a1"),
        result("Here's what I found.", 0.1),
    ])
    reporter = Recorder()
    worker_run(monkeypatch, client, reporter)
    assert reporter.log == ["web researcher started in the background: prices", "web researcher finished"]


# ── Waiting for their reports ───────────────────────────────────


def launched(id: str) -> UserMessage:
    """Agent tool result for a subagent the CLI backgrounded (its default)."""
    text = "Async agent launched successfully. (This tool result is internal metadata.)\nagentId: x1"
    return UserMessage(content=[ToolResultBlock(tool_use_id=id, content=[{"type": "text", "text": text}])])


def notified(id: str, status: str = "completed") -> TaskNotificationMessage:
    return TaskNotificationMessage(subtype="task_notification", data={}, task_id="t1", status=status,
                                   output_file="", summary="done", uuid="u", session_id="s1", tool_use_id=id)


TESTS_IN_BACKGROUND = [said_by(None, agent_call("a1", "test-runner", "run the tests", tool="Agent")), launched("a1")]


@pytest.fixture
def quick(monkeypatch):
    monkeypatch.setattr(worker, "WAKE_GRACE", 0.05)
    monkeypatch.setattr(worker, "SUBAGENT_WAIT", 5.0)


def test_a_turn_ending_with_a_subagent_still_running_waits_for_it_and_its_report(monkeypatch, quick):
    """The CLI wakes the session when the subagent finishes; the run continues to that turn's report."""
    client = FakeClient([*TESTS_IN_BACKGROUND, result("I'll wait for the test runner.", 0.1), 0.1, notified("a1"),
                         said_by(None), result("All 40 tests pass.\nDetails.", 0.3)])
    reporter = Recorder()
    outcome = worker_run(monkeypatch, client, reporter)
    assert outcome == worker.Outcome("All 40 tests pass.\nDetails.", "", 0.3)
    assert client.prompts == ["Do it"]
    assert reporter.log == ["test runner started: run the tests", "test runner finished"]  # not before it had


def test_if_the_cli_doesnt_wake_the_session_the_run_does(monkeypatch, quick):
    client = FakeClient([*TESTS_IN_BACKGROUND, result("Waiting on the tests.", 0.1), notified("a1")],
                        [result("Tests pass.", 0.2)])
    outcome = worker_run(monkeypatch, client, Recorder())
    assert client.prompts == ["Do it", worker.SUBAGENTS_DONE]
    assert outcome == worker.Outcome("Tests pass.", "", 0.2)


def test_a_terminal_task_update_also_ends_a_subagent(monkeypatch, quick):
    client = FakeClient([
        said_by(None, agent_call("a1", "code-searcher", "find it", tool="Agent")),
        TaskStartedMessage(subtype="task_started", data={}, task_id="k7", description="find it", uuid="u",
                           session_id="s1", tool_use_id="a1", task_type="local_agent"),
        launched("a1"), result("Waiting.", 0.1),
        TaskUpdatedMessage(subtype="task_updated", data={}, task_id="k7", patch={"status": "killed"}, status="killed"),
    ], [result("The searcher was stopped, so I searched myself: it's in x.py.", 0.2)])
    reporter = Recorder()
    outcome = worker_run(monkeypatch, client, reporter)
    assert not outcome.error and reporter.log[-1] == "code searcher failed"


def test_subagents_that_never_finish_fail_the_task_rather_than_finish_it(monkeypatch, quick):
    monkeypatch.setattr(worker, "SUBAGENT_WAIT", 0.1)
    client = FakeClient([*TESTS_IN_BACKGROUND, result("Waiting on the tests.", 0.1)])
    outcome = worker_run(monkeypatch, client, Recorder())
    assert outcome.error == "its subagents (test runner) were still running after 0.00166667 minutes"
    assert client.prompts == ["Do it"]


def test_a_session_ending_under_its_subagents_fails_saying_so(monkeypatch, quick):
    client = FakeClient([*TESTS_IN_BACKGROUND, result("Waiting on the tests.", 0.1)], end=True)
    outcome = worker_run(monkeypatch, client, Recorder())
    assert outcome.error == "the session ended while its subagents (test runner) were still running"
    assert outcome.cost == 0.1



# ── What a run cost ─────────────────────────────────────────────

OPUS, HAIKU = "claude-opus-5-5", "claude-haiku-5-5"
MAIN_USE = {"input_tokens": 10, "output_tokens": 300, "cache_read_input_tokens": 50_000}
SUB_USE = {"input_tokens": 5, "output_tokens": 80, "cache_creation_input_tokens": 9_000}


def call(id: str, model: str, usage: dict, parent: str | None = None) -> AssistantMessage:
    return AssistantMessage([ToolUseBlock(id=f"t-{id}", name="Read", input={"file_path": "/repo/x.py"})], model=model,
                            parent_tool_use_id=parent, usage=usage, message_id=id)


def test_a_runs_cost_adds_up_its_calls_once_each_not_the_clis_running_total(monkeypatch):
    """Resumed, the CLI's total starts from the session's earlier runs; one call can come as several messages."""
    client = FakeClient([call("m1", OPUS, MAIN_USE), call("m1", OPUS, MAIN_USE), call("m2", HAIKU, SUB_USE, parent="a1"),
                         result("Done.", 9.0)])
    outcome = worker_run(monkeypatch, client, Recorder())
    assert outcome.cost == pytest.approx(config.MODELS.cost(OPUS, MAIN_USE) + config.MODELS.cost(HAIKU, SUB_USE))
    assert 0 < outcome.cost < 0.05


def test_a_model_with_no_price_leaves_the_run_at_the_clis_figure(monkeypatch):
    client = FakeClient([call("m1", OPUS, MAIN_USE), call("m2", "some-new-model", MAIN_USE), result("Done.", 0.7)])
    assert worker_run(monkeypatch, client, Recorder()).cost == 0.7
    # a message with no tokens (the CLI's own) needs no price
    client = FakeClient([call("m1", OPUS, MAIN_USE), call("m2", "<synthetic>", {"input_tokens": 0}), result("Done.", 0.7)])
    assert worker_run(monkeypatch, client, Recorder()).cost == pytest.approx(config.MODELS.cost(OPUS, MAIN_USE))


def test_a_session_ending_without_any_result_fails(monkeypatch):
    client = FakeClient([said_by(None, ToolUseBlock(id="e1", name="Edit", input={"file_path": "/repo/x.py"}))], end=True)
    assert worker_run(monkeypatch, client, Recorder()).error == "the session ended before its final report"


def test_a_missing_report_is_asked_for_once_then_fails_the_task(monkeypatch):
    client = FakeClient([result("", 0.1)], [result("Fixed the retry logic.\nDetails.", 0.2)])
    outcome = worker_run(monkeypatch, client, Recorder())
    assert client.prompts == ["Do it", worker.REPORT_MISSING]
    assert outcome == worker.Outcome("Fixed the retry logic.\nDetails.", "", 0.2)

    client = FakeClient([result("", 0.1)], [result("  ", 0.2)])
    outcome = worker_run(monkeypatch, client, Recorder())
    assert client.prompts == ["Do it", worker.REPORT_MISSING]
    assert outcome.error == "it ended without a final report"


def test_a_question_asked_under_a_running_subagent_waits_for_it_but_isnt_woken(monkeypatch, quick):
    ask = said_by(None, ToolUseBlock(id="q1", name="mcp__task__ask_user", input={"question": "A or B?"}))
    client = FakeClient([*TESTS_IN_BACKGROUND, ask, result("Asked.", 0.1), notified("a1")])
    reporter = Recorder()
    outcome = worker_run(monkeypatch, client, reporter)
    assert client.prompts == ["Do it"] and outcome == worker.Outcome("Asked.", "", 0.1)
    assert reporter.log[-1] == "test runner finished"


def test_a_failed_task_is_marked_failed_with_its_reason(env, monkeypatch):
    async def body(task, prompt, reporter):
        return worker.Outcome("", "the session ended while its subagents (test runner) were still running", 0.1)

    monkeypatch.setattr(runner, "run", body)

    async def main():
        r = runner.Runner()
        task = r.store.create("research", "prices", "Look up prices", None)
        await r._job(task["id"], "Look up prices")
        return r.store.get(task["id"])

    task = asyncio.run(main())
    assert task["status"] == "failed" and "subagents (test runner) were still running" in task["error"]


def test_the_runner_logs_subagents_without_announcing_them(env, monkeypatch):
    async def main():
        r = runner.Runner()
        events: asyncio.Queue = asyncio.Queue()
        r.listeners.add(events)
        task = r.store.create("research", "prices", "Look up prices", None)
        r.subagent(task["id"], "web researcher started: prices")
        return r, task, events

    r, task, events = asyncio.run(main())
    assert said(r, task["id"], "subagent") == ["web researcher started: prices"]
    assert r.store.get(task["id"])["activity"] == "web researcher started: prices"
    assert events.empty()


# ── The voice brain ─────────────────────────────────────────────


@pytest.fixture
def voice_options(tmp_path, monkeypatch):
    use_profile(monkeypatch, tmp_path)
    monkeypatch.setattr(memory, "read_memory", lambda: "")
    monkeypatch.setattr(memory, "read_recent", lambda: "")
    return cli._options(mcp_gate.Gate(), shell.Jobs())


def test_voice_brain_has_claude_codes_tools_and_still_its_lookups(voice_options):
    """Claude Code's own tools and subagents are there, but file and computer lookups still run beside the turn."""
    opts = voice_options
    assert not opts.agents and opts.tools is None  # all of Claude Code's tools
    assert not any(d.startswith(("Agent", "Task")) for d in opts.disallowed_tools)
    assert {"mcp__alfred__file_lookup", "mcp__alfred__computer_lookup"} <= set(opts.allowed_tools)
    assert "file_lookup" in opts.system_prompt and "code_question background task" in opts.system_prompt
    assert {"alfred", "shell"} <= set(opts.mcp_servers) and shell.JOB_STATUS in opts.allowed_tools  # long jobs kept


def test_voice_prompt_hands_multi_step_work_to_background_tasks(voice_options):
    prompt = voice_options.system_prompt
    for words in ("Anything with several steps goes to a background task", "installing or setting something up",
                  "fixing what a build or test run turned up", "stop after your second step"):
        assert words in prompt
    # the shell does one command a call; checks go to computer_lookup, long work to start_job
    assert "one command a call" in shell.prompt() and "use computer_lookup" in shell.prompt()
    assert "start_job" in shell.prompt()
    for words in ("You talk; helpers and tasks do the work", "computer_lookup for anything about this computer",
                  "Alfred refuses more"):
        assert words in prompt


def test_voice_gate_refuses_subagents_and_file_reads():
    g = shell.ShellGate()
    for name, args in (("Agent", {"subagent_type": "lookup"}), ("Read", {"file_path": "/Users/x/app/pyproject.toml"}),
                       ("Edit", {"file_path": "/tmp/x"})):
        assert isinstance(asyncio.run(g(name, args, MAIN)), PermissionResultDeny)
