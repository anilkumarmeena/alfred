"""Tests for task token use (alfred.tasks.tokens): counting a run per model and subagent, recording it however the run
ends, keeping it in the task database, and showing it in task details."""

import asyncio
import json
import time

import pytest
from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock, ToolUseBlock
from test_in_place import NullReporter, env, worker_task  # noqa: F401  (env is a fixture)
from test_log_and_discard import Request
from test_nudges import FakeClient, worker_run

from alfred.storage import database
from alfred.tasks import api, client, runner, store, tokens, worker
from alfred.tools import tasks as task_tools

MAIN_MODEL, HELPER_MODEL = "claude-main", "claude-helper"


def call(id: str, input: int, output: int, read: int = 0, write: int = 0, parent: str | None = None,
         model: str = MAIN_MODEL, blocks: int = 1) -> list[AssistantMessage]:
    """One API call's reply, split into a message per content block as the CLI streams it, each with its usage."""
    usage = {"input_tokens": input, "output_tokens": output, "cache_read_input_tokens": read,
             "cache_creation_input_tokens": write}
    return [AssistantMessage(content=[TextBlock(text="...")], model=model, parent_tool_use_id=parent, usage=usage,
                             message_id=id) for _ in range(blocks)]


def finished(text: str = "Done.", cost: float = 0.1, models: dict | None = None, error: bool = False) -> ResultMessage:
    return ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=error, num_turns=1,
                         session_id="s1", total_cost_usd=cost, result=text, model_usage=models)


def used(input: int, output: int, read: int = 0, write: int = 0) -> dict:
    return {"inputTokens": input, "outputTokens": output, "cacheReadInputTokens": read,
            "cacheCreationInputTokens": write, "webSearchRequests": 0, "costUSD": 0.0, "contextWindow": 200000,
            "maxOutputTokens": 32000}


def row(agent: str, model: str, input: int, output: int, read: int = 0, write: int = 0) -> dict:
    return {"agent": agent, "model": model, "input": input, "output": output, "cache_read": read, "cache_write": write}


def tallied(*messages) -> list[dict]:
    tally = tokens.Tally()
    for msg in messages:
        if isinstance(msg, AssistantMessage):
            tally.saw(msg, "code searcher" if msg.parent_tool_use_id else tokens.MAIN)
        else:
            tally.result(msg)
    return tally.rows()


# ── Counting a run ──────────────────────────────────────────────


def test_a_call_split_into_several_messages_counts_once():
    assert tallied(*call("m1", 10, 200, 3000, 400, blocks=3)) == [row("", MAIN_MODEL, 10, 200, 3000, 400)]


def test_subagents_get_their_own_calls_and_the_main_session_what_the_result_says_is_left():
    rows = tallied(*call("m1", 10, 100, 1000), *call("h1", 5, 50, 500, parent="a1", model=HELPER_MODEL),
                   *call("m2", 20, 80, 2000),
                   # The result covers calls nothing streamed, a background subagent's say
                   finished(models={MAIN_MODEL: used(30, 180, 3000), HELPER_MODEL: used(15, 90, 900, 10)}))
    assert rows == [row("", HELPER_MODEL, 10, 40, 400, 10), row("", MAIN_MODEL, 30, 180, 3000),
                    row("code searcher", HELPER_MODEL, 5, 50, 500)]
    assert sum(r["output"] for r in rows) == 180 + 90  # every token once


def test_a_model_with_the_cli_s_context_suffix_matches_its_calls():
    rows = tallied(*call("m1", 10, 100), finished(models={f"{MAIN_MODEL}[1m]": used(10, 100)}))
    assert rows == [row("", MAIN_MODEL, 10, 100)]


def test_calls_after_the_latest_result_add_to_it_as_when_the_run_is_stopped_mid_turn():
    rows = tallied(*call("m1", 10, 100), finished(models={MAIN_MODEL: used(12, 110)}), *call("m2", 5, 40))
    assert rows == [row("", MAIN_MODEL, 17, 150)]


