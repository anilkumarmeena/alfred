"""Tests for jarvis.state.activity: which tools count as changes, the post-tool hook, retention, runner events."""

import asyncio
import sqlite3
import time

import pytest

from conftest import use_profile
from jarvis import panel, tools
from jarvis.brain import cli
from jarvis.core import database, profile
from jarvis.integrations.mcp_servers import Server, Tool
from jarvis.safety import mcp_gate
from jarvis.state import activity
from jarvis.tasks import runner

ACME = Server("acme", "Acme", command="acme-mcp",
              tools={"find": Tool("read"), "post": Tool("write", "post to {channel}, saying {text}"),
                     "mark": Tool("own")})


@pytest.fixture
def db(tmp_path, monkeypatch):
    use_profile(monkeypatch, tmp_path)


def texts() -> list[str]:
    return [e["text"] for e in activity.recent()]


# ── Which tools change something ────────────────────────────────


def test_the_registry_marks_what_changes_something():
    changes = {t.name for t in tools.all_tools() if t.changes}
    assert {"remember", "memory_replace", "forget", "todo_add", "todo_done", "project_register", "calendar_create_event", "chat_send",
            "sheets_write", "docs_insert", "gmail_modify"} <= changes
    assert not changes & {"todo_list", "calendar_events", "gmail_search", "docs_read", "chat_draft", "task_list",
                          "task_start"}  # reads, drafts and tasks (the runner logs tasks itself)
    assert tools.get("remember").said == "Remembered: {fact}"


# ── After each of the voice brain's tool calls ──────────────────


def ran(name: str, args: dict, response, gate: mcp_gate.Gate | None = None) -> None:
    """Run the voice brain's PostToolUse hook for a tool call that ran."""
    gate = gate or mcp_gate.Gate()
    hook = cli._bell_hook(gate)[0].hooks[0]
    asyncio.run(hook({"tool_name": name, "tool_input": args, "tool_response": response}, None, None))


def test_only_tools_that_changed_something_are_kept_in_their_own_words(db):
    ran("mcp__jarvis__calendar_events", {}, "3 events today.")  # a read
    ran("mcp__jarvis__calendar_create_event", {"title": "Call"},
        [{"type": "text", "text": "Created 'Call' on the work calendar: https://calendar.example/e"}])
    ran("mcp__jarvis__todo_add", {"item": "Milk"},
        {"content": [{"type": "text", "text": "Added to the to-do list: Milk"}]})
    ran("mcp__jarvis__remember", {"fact": "Likes tea"}, "Saved: Likes tea")
    ran("mcp__jarvis__docs_replace_text", {"document": "x"}, "No 'foo' in 'Plan': nothing changed.")  # nothing to keep
    ran("mcp__jarvis__gmail_modify", {"message_ids": ["1"]}, '{"modified": 1}')  # structured: the tool's name
    ran("mcp__jarvis__todo_done", {"text": "x"}, {"is_error": True, "content": "No open to-do matches"})  # failed
    ran("Bash", {"command": "ls"}, "a b")
    assert texts() == ["Gmail modify", "Remembered: Likes tea", "Added to the to-do list: Milk",
                       "Created 'Call' on the work calendar"]


def test_a_memory_replaced_is_kept_and_a_save_refused_as_full_is_not(db, tmp_path, monkeypatch):
    profile.current().memory_file.write_text("## User\n" + "".join(f"- Fact {n} {'x' * 375} (2026-09-01)\n" for n in range(5)))  # 1,985 chars

    def called(name: str, args: dict) -> None:
        """Run the tool as the SDK does, then the hook with its response."""
        content, is_error = asyncio.run(tools.run(name, args))
        ran(f"mcp__jarvis__{name}", args, {"is_error": is_error, "content": [{"type": "text", "text": content}]})

    called("remember", {"fact": "Likes tea", "section": "user"})  # full: refused
    assert texts() == [] and "Likes tea" not in profile.current().memory_file.read_text()
    called("memory_replace", {"old": "Fact 3", "fact": "Likes tea"})
    assert texts() == ["Changed a memory to: Likes tea"]


