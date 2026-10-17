"""Tests for jarvis.llm.retry: typed failure kinds, Policy schedules, retry(), and claude.ask timeouts."""

import asyncio
import logging
from dataclasses import replace

import pytest
from claude_agent_sdk import (
    AssistantMessage,
    CLIConnectionError,
    CLIJSONDecodeError,
    CLINotFoundError,
    ProcessError,
    RateLimitEvent,
    RateLimitInfo,
    ResultError,
    ResultMessage,
    TextBlock,
)
from test_in_place import NullReporter
from test_nudges import FakeClient, result, worker_run

from jarvis.llm import claude, retry
from jarvis.llm.retry import ERROR, LIMIT, PASSING, CallFailed, Policy
from jarvis.tasks import client
from jarvis.voice import session

# ── Telling failures apart ──────────────────────────────────────


@pytest.mark.parametrize("error, kind", [
    (CallFailed("the model call failed: success", LIMIT), LIMIT),
    (ResultError("failed", {"subtype": "success", "api_error_status": 529}), PASSING),
    (ResultError("failed", {"subtype": "success", "api_error_status": 429}), LIMIT),
    (ResultError("failed", {"subtype": "success", "api_error_status": 401}), ERROR),
    (ResultError("failed", {"subtype": "error_during_execution", "terminal_reason": "api_error"}), PASSING),
    (ResultError("failed", {"subtype": "error_max_turns", "terminal_reason": "max_turns"}), ERROR),
    (CLIConnectionError("the CLI went away"), PASSING),
    (CLINotFoundError(), ERROR),  # not installed: waiting won't help
    (ProcessError("exited", exit_code=1), PASSING),
    (CLIJSONDecodeError("{", ValueError()), PASSING),
    (TimeoutError(), PASSING),
    (ConnectionResetError(), PASSING),
    (RuntimeError("API Error: 529 overloaded, connection timed out"), ERROR),  # the words don't count
    (ValueError("the project's folder is gone"), ERROR),
])
def test_an_error_is_classified_by_its_type_and_what_the_cli_reported(error, kind):
    assert retry.classify(error) == kind


@pytest.mark.parametrize("args, kind", [
    ({"rejected": True}, LIMIT),
    ({"status": 429}, LIMIT),
    ({"error": "rate_limit"}, LIMIT),
    ({"status": 529}, PASSING),
    ({"status": 503}, PASSING),
    ({"status": 408}, PASSING),
    ({"error": "server_error"}, PASSING),
    ({"reason": "api_error"}, PASSING),
    ({"status": 400, "reason": "api_error"}, ERROR),
    ({"error": "authentication_failed"}, ERROR),
    ({"error": "billing_error"}, ERROR),
    ({}, ERROR),
])
def test_a_failed_result_is_classified_by_its_status_reason_and_rate_limit(args, kind):
    assert retry.result_kind(**args) == kind


def test_a_callers_own_types_can_count_as_passing():
    down = client.RunnerUnavailable("down")
    assert retry.classify(down) == ERROR
    assert retry.classify(down, (client.RunnerUnavailable,)) == PASSING
    assert Policy(passing=(client.RunnerUnavailable,)).kind(down) == PASSING


# ── A policy's schedule ─────────────────────────────────────────


def test_a_policy_waits_in_turn_then_gives_up():
    p = Policy(waits=(300, 900, 1800))
    assert [p.wait(n, PASSING) for n in (1, 2, 3, 4)] == [300, 900, 1800, None]
    assert p.wait(1, LIMIT) == 300 and p.wait(1, ERROR) is None  # an error that won't pass isn't tried again
    assert Policy().wait(1, PASSING) is None  # one try


def test_a_policy_forever_keeps_its_last_wait_and_can_retry_every_kind():
    p = Policy(waits=(30,), retries=retry.KINDS, forever=True)
    assert [p.wait(n, ERROR) for n in (1, 2, 50)] == [30, 30, 30]


def test_each_call_sites_schedule_is_its_policy():
    from jarvis.tasks import learning, scheduler

    assert scheduler.RUNS.waits == (300, 900, 1800) and scheduler.RUNS.retries == {LIMIT, PASSING}
    assert learning.RETRIES.waits == (900, 900)  # three tries, 15 minutes apart
    assert session.RUNNER_EVENTS.wait(1000, ERROR) == 30


# ── retry() ─────────────────────────────────────────────────────


class Calls:
    """Fails with each of `failures` in turn, then returns "done"; records the waits in `slept`."""

    def __init__(self, *failures: BaseException) -> None:
        self.failures, self.made, self.slept = list(failures), 0, []

    async def __call__(self) -> str:
        self.made += 1
        if self.failures:
            raise self.failures.pop(0)
        return "done"

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)


def test_retry_tries_again_after_passing_errors_as_the_policy_says():
    calls = Calls(TimeoutError(), CLIConnectionError("gone"))
    assert asyncio.run(retry.retry(Policy(waits=(5, 10)), calls, "test", calls.sleep)) == "done"
    assert (calls.made, calls.slept) == (3, [5, 10])


def test_retry_raises_an_error_that_wont_pass_at_once():
    calls = Calls(ValueError("bad"))
    with pytest.raises(ValueError):
        asyncio.run(retry.retry(Policy(waits=(5, 10)), calls, "test", calls.sleep))
    assert (calls.made, calls.slept) == (1, [])


def test_retry_raises_the_last_failure_once_the_waits_run_out():
    calls = Calls(TimeoutError(), TimeoutError("again"))
    with pytest.raises(TimeoutError, match="again"):
        asyncio.run(retry.retry(Policy(waits=(5,)), calls, "test", calls.sleep))
    assert calls.slept == [5]


