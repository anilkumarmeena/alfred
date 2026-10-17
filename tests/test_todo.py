"""Tests for jarvis.state.todo: the to-do list in data/tasks.db."""

import asyncio
import json
import sqlite3
from datetime import date, datetime

import pytest

from conftest import use_profile
from jarvis import tools
from jarvis.brain import persona
from jarvis.state import todo
from jarvis.tasks import store as store_module
from jarvis.tools import core

TODAY = f"{date.today():%Y-%m-%d}"


@pytest.fixture
def db(tmp_path, monkeypatch):
    use_profile(monkeypatch, tmp_path)
    return tmp_path / "tasks.db"


def rows(db) -> list[tuple]:
    return sqlite3.connect(db).execute("select id, text, done, done_at is not null from todos order by id").fetchall()


def test_an_empty_list_reads_as_empty(db):
    assert todo.read() == ""
    assert core.todo_list() == "The to-do list is empty."


def test_add_tidies_the_item_and_keeps_when(db):
    assert todo.add("  Build   an iPhone app ") == "Added to the to-do list: Build an iPhone app"
    assert todo.read() == f"# To-do\n\n- [ ] Build an iPhone app (added {TODAY})\n"
    (created,) = sqlite3.connect(db).execute("select created from todos").fetchone()
    assert f"{datetime.fromtimestamp(created):%Y-%m-%d}" == TODAY


def test_add_refuses_an_empty_item(db):
    with pytest.raises(ValueError):
        todo.add("   ")
    assert todo.read() == ""


def test_open_items_are_listed_in_order_then_the_done_ones(db):
    for item in ("One", "Two", "Three"):
        todo.add(item)
    todo.done("two")
    todo.add("Four")
    assert todo.read() == (f"# To-do\n\n- [ ] One (added {TODAY})\n- [ ] Three (added {TODAY})\n- [ ] Four (added {TODAY})\n"
                           f"\n## Done\n- [x] Two (done {TODAY})\n")


def test_a_list_with_nothing_open_says_so(db):
    todo.add("Old")
    todo.done("old")
    assert todo.read() == f"# To-do\n\nNothing open.\n\n## Done\n- [x] Old (done {TODAY})\n"


def test_done_ticks_off_exactly_one_match(db):
    for item in ("Gmail modify tool", "Memory upgrade", "Memory search"):
        todo.add(item)
    assert todo.done("gmail") == "Ticked off: Gmail modify tool"
    assert rows(db) == [(1, "Gmail modify tool", 1, 1), (2, "Memory upgrade", 0, 0), (3, "Memory search", 0, 0)]
    with pytest.raises(ValueError, match="2 open to-dos"):
        todo.done("memory")
    with pytest.raises(ValueError, match="No open to-do"):
        todo.done("gmail")  # already ticked off
    assert rows(db)[1:] == [(2, "Memory upgrade", 0, 0), (3, "Memory search", 0, 0)]


def test_the_table_sits_beside_the_tasks_without_disturbing_them(db):
    store = store_module.Store()
    task = store.create("research", "t", "i", None)
    todo.add("One")
    assert store_module.Store().get(task["id"])["title"] == "t" and todo.read().count("- [ ]") == 1


def test_todo_tools_are_registered_and_say_the_list_is_theirs_alone():
    assert {"todo_list", "todo_add", "todo_done"} <= {t.name for t in tools.all_tools()}
    assert "through the todo tools" in tools.get("todo_list").description
    assert "never into memory" in tools.get("todo_add").description
    assert "never into memory" in tools.get("remember").description and "todo" not in persona.persona()


# ── Repeats ─────────────────────────────────────────────────────


@pytest.fixture
def judge(db, monkeypatch):
    """The repeat check's model call, faked: set `judge.reply` (or an exception); `judge.asked` keeps what it saw."""
    from jarvis.llm import claude

    class Judge:
        reply: dict | Exception | None = {"same": None, "merged": ""}
        asked: list[dict] = []

    async def ask_json(prompt, schema, system_prompt, model, **options):
        Judge.asked.append(json.loads(prompt))
        assert "free of repeats" in system_prompt and options["timeout"] == todo.TODO_MATCH_SECONDS
        if isinstance(Judge.reply, Exception):
            raise Judge.reply
        return Judge.reply, 0.0

    Judge.asked = []
    monkeypatch.setattr(todo, "TODO_MATCH_ENABLED", True)
    monkeypatch.setattr(claude, "ask_json", ask_json)
    return Judge


def add_new(item: str, reopen: bool = False) -> str:
    return asyncio.run(todo.add_new(item, reopen))