def test_an_mcp_server_write_is_kept_once_it_has_run_and_a_read_never(db):
    gate = mcp_gate.Gate()
    gate.use([ACME])
    ran("mcp__acme__find", {"q": "x"}, "found", gate)
    ran("mcp__acme__post", {"channel": "general", "text": "hi"}, "posted", gate)
    assert texts() == ["On Acme, post to general, saying hi."]


def test_the_gate_says_what_a_write_or_own_tool_does_and_nothing_for_a_read():
    gate = mcp_gate.Gate()
    gate.use([ACME])
    said = gate.change("mcp__acme__post", {"channel": "general", "text": "hi"})
    assert said == "On Acme, post to general, saying hi."
    assert gate.change("mcp__acme__mark", {}) == "On Acme, mark."
    assert gate.change("mcp__acme__find", {}) is None
    assert gate.change("mcp__jarvis__todo_add", {}) is None and gate.change("Bash", {}) is None


# ── Keeping entries ─────────────────────────────────────────────


def test_entries_are_trimmed_and_old_ones_go_now_and_then(db, monkeypatch):
    entry = activity.add("x" * 500, "nonsense")
    assert len(entry["text"]) == activity.MAX_TEXT and entry["kind"] == "ok"
    assert activity.add("   ") is None
    now = time.time()
    monkeypatch.setattr(time, "time", lambda: now + activity.KEEP_DAYS * 86400 + 60)
    monkeypatch.setattr(activity, "PRUNE_EVERY", 2)
    activity.add("new")  # adding prunes the week-old entry
    assert texts() == ["new"]
    assert [e["text"] for e in activity.recent(after=activity.latest_id() - 1)] == ["new"]


def test_a_failing_activity_list_never_fails_the_caller(db, monkeypatch):
    def broken(*_, **__):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(database, "table", broken)
    assert activity.add("Milk") is None  # logged, not raised


# ── The runner's events ─────────────────────────────────────────


APPROVAL = {"id": 14, "task": 8, "commands": [{"tool": "host_run", "command": "brew install jq", "folder": "/r",
                                               "why": "installs software (brew install)"}]}


@pytest.mark.parametrize(("event", "said"), [
    ({"type": "status", "text": "Task 3, Docs, is done.", "task": {"id": 3, "status": "done"}},
     ("Task 3, Docs, is done.", "ok", 3)),
    ({"type": "status", "text": "Task 3 failed.", "task": {"id": 3, "status": "failed"}}, ("Task 3 failed.", "bad", 3)),
    ({"type": "approval", "text": "...", "approval": APPROVAL}, ("Task 8 asks to run brew install jq", "warn", 8)),
    ({"type": "approval_answered", "approval": APPROVAL | {"state": "approved", "via": "panel"}},
     ("Approved brew install jq for task 8, in the control panel", "ok", 8)),
    ({"type": "approval_answered", "approval": APPROVAL | {"state": "denied", "via": "voice"}},
     ("Declined brew install jq for task 8, by voice", "bad", 8)),
    ({"type": "approval_answered", "approval": APPROVAL | {"state": "expired", "via": None}},
     ("Task 8's request to run brew install jq: lapsed", "warn", 8)),
    ({"type": "alert", "text": "Heads up. Standup soon."}, ("Heads up. Standup soon.", "warn", None)),
    ({"type": "progress", "text": "step"}, None),
])
def test_what_each_runner_event_says_in_the_bell(event, said):
    assert runner.bell_entry(event) == said


def test_broadcasting_an_event_keeps_it(db):
    r = runner.Runner()
    r.broadcast({"type": "alert", "text": "Heads up."})
    assert [(e["text"], e["kind"]) for e in activity.recent()] == [("Heads up.", "warn")]


def test_the_panel_lists_the_bell_newest_first(db):
    activity.add("first")
    activity.add("second", "run", task=4)
    shown = asyncio.run(panel.call("activity", {}))
    assert [(e["text"], e["kind"], e["task"]) for e in shown] == [("second", "run", 4), ("first", "ok", None)]
