"""Tests for settling to-dos as tasks finish (jarvis.tasks.goal, runner): tick off, reword, or suggest ticking off."""

import asyncio

import pytest
from test_approvals import drained, events, pushes  # noqa: F401  (pushes is a fixture)
from test_goal import checks, statuses, verdict  # noqa: F401  (checks is a fixture)
from test_in_place import env  # noqa: F401  (env is a fixture)
from test_nudges import fake_run, finishes, settle

from jarvis import tools
from jarvis.core import profile
from jarvis.core.database import edits
from jarvis.state import todo
from jarvis.tasks import announce, briefs, data_edits, goal, runner
from jarvis.tasks.goal import BLOCKED, COMPLETE, Check
from jarvis.tools import tasks as task_tools


@pytest.fixture
def listed(env):
    """Task database with to-dos 1 and 2 open, 3 done."""
    for item in ("Load MCP connection tools only when needed", "Build the iPhone app", "Old thing"):
        todo.add(item)
    todo.done("Old thing")
    return env


def run(*replies, checks, todo_item: str = "", then=None, job=None):
    """Start a research task (for `todo_item`, if given) with checks answering `replies`; let it settle.

    Returns (runner, task, announced). `then(r, task)` runs once it settles, then it settles again.
    """
    checks.replies = list(replies)

    async def main():
        r = runner.Runner()
        q = events(r)
        task = r.start("research", "mcp tools", "Load MCP tools lazily", job=job, todo=todo_item or None)
        await settle(r)
        if then:
            await then(r, task)
            await settle(r)
        await asyncio.sleep(0.02)
        return r, r.store.get(task["id"]), drained(q)

    return asyncio.run(main())


def is_open(item_id: int) -> bool:
    return not todo.get(item_id)["done"]


# ── The to-do list ──────────────────────────────────────────────


def test_find_prefers_the_item_worded_exactly_and_takes_a_listed_line(env):
    todo.add("Fix the build")
    todo.add("Fix the build on CI")
    assert todo.find("fix the build")["id"] == 1  # also inside the second, but this one matches exactly
    assert todo.find("- [ ] Fix the build on CI (added 2026-10-09)")["id"] == 2
    with pytest.raises(ValueError, match="2 open to-dos"):
        todo.find("fix")
    with pytest.raises(ValueError, match="No open to-do"):
        todo.find("deploy")


def test_reword_changes_an_open_items_wording(env):
    todo.add("Build the iPhone app and its widget")
    assert todo.reword("iphone", "Build the iPhone widget") == \
        "Reworded the to-do 'Build the iPhone app and its widget' as: Build the iPhone widget"
    assert todo.get(1)["text"] == "Build the iPhone widget" and todo.get(1)["done"] is False


def test_a_reword_is_a_suggested_edit_put_to_the_user(listed):
    edit = data_edits.check({"action": "todo_reword", "text": "iPhone app", "item": "Ship the iPhone app", "reason": "x"})
    assert data_edits.describe(edit) == "reword the to-do matching 'iPhone app' as: Ship the iPhone app"
    assert data_edits.needs_yes(edit, "code_change") == "it rewords an item on the to-do list"
    asyncio.run(data_edits.apply(edit))
    assert todo.get(2)["text"] == "Ship the iPhone app"
    assert "todo_reword" in data_edits.ACTIONS


# ── The check ───────────────────────────────────────────────────


def test_the_checks_word_on_to_dos_is_read_apart_from_what_to_finish():
    check = goal.parse(verdict(BLOCKED, "signing needs Xcode.", "install Xcode",
                               todos={4: " Sign the iPhone app ", 7: ""}))
    assert check.details == "- install Xcode"
    assert check.todos == {4: "Sign the iPhone app", 7: ""}
    assert goal.parse(verdict(COMPLETE, "done.")).todos == {}


def test_the_check_reads_the_item_a_task_was_started_for_or_else_the_open_ones(listed):
    base = {"id": 5, "title": "t", "kind": "research", "project": None, "instruction": "i", "job": None, "fixes": None}
    linked = base | {"todo": 2}
    text = goal.material(linked, "Did it.", [], "", 0, goal.todos(linked))
    assert "<todo>\n2. Build the iPhone app\n</todo>" in text and "open_todos" not in text
    text = goal.material(base, "Did it.", [], "", 0, goal.todos(base))
    assert "<open_todos>\n1. Load MCP connection tools only when needed\n2. Build the iPhone app\n</open_todos>" in text
    assert goal.todos(base | {"job": 3}) == [] and goal.todos(base | {"fixes": 4}) == []  # scheduled runs and fixes
    todo.done("iPhone")
    assert goal.todos(linked) == []  # ticked off meanwhile


# ── In the runner ───────────────────────────────────────────────


def test_a_task_for_a_to_do_ticks_it_off_once_it_is_checked_finished(listed, pushes, checks):
    r, task, said = run(verdict(COMPLETE, "it loads them lazily now.", todos={1: ""}), checks=checks,
                        todo_item="MCP connection")
    assert task["todo"] == 1 and not is_open(1) and is_open(2)
    assert statuses(said) == ['Your task, mcp tools, is done. Did it. I\'ve ticked off your to-do "Load MCP connection '
                              'tools only when needed".']
    assert [(e["action"], e["status"]) for e in edits(task)] == [("todo_done", data_edits.APPLIED)]
    assert "<todo>\n1. Load MCP connection tools only when needed\n</todo>" in checks.asked[0]
    assert any("works on the to-do: Load MCP" in e["text"] for e in r.store.history(task["id"]))