def test_once_is_one_try():
    calls = Calls(TimeoutError())
    with pytest.raises(TimeoutError):
        asyncio.run(retry.retry(retry.ONCE, calls, "test", calls.sleep))
    assert calls.made == 1


def test_retries_are_logged_by_type_and_kind_alone(caplog):
    calls = Calls(CallFailed("a secret prompt leaked into the message", PASSING))
    with caplog.at_level(logging.INFO, logger="jarvis.llm.retry"):
        asyncio.run(retry.retry(Policy(waits=(5,)), calls, "the summary", calls.sleep))
    assert "the summary failed (passing: CallFailed)" in caplog.text and "secret" not in caplog.text


# ── claude.ask: typed failures and timeouts ────────────────────────


def ended(**fields) -> ResultMessage:
    return ResultMessage(**{"subtype": "success", "duration_ms": 1, "duration_api_ms": 1, "is_error": True,
                            "num_turns": 1, "session_id": "s", "total_cost_usd": 0.01, "result": "API Error"} | fields)


def rejected() -> RateLimitEvent:
    return RateLimitEvent(RateLimitInfo(status="rejected", rate_limit_type="five_hour", resets_at=2_000_000_000),
                          "u1", "s")


def fake_query(monkeypatch, *messages, hang: bool = False):
    import claude_agent_sdk

    async def query(prompt, options):
        for m in messages:
            yield m
        if hang:
            await asyncio.Event().wait()

    monkeypatch.setattr(claude_agent_sdk, "query", query)


@pytest.mark.parametrize("messages, kind", [
    ([ended(api_error_status=529)], PASSING),
    ([rejected(), ended()], LIMIT),
    ([AssistantMessage([TextBlock("API Error")], "m", error="rate_limit"), ended()], LIMIT),
    ([AssistantMessage([TextBlock("API Error")], "m", error="server_error"), ended()], PASSING),
    ([ended(subtype="error_during_execution")], ERROR),
])
def test_a_failed_ask_says_what_kind_of_failure_it_was(monkeypatch, tmp_path, messages, kind):
    monkeypatch.setattr(claude.usage, "USAGE_FILE", tmp_path / "usage.json")
    fake_query(monkeypatch, *messages)
    with pytest.raises(CallFailed) as failed:
        asyncio.run(claude.ask("Sum it up.", "s", "claude-x"))
    assert failed.value.kind == kind and isinstance(failed.value, RuntimeError)  # callers' except RuntimeError hold


def test_every_ask_has_a_timeout(monkeypatch):
    assert claude.ASK_SECONDS > 0
    fake_query(monkeypatch, hang=True)
    with pytest.raises(TimeoutError):
        asyncio.run(claude.ask("Sum it up.", "s", "claude-x", timeout=0.05))


def test_the_summaries_fail_rather_than_hang(monkeypatch):
    from jarvis.tasks import command_jobs, connection_jobs

    monkeypatch.setattr(claude.ask, "__defaults__", (0.05,))  # ASK_SECONDS, as the default timeout
    fake_query(monkeypatch, hang=True)
    with pytest.raises(TimeoutError):
        asyncio.run(command_jobs.summarize("Build", "make", 5, "ok"))
    with pytest.raises(TimeoutError):
        asyncio.run(connection_jobs.summarize("Issues", "list issues", False, 5, "[]"))
    assert asyncio.run(command_jobs.triage("Build", "make", "/tmp", "exit code 1", 5, "boom", True)) is None


# ── Failure kind of a task's run ────────────────────────────────


@pytest.mark.parametrize("ending, kind", [
    ([ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=True, num_turns=1, session_id="s1",
                    total_cost_usd=0.1, result="API Error: Overloaded", api_error_status=529)], PASSING),
    ([rejected(), result("Claude AI usage limit reached", 0.1, error=True)], LIMIT),
    ([result("It broke.", 0.1, error=True)], ERROR),
])
def test_a_task_run_failure_carries_its_kind(monkeypatch, tmp_path, ending, kind):
    monkeypatch.setattr(claude.usage, "USAGE_FILE", tmp_path / "usage.json")
    outcome = worker_run(monkeypatch, FakeClient(ending), NullReporter())
    assert outcome.error and outcome.kind == kind


def test_a_session_that_ends_before_its_report_is_a_passing_failure(monkeypatch):
    outcome = worker_run(monkeypatch, FakeClient([], end=True), NullReporter())
    assert outcome.error == "the session ended before its final report" and outcome.kind == PASSING


def test_a_run_that_succeeds_has_no_kind(monkeypatch):
    class Reporter(NullReporter):
        def take_nudges(self, task_id: int) -> list[str]:
            return []

    assert worker_run(monkeypatch, FakeClient([result("Did it.", 0.1)]), Reporter()).kind == ""


# ── watch_tasks reconnects ──────────────────────────────────────


def test_watch_tasks_follows_the_events_again_whatever_ended_them(monkeypatch):
    streams = [client.RunnerUnavailable("down"), [{"type": "approval", "approval": {"id": 1}}], ValueError("bad")]
    added, followed = [], []

    async def events():
        followed.append(len(streams))
        if not streams:
            raise asyncio.CancelledError  # the session is over
        stream = streams.pop(0)
        if isinstance(stream, Exception):
            raise stream
        for e in stream:
            yield e

    monkeypatch.setattr(client, "events", events)
    monkeypatch.setattr(session, "RUNNER_EVENTS", replace(session.RUNNER_EVENTS, waits=(0,)))
    voice = session.VoiceSession(None)
    voice.asks = type("Asks", (), {"add": lambda self, a: added.append(a)})()
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(voice.watch_tasks())
    assert added == [{"id": 1}] and followed == [3, 2, 1, 0]  # down, the stream's end, the error: followed again