def test_a_run_stopped_before_any_result_counts_the_calls_it_saw():
    assert tallied(*call("m1", 10, 100, 50), *call("h1", 1, 2, parent="a1")) == [
        row("", MAIN_MODEL, 10, 100, 50), row("code searcher", MAIN_MODEL, 1, 2)]


def test_a_run_that_used_nothing_has_no_rows():
    assert tallied(finished(models={})) == [] and tallied() == []


def test_rows_from_another_machine_are_kept_only_where_well_formed():
    sent = [row("", MAIN_MODEL, 1, 2), {"model": 5}, "junk", {"agent": "test runner", "model": "m", "input": "x"},
            {"model": "m", "output": -4, "input": "3"}]
    assert tokens.checked(sent) == [row("", MAIN_MODEL, 1, 2), row("", "m", 3, 0)]
    assert tokens.checked({"not": "a list"}) == []


# ── Recording it however the run ends ───────────────────────────


class Recorder(NullReporter):
    def __init__(self) -> None:
        self.recorded: list[tuple[list[dict], str]] = []

    def take_nudges(self, task_id: int) -> list[str]:
        return []

    def tokens(self, task_id: int, rows: list[dict], ended: str) -> None:
        self.recorded.append((rows, ended))


def test_a_finished_run_reports_its_tokens_per_model_and_subagent(monkeypatch):
    client = FakeClient([
        AssistantMessage(content=[ToolUseBlock(id="a1", name="Agent", input={"subagent_type": "test-runner",
                                                                                 "prompt": "p", "description": "d"})],
                         model=MAIN_MODEL, usage={"input_tokens": 10, "output_tokens": 100}, message_id="m1"),
        *call("h1", 3, 30, parent="a1", model=HELPER_MODEL),
        finished("All done.", models={MAIN_MODEL: used(10, 100), HELPER_MODEL: used(3, 30)}),
    ])
    reporter = Recorder()
    assert worker_run(monkeypatch, client, reporter).result == "All done."
    assert reporter.recorded == [([row("", MAIN_MODEL, 10, 100), row("test runner", HELPER_MODEL, 3, 30)],
                                  tokens.FINISHED)]


def test_a_failed_run_reports_its_tokens_as_failed(monkeypatch):
    client = FakeClient([*call("m1", 10, 100), finished("API Error", models={MAIN_MODEL: used(10, 100)}, error=True)])
    reporter = Recorder()
    assert worker_run(monkeypatch, client, reporter).error
    assert reporter.recorded == [([row("", MAIN_MODEL, 10, 100)], tokens.FAILED)]


def test_a_stopped_run_reports_the_tokens_it_had_used(monkeypatch):
    client = FakeClient([*call("m1", 10, 100), 30])  # stopped while it waits for the next message
    reporter = Recorder()
    monkeypatch.setattr(worker, "ClaudeSDKClient", client)
    monkeypatch.setattr(worker, "_options", lambda task, reporter, live=None: None)

    async def main():
        running = asyncio.create_task(worker.run(worker_task(), "Do it", reporter))
        await asyncio.sleep(0.1)
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running

    asyncio.run(main())
    assert reporter.recorded == [([row("", MAIN_MODEL, 10, 100)], tokens.STOPPED)]


def test_a_failure_to_record_never_changes_how_the_run_ended(monkeypatch):
    class Broken(Recorder):
        def tokens(self, *a):
            raise RuntimeError("database is locked")

    outcome = worker_run(monkeypatch, FakeClient([*call("m1", 1, 2), finished("Done.")]), Broken())
    assert outcome == worker.Outcome("Done.", "", 0.1)


# ── Kept in the database, shown in task details ─────────────────