def test_an_item_worded_the_same_is_not_added_twice_and_the_model_isnt_asked(judge, db):
    todo.add("Renew the domain.")
    assert add_new("renew  the DOMAIN") == "Not added: it's already on the to-do list as 'Renew the domain.'."
    assert rows(db) == [(1, "Renew the domain.", 0, 0)] and judge.asked == []


def test_a_done_item_worded_the_same_is_reopened_by_voice_and_left_for_suggestions(judge, db):
    todo.add("Renew the domain")
    todo.done("domain")
    assert add_new("Renew the domain") == f"Not added: it's already done (ticked off {TODAY}): 'Renew the domain'."
    assert rows(db) == [(1, "Renew the domain", 1, 1)]
    assert add_new("Renew the domain", reopen=True) == (f"Not added twice: it's already done (ticked off {TODAY}): "
                                                        "'Renew the domain', so it's back on the list.")
    assert rows(db) == [(1, "Renew the domain", 0, 0)]


def test_an_item_like_others_goes_to_the_model_with_its_likeliest_lookalikes(judge, db):
    for item in ("Paint the fence", "Plan the trip to the coast", "Book the train for the coast trip"):
        todo.add(item)
    todo.done("fence")
    said = add_new("Book train tickets for the coast trip")
    assert said == "Added to the to-do list: Book train tickets for the coast trip"
    assert judge.asked == [{"new": "Book train tickets for the coast trip",
                            "listed": [{"n": 1, "text": "Book the train for the coast trip", "state": "open"},
                                       {"n": 2, "text": "Plan the trip to the coast", "state": "open"}]}]
    judge.asked.clear()
    add_new("Water the plants")  # shares nothing: no call
    assert judge.asked == []


def test_a_repeat_the_model_finds_is_skipped_or_takes_in_the_new_detail(judge, db):
    todo.add("Second laptop support: run scheduled jobs there while the main computer is off")
    judge.reply = {"same": 1, "merged": ""}
    said = add_new("Second laptop support, still to build: scheduled jobs run there while the main computer is off")
    assert said == ("Not added: it's already on the to-do list as 'Second laptop support: run scheduled jobs there "
                    "while the main computer is off'.")
    merged = "Second laptop support: run scheduled jobs there while the main computer is off, and pair it by login only"
    judge.reply = {"same": 1, "merged": f" {merged} "}
    said = add_new("Second laptop support: scheduled jobs while the main computer is off, pairing by login only")
    assert said == ("Merged into the to-do already on the list, 'Second laptop support: run scheduled jobs there while "
                    f"the main computer is off', which now reads: {merged}")
    assert rows(db) == [(1, merged, 0, 0)]


def test_a_merge_that_changes_nothing_or_points_nowhere_is_ignored(judge, db):
    todo.add("Plan the trip to the coast")
    judge.reply = {"same": 1, "merged": "plan the trip to the coast."}
    assert add_new("Plan a coast trip") == "Not added: it's already on the to-do list as 'Plan the trip to the coast'."
    judge.reply = {"same": 7, "merged": "Anything"}
    assert add_new("Plan a coast trip") == "Added to the to-do list: Plan a coast trip"


def test_a_failed_model_call_adds_the_item(judge, db):
    todo.add("Plan the trip to the coast")
    judge.reply = TimeoutError()
    assert add_new("Plan a coast trip") == "Added to the to-do list: Plan a coast trip"
    judge.reply = None  # a reply that wasn't JSON
    assert add_new("Plan the coast trip") == "Added to the to-do list: Plan the coast trip"
    assert len(rows(db)) == 3


def test_with_the_check_off_only_the_same_wording_counts(judge, db, monkeypatch):
    monkeypatch.setattr(todo, "TODO_MATCH_ENABLED", False)
    todo.add("Plan the trip to the coast")
    assert add_new("Plan a coast trip") == "Added to the to-do list: Plan a coast trip"
    assert add_new("Plan a coast trip!").startswith("Not added") and judge.asked == []


def test_the_voice_tool_reopens_a_done_repeat_and_says_so(judge, db):
    todo.add("Plan the trip to the coast")
    todo.done("coast")
    judge.reply = {"same": 1, "merged": "Plan the trip to the coast, with a night by the sea"}
    said = asyncio.run(core.todo_add("Plan a coast trip with a night by the sea"))
    assert said == (f"Not added twice: it's already done (ticked off {TODAY}): 'Plan the trip to the coast', so it's "
                    "back on the list, now reading: Plan the trip to the coast, with a night by the sea")
    assert rows(db) == [(1, "Plan the trip to the coast, with a night by the sea", 0, 0)]
    assert "isn't added twice" in tools.get("todo_add").description