def test_a_complete_task_whose_check_says_nothing_of_its_to_do_ticks_it_off(listed, pushes, checks):
    _, task, _ = run(verdict(COMPLETE, "done."), checks=checks, todo_item="MCP connection")
    assert not is_open(1)


def test_a_part_done_to_do_gets_a_rewording_put_to_the_user(listed, pushes, checks):
    _, task, said = run(verdict(BLOCKED, "the iPhone half needs full Xcode.",
                                todos={1: "Load the iPhone's MCP tools only when needed"}),
                        checks=checks, todo_item="MCP connection")
    assert is_open(1) and todo.get(1)["text"] == "Load MCP connection tools only when needed"
    assert statuses(said)[0].endswith("It'd also like to reword the to-do matching 'Load MCP connection tools only when "
                                      "needed' as: Load the iPhone's MCP tools only when needed.")
    (edit,) = data_edits.pending(task)
    assert edit["action"] == "todo_reword" and edit["needs_yes"]
    assert "1. reword the to-do" in announce.context(task)


def test_no_headway_on_the_to_do_leaves_it_as_it_is(listed, pushes, checks):
    unchanged = {1: "Load MCP connection tools only when needed"}
    _, task, said = run(verdict(BLOCKED, "it needs your Apple ID.", todos=unchanged), checks=checks,
                        todo_item="MCP connection")
    assert is_open(1) and edits(task) == [] and "to-do" not in statuses(said)[0]


def test_an_unchecked_task_puts_ticking_its_to_do_off_to_the_user(listed, pushes, checks, monkeypatch):
    monkeypatch.setattr(goal, "GOAL_LOOP", False)
    _, task, said = run(checks=checks, todo_item="MCP connection")
    assert is_open(1) and checks.asked == []
    assert statuses(said)[0].endswith("It'd also like to tick off the to-do matching 'Load MCP connection tools only "
                                      "when needed'.")
    (edit,) = data_edits.pending(task)
    assert edit["action"] == "todo_done" and "couldn't be checked" in edit["needs_yes"]


def test_an_open_item_a_task_finishes_unasked_is_put_to_the_user(listed, pushes, checks):
    _, task, said = run(verdict(COMPLETE, "done.", todos={2: "", 3: "", 1: "something"}), checks=checks)
    assert "<open_todos>" in checks.asked[0]
    assert is_open(1) and is_open(2)  # nothing changes without their yes
    assert statuses(said)[0].endswith("It'd also like to tick off the to-do matching 'Build the iPhone "
                                      "app'.")  # 3 is done already, and a LEFT changes nothing it wasn't for
    (edit,) = data_edits.pending(task)
    assert edit["text"] == "Build the iPhone app" and "wasn't started for it" in edit["needs_yes"]


def test_a_later_run_that_finishes_it_replaces_the_rewording(listed, pushes, checks, monkeypatch):
    fake_run(monkeypatch, finishes("Half of it."), finishes("All of it."))

    async def carry_on(r, task):
        r.reply(task["id"], "Do the iPhone half too")

    _, task, said = run(verdict(BLOCKED, "half.", todos={1: "The iPhone half"}),
                        verdict(COMPLETE, "all.", todos={1: ""}), checks=checks, todo_item="MCP connection",
                        then=carry_on)
    assert not is_open(1)
    assert [(e["action"], e["status"]) for e in edits(task)] == [("todo_reword", data_edits.DISMISSED),
                                                                 ("todo_done", data_edits.APPLIED)]
    assert statuses(said)[-1].endswith('ticked off your to-do "Load MCP connection tools only when needed".')


def test_a_to_do_that_is_not_open_is_refused_and_no_task_starts(listed):
    r = runner.Runner()
    with pytest.raises(ValueError, match="No open to-do matches 'Old thing'"):
        r.start("research", "x", "Do it", todo="Old thing")
    assert r.store.recent() == []


# ── What the task and the voice brain see ───────────────────────


def test_the_task_hears_jarvis_settles_its_to_do(listed):
    assert briefs._todo_brief({"todo": 2}) == (
        f"\n- This task works on {profile.name()}'s to-do item \"Build the iPhone app\". JARVIS ticks it off, or "
        "rewords it to what's left, when you finish, so don't suggest todo_done or todo_reword for it.")
    assert briefs._todo_brief({"todo": None}) == briefs._todo_brief({}) == ""


def test_task_start_takes_the_to_do_and_task_views_show_it(listed):
    start = tools.get("task_start")
    assert "todo_list shows it" in start.properties["todo"]["description"] and "to-do" in start.description
    view = task_tools._brief({"id": 1, "title": "t", "kind": "research", "status": "done", "updated": 0, "todo": 3})
    assert view["todo"] == "Old thing (ticked off)"


def test_the_check_is_told_to_judge_to_dos_by_meaning():
    assert '"todos"' in goal.system() and "not by\nshared words" in goal.system()
    assert goal.SCHEMA["properties"]["todos"]["items"]["required"] == ["id", "done"]


def test_what_a_check_changes_needs_its_task_to_have_been_for_the_item(listed):
    task = {"id": 1, "todo": None, "job": None, "fixes": None}
    assert goal.todo_changes(task, None) == []  # unchecked and not started for one: nothing
    changes = goal.todo_changes(task | {"todo": 2}, Check(COMPLETE, "done", "", 0))
    assert [(c.edit["action"], c.at_once) for c in changes] == [("todo_done", True)]
    assert goal.todo_changes(task | {"todo": 2}, Check(BLOCKED, "half", "", 0)) == []  # no word on it, not complete