def test_the_table_is_made_by_its_migration_and_kept_per_run(env):
    r = runner.Runner()
    task = r.store.create("research", "prices", "Look up prices", None)
    r.tokens(task["id"], [row("", MAIN_MODEL, 10, 100, 1000, 50), row("web researcher", HELPER_MODEL, 2, 20)],
             tokens.FINISHED)
    r.tokens(task["id"], [row("", MAIN_MODEL, 5, 40)], tokens.STOPPED)
    kept = r.store.tokens(task["id"])
    assert [(k["agent"], k["model"], k["ended"], k["output"]) for k in kept] == [
        ("", MAIN_MODEL, "finished", 100), ("web researcher", HELPER_MODEL, "finished", 20), ("", MAIN_MODEL, "stopped", 40)]
    assert database._version(r.store.db, "task_tokens") == 1
    assert store.Store().tokens(task["id"]) == kept  # opening again leaves it as it is


def test_a_task_s_tokens_add_up_over_its_runs_by_model_and_by_agent():
    at = time.time()
    rows = [row("", MAIN_MODEL, 10, 100, 1000, 50) | {"at": at}, row("test runner", HELPER_MODEL, 2, 20) | {"at": at},
            row("", MAIN_MODEL, 5, 40) | {"at": at + 60}]
    shown = tokens.summary(rows)
    assert shown["total"] == 1227 and shown["runs"] == 2
    assert {k: shown[k] for k in tokens.FIELDS} == {"input": 17, "output": 160, "cache_read": 1000, "cache_write": 50}
    assert list(shown["by_model"]) == [MAIN_MODEL, HELPER_MODEL] and shown["by_model"][HELPER_MODEL]["total"] == 22
    assert shown["by_agent"]["main session"]["total"] == 1205 and shown["by_agent"]["test runner"]["output"] == 20
    assert tokens.summary([]) is None


def test_pruned_hidden_runs_take_their_token_rows_with_them(env):
    r = runner.Runner()
    old = r.store.create("research", "watch", "Check it", None, hidden=True)
    r.store.create("research", "newest", "Keep it", None)
    r.tokens(old["id"], [row("", MAIN_MODEL, 1, 2)], tokens.FINISHED)
    r.store.db.execute("update tasks set status = 'done', updated = 0 where id = ?", (old["id"],))
    assert r.store.prune_hidden(1) == [old["id"]] and r.store.tokens(old["id"]) == []


def route(r: runner.Runner, suffix: str):
    return next(d.handler for d in api.routes(r) if d.method == "GET" and d.path.endswith(suffix))


def test_task_details_show_its_token_use_on_the_api_and_to_the_voice(env, monkeypatch):
    async def main():
        r = runner.Runner()
        task = r.store.create("research", "prices", "Look up prices", None)
        bare = json.loads((await route(r, "/log")(Request(task["id"]))).text)
        r.tokens(task["id"], [row("", MAIN_MODEL, 10, 100, 1000, 50), row("web researcher", HELPER_MODEL, 2, 20)],
                 tokens.FINISHED)
        r.store.set(task["id"], cost=0.4321)
        shown = json.loads((await route(r, "+}")(Request(task["id"]))).text)
        tail = json.loads((await route(r, "/log")(Request(task["id"]))).text)
        return r, bare, shown, tail

    r, bare, shown, tail = asyncio.run(main())
    assert bare["tokens"] is None
    assert shown["tokens"] == tail["tokens"] and shown["tokens"]["total"] == 1182
    assert shown["tokens"]["by_agent"]["web researcher"]["total"] == 22

    async def task(task_id):
        return shown

    async def nothing(*a):
        return []

    monkeypatch.setattr(client, "task", task)
    monkeypatch.setattr(client, "mark_seen", nothing)
    monkeypatch.setattr(client, "approvals", nothing)
    detail = asyncio.run(task_tools.task_detail(shown["id"]))
    assert detail["tokens"]["total"] == 1182 and detail["cost_usd"] == 0.43
    from alfred import tools

    assert "how many tokens" in tools.get("task_detail").description
