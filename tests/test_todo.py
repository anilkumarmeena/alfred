"""Tests for jarvis.state.todo: the to-do list in data/tasks.db."""

import sqlite3
from datetime import date, datetime

import pytest

from conftest import use_profile
from jarvis import tools
from jarvis.brain import persona
from jarvis.core import database
from jarvis.state import todo
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
    store = database.Store()
    task = store.create("research", "t", "i", None)
    todo.add("One")
    assert database.Store().get(task["id"])["title"] == "t" and todo.read().count("- [ ]") == 1


def test_todo_tools_are_registered_and_say_the_list_is_theirs_alone():
    assert {"todo_list", "todo_add", "todo_done"} <= {t.name for t in tools.all_tools()}
    assert "through the todo tools" in tools.get("todo_list").description
    assert "never into memory" in tools.get("todo_add").description
    assert "never into memory" in tools.get("remember").description and "todo" not in persona.persona()
