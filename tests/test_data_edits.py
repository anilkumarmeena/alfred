"""Tests for tasks.data_edits: suggested edits to JARVIS's data files, checks, applying, dismissing, voice side."""

import asyncio
from datetime import date

import pytest
from test_in_place import NullReporter, env, worker_task  # noqa: F401  (env is a fixture)
from test_nudges import fake_run, settle

from conftest import use_task_cache
from jarvis import tools
from jarvis.brain import persona
from jarvis.core import profile
from jarvis.core.database import edits
from jarvis.state import memory, todo
from jarvis.tasks import announce, client, data_edits, projects, reporting, runner, worker
from jarvis.tasks.worker import Outcome
from jarvis.tools import tasks as task_tools

TODAY = f"{date.today():%Y-%m-%d}"


@pytest.fixture
def data(env, monkeypatch):
    """env plus a to-do list and a memory file of its own."""
    monkeypatch.setattr(projects, "ALLOWED_ROOT", env["tmp"])
    monkeypatch.setattr(projects, "NEW_PROJECTS_DIR", env["tmp"] / "projects")
    todo.add("Old")
    todo.done("Old")
    todo.add("Build the iPhone app")
    profile.current().memory_file.write_text("- Alex prefers short answers (2026-09-01)\n")
    return env


def suggests(*proposals: dict):
    """A run that suggests each edit, then finishes."""

    async def body(task, prompt, reporter) -> Outcome:
        for p in proposals:
            reporter.propose(task["id"], p)
        reporter.session(task["id"], "s1")
        return Outcome("Did it.\nDetails.", "", 0.01)

    return body


TODO = {"action": "todo_add", "item": "Try the new push self-test", "reason": "It needs a real phone."}
FACT = {"action": "remember", "fact": "Alex's iPhone app is called jarvis-app", "section": "environment",
        "reason": "Found while wiring push."}
FORGET = {"action": "forget", "text": "short answers", "reason": "Out of date."}


def run_task(monkeypatch, kind: str, project: str | None, *proposals: dict) -> tuple[runner.Runner, dict]:
    fake_run(monkeypatch, suggests(*proposals))

    async def main():
        r = runner.Runner()
        task = r.start(kind, "a thing", "Do it", project)
        await settle(r)
        return r, r.store.get(task["id"])

    return asyncio.run(main())


# ── Checking a suggestion ───────────────────────────────────────


@pytest.mark.parametrize("args, problem", [
    ({"action": "rename", "reason": "x"}, "action must be one of"),
    ({"action": "todo_add", "item": "X"}, "reason"),
    ({"action": "todo_add", "item": "  ", "reason": "x"}, "todo_add needs item"),
    ({"action": "todo_add", "item": "X", "fact": "Y", "reason": "x"}, "doesn't take fact"),
    ({"action": "project_register", "name": "p", "in_place": "yes", "reason": "x"}, "true or false"),
    ({"action": "project_register", "name": "p", "aliases": "q", "reason": "x"}, "list of text"),
    ({"action": "project_register", "name": "Video Agent", "reason": "x"}, "already the project video-agent"),
    ({"action": "edit", "file": ".env", "old": "A", "new": "B", "reason": "x"}, "file must be one of"),
    ({"action": "edit", "file": "task.md", "old": "- [ ] A", "new": "B", "reason": "x"}, "file must be one of"),  # to-dos are in tasks.db
    ({"action": "project_update", "name": "other", "reason": "x"}, "at least one of path, aliases, about or in_place"),
    ({"action": "project_update", "name": "nowhere", "about": "x", "reason": "x"}, "Unknown project"),
    ({"action": "project_update", "name": "other", "in_place": "no", "reason": "x"}, "true or false"),
    ({"action": "edit", "file": "memory.md", "old": "Nowhere", "new": "B", "reason": "x"}, "appears 0 times"),
    ({"action": "edit", "file": "memory.md", "old": "er", "new": "B", "reason": "x"}, "appears 2 times"),
    ({"action": "remember", "fact": "x" * 5000, "section": "user", "reason": "x"}, "too long"),
    ({"action": "remember", "fact": "Likes tea", "reason": "x"}, "remember needs section"),
    ({"action": "remember", "fact": "Likes tea", "section": "home", "reason": "x"}, "user or environment"),
    ({"action": "remember", "fact": "x" * 390, "section": "user", "reason": "x"}, "at most 400 characters"),
    ({"action": "edit", "file": "memory.md", "old": "short answers", "new": "x" * 400, "reason": "x"},
     "at most 400 characters"),
])
def test_unusable_suggestions_are_refused_with_what_to_fix(data, args, problem):
    with pytest.raises(ValueError, match=problem):
        data_edits.check(args)


def test_a_memory_suggestion_names_its_section_and_says_which():
    edit = data_edits.check(FACT | {"section": " Environment "})
    assert edit["section"] == "environment"
    assert data_edits.describe(edit) == \
        f"save to memory, under {memory.title('environment')}: Alex's iPhone app is called jarvis-app"


def test_an_edit_that_would_overfill_memory_is_refused_with_its_facts(data):
    profile.current().memory_file.write_text(f"## User\n- {'a' * 380} (2026-09-01)\n- Alex prefers short answers (2026-09-01)\n")
    grow = {"action": "edit", "file": "memory.md", "old": "- Alex prefers short answers (2026-09-01)\n",
            "new": "".join(f"- Fact {n} {'b' * 350}\n" for n in range(5)), "reason": "x"}
    with pytest.raises(ValueError, match=f"{memory.title('user')} is full") as e:
        data_edits.check(grow)
    assert "1. - aaaa" in str(e.value) and "2. - Alex prefers short answers (2026-09-01) [41]" in str(e.value)
    data_edits.check(grow | {"new": "- Alex prefers short answers and plain words (2026-10-01)\n"})  # within limits


def test_a_suggestion_is_cleaned_up_and_ignores_empty_fields():
    edit = data_edits.check({"action": "todo_add", "item": "Ship it", "reason": "  Because\n it's done. ", "fact": "", "aliases": []})
    assert edit == {"action": "todo_add", "item": "Ship it", "reason": "Because it's done."}


def test_an_edit_may_delete_text_with_an_empty_replacement(data):
    edit = data_edits.check({"action": "edit", "file": "memory.md", "old": "- Alex prefers short answers (2026-09-01)\n",
                             "new": "", "reason": "x"})
    assert data_edits.describe(edit) == "in memory.md, delete '- Alex prefers short answers (2026-09-01)'"


@pytest.mark.parametrize("args, kind, routine", [
    (TODO, "code_change", True),
    (FACT, "code_question", True),
    ({"action": "todo_done", "text": "iPhone", "reason": "x"}, "code_change", True),
    ({"action": "project_register", "name": "astro", "reason": "x"}, "code_change", True),
    ({"action": "edit", "file": "memory.md", "old": "(2026-09-01)\n", "new": "(2026-09-01)\n- New fact\n", "reason": "x"},
     "code_change", True),
    (FORGET, "code_change", False),
    ({"action": "edit", "file": "memory.md", "old": "short answers", "new": "long answers", "reason": "x"}, "code_change", False),
    ({"action": "project_update", "name": "other", "in_place": True, "reason": "x"}, "code_change", False),
    ({"action": "project_update", "name": "other", "aliases": ["another"], "reason": "x"}, "code_change", False),
    (TODO, "research", False),  # it read mail and web pages, which might be steering it
])
def test_routine_edits_need_no_yes_and_riskier_ones_do(data, args, kind, routine):
    assert (data_edits.needs_yes(data_edits.check(args), kind) is None) is routine


# ── On the task ─────────────────────────────────────────────────


def test_suggested_edits_are_kept_numbered_and_announced(data, monkeypatch):
    r, task = run_task(monkeypatch, "code_change", "video-agent", TODO, FACT, FORGET)
    kept = edits(task)
    assert [(e["n"], e["action"], e["status"]) for e in kept] == [(1, "todo_add", "pending"), (2, "remember", "pending"),
                                                                  (3, "forget", "pending")]
    assert "needs_yes" not in kept[0] and "long-term memory" in kept[2]["needs_yes"]
    assert task["spoken"] == "Your task, a thing, is done. Did it. It has 3 suggestions for your lists and my memory."
    assert [e["text"] for e in r.store.history(1) if e["kind"] == "edit"][0] == \
        "suggested edit 1: add a to-do: Try the new push self-test. It needs a real phone."
    assert todo.read().count("- [ ]") == 1  # nothing applied yet

    note = announce.context(task)  # for the brain alone, by number
    assert note.startswith('The update on "a thing" was task 1. Its suggested edits, pending: 1. add a to-do: Try the new push '
                           "self-test; 2. save to memory")
    assert "3. delete the memory lines containing 'short answers' (needs a yes: it deletes lines" in note


def test_one_edit_is_announced_as_such_and_none_adds_nothing(data, monkeypatch):
    _, task = run_task(monkeypatch, "code_change", "video-agent", TODO)
    assert task["spoken"].endswith("Did it. It'd also like to add a to-do: Try the new push self-test.")
    _, task = run_task(monkeypatch, "code_change", "video-agent")
    assert task["spoken"] == "Your task, a thing, is done. Did it." and announce.context(task) == 'The update on "a thing" was task 2.'


def test_a_task_with_a_question_mentions_its_edits_only_once_finished(data, monkeypatch):
    async def asks(task, prompt, reporter) -> Outcome:
        reporter.propose(task["id"], TODO)
        reporter.ask(task["id"], "Which phone?")
        return Outcome("Asked.", "", 0.01)

    fake_run(monkeypatch, asks, suggests())

    async def main():
        r = runner.Runner()
        r.start("code_change", "a thing", "Do it", "video-agent")
        await settle(r)
        waiting = r.store.get(1)
        r.reply(1, "the iPhone")
        await settle(r)
        return waiting, r.store.get(1)

    waiting, done = asyncio.run(main())
    assert waiting["spoken"] == "Your task, a thing, has a question: Which phone?"
    assert "add a to-do" not in waiting["spoken"] + announce.context(waiting)
    assert done["spoken"].endswith("It'd also like to add a to-do: Try the new push self-test.")
    assert "1. add a to-do" in announce.context(done)


def test_a_task_can_suggest_only_so_many_edits(data, monkeypatch):
    monkeypatch.setattr(data_edits, "MAX_EDITS", 2)
    r = runner.Runner()
    task = r.store.create("code_change", "t", "i", "video-agent")
    r.propose(task["id"], TODO)
    r.propose(task["id"], FACT)
    with pytest.raises(ValueError, match="at most 2"):
        r.propose(task["id"], FACT)


# ── Applying and dismissing ─────────────────────────────────────


def test_applying_and_dismissing_by_number(data, monkeypatch):
    r, _ = run_task(monkeypatch, "code_change", "video-agent", TODO, FACT, FORGET)

    async def main():
        applied = await r.edits(1, "apply", [1, 2])
        dismissed = await r.edits(1, "dismiss", [3])
        again = await r.edits(1, "apply", [1, 3])
        return applied, dismissed, again

    applied, dismissed, again = asyncio.run(main())
    saved = f"- Alex's iPhone app is called jarvis-app ({TODAY})"
    assert applied["outcome"] == ["edit 1 applied: Added to the to-do list: Try the new push self-test",
                                  f"edit 2 applied: Saved to {memory.title('environment')} ({len(saved)} of 3,000)."]
    assert f"- [ ] Build the iPhone app (added {TODAY})\n- [ ] Try the new push self-test (added {TODAY})\n" in todo.read()
    assert memory.facts() == {"user": ["- Alex prefers short answers (2026-09-01)"], "environment": [saved]}
    assert dismissed["outcome"] == ["edit 3 dismissed: dismissed"]
    assert again["outcome"] == ["edit 1 was already applied", "edit 3 was already dismissed"]
    assert [e["status"] for e in edits(r.store.get(1))] == ["applied", "applied", "dismissed"]
    assert data_edits.pending(r.store.get(1)) == [] and announce.suggestions(r.store.get(1)) == []
    assert [e["kind"] for e in r.store.history(1)][-3:] == ["edit applied", "edit applied", "edit dismissed"]


def test_an_edit_that_fails_is_marked_and_the_rest_still_apply(data, monkeypatch):
    missing = {"action": "todo_done", "text": "no such thing", "reason": "x"}
    r, _ = run_task(monkeypatch, "code_change", "video-agent", missing, TODO)
    out = asyncio.run(r.edits(1, "apply", [1, 2]))
    assert out["outcome"][0].startswith("edit 1 failed: No open to-do matches 'no such thing'")
    assert out["outcome"][1].startswith("edit 2 applied")
    assert [e["status"] for e in edits(out)] == ["failed", "applied"]


@pytest.mark.parametrize("numbers", [[], [4]])
def test_unknown_edit_numbers_are_refused(data, monkeypatch, numbers):
    r, _ = run_task(monkeypatch, "code_change", "video-agent", TODO)
    with pytest.raises(ValueError, match="suggested edits are 1; say which"):
        asyncio.run(r.edits(1, "apply", numbers))
    with pytest.raises(ValueError, match="apply or dismiss"):
        asyncio.run(r.edits(1, "undo", [1]))


def test_a_project_suggestion_registers_it(data, monkeypatch):
    add = {"action": "project_register", "name": "astro agent", "aliases": ["astro"], "about": "Personal.", "reason": "x"}
    r, _ = run_task(monkeypatch, "code_change", "video-agent", add)
    out = asyncio.run(r.edits(1, "apply", [1]))
    assert out["outcome"] == [f"edit 1 applied: Registered astro agent at {data['tmp'] / 'projects' / 'astro-agent'}, in place."]
    assert projects.find("astro").path == data["tmp"] / "projects" / "astro-agent"


def test_a_project_update_changes_only_what_it_names(data, monkeypatch):
    change = {"action": "project_update", "name": "Other", "aliases": ["another"], "in_place": True,
              "reason": "Whisper hears 'another'."}
    r, task = run_task(monkeypatch, "code_change", "video-agent", change)
    kept = edits(task)[0]
    assert kept["name"] == "other" and "project list's settings" in kept["needs_yes"]
    assert data_edits.describe(kept) == "set the project other's aliases to another, in_place to true"
    out = asyncio.run(r.edits(1, "apply", [1]))
    assert out["outcome"] == [f"edit 1 applied: Updated other: at {data['other']}, in place."]
    found = projects.find("another")
    assert (found.name, found.aliases, found.in_place, found.path) == ("other", ["another"], True, data["other"])


def test_a_fact_for_a_full_section_fails_on_applying_with_its_facts(data, monkeypatch):
    r, _ = run_task(monkeypatch, "code_change", "video-agent", FACT)
    profile.current().memory_file.write_text("## Environment\n" + "".join(f"- Setup {n} {'c' * 370} (2026-09-01)\n" for n in range(7))
                                  + f"- {'d' * 220} (2026-09-01)\n")  # 2,986 of 3,000
    before = profile.current().memory_file.read_text()
    out = asyncio.run(r.edits(1, "apply", [1]))
    assert out["outcome"][0].startswith(f"edit 1 failed: {memory.title('environment')} is full: this would make it "
                                        "3,039 of 3,000 characters. Its facts now (2,986 of 3,000")
    assert f"8. - {'d' * 220} (2026-09-01) [235]\nMerge, shorten or drop" in out["outcome"][0]  # listed in full
    assert edits(out)[0]["status"] == "failed" and profile.current().memory_file.read_text() == before


def test_an_old_remember_from_before_sections_fails_saying_what_it_needs(data, monkeypatch):
    r, task = run_task(monkeypatch, "code_change", "video-agent")
    r.store.add_edit(task["id"], {"action": "remember", "fact": "Likes tea", "reason": "suggested before sections"})
    assert data_edits.describe(edits(r.store.get(task["id"]))[0]) == "save to memory: Likes tea"
    out = asyncio.run(r.edits(task["id"], "apply", [1]))
    assert out["outcome"] == ["edit 1 failed: section must be user or environment."]


def test_an_old_edit_to_a_file_no_longer_editable_fails_saying_which_is(data, monkeypatch):
    r, task = run_task(monkeypatch, "code_change", "video-agent")
    r.store.add_edit(task["id"], {"action": "edit", "file": "task.md", "old": "- [ ] A\n", "new": "- [x] A\n",
                                  "reason": "suggested before the to-do list moved"})
    out = asyncio.run(r.edits(task["id"], "apply", [1]))
    assert out["outcome"] == ["edit 1 failed: task.md isn't a data file; the only one is memory.md."]


def test_an_edit_whose_text_changed_since_is_not_applied(data, monkeypatch):
    change = {"action": "edit", "file": "memory.md", "old": "- Alex prefers short answers (2026-09-01)\n", "new": "", "reason": "x"}
    r, _ = run_task(monkeypatch, "code_change", "video-agent", change)
    profile.current().memory_file.write_text("- Alex prefers long answers\n")
    out = asyncio.run(r.edits(1, "apply", [1]))
    assert out["outcome"][0].startswith("edit 1 failed: The old text appears 0 times in memory.md")
    assert profile.current().memory_file.read_text() == "- Alex prefers long answers\n"


# ── The voice side ──────────────────────────────────────────────


def test_task_list_shows_pending_edits_and_task_detail_all_of_them(data, monkeypatch):
    r, _ = run_task(monkeypatch, "code_change", "video-agent", TODO, FORGET)

    async def fake_edits(task_id, action, numbers):
        return await r.edits(task_id, action, numbers)

    monkeypatch.setattr(client, "edits", fake_edits)
    out = asyncio.run(task_tools.task_edits(1, "apply", [1]))
    assert out["outcome"] == ["edit 1 applied: Added to the to-do list: Try the new push self-test"]
    assert out["still_pending"] == [{"n": 2, "change": "delete the memory lines containing 'short answers'",
                                     "reason": "Out of date.", "needs_yes": "it deletes lines from long-term memory"}]
    task = r.store.get(1)
    assert [e["n"] for e in task_tools._brief(task)["suggested_edits"]] == [2]

    async def fake_task(task_id):
        return r.store.get(task_id) | {"log": r.store.history(task_id)}

    async def no_mark(ids):
        pass

    async def no_approvals():
        return []

    monkeypatch.setattr(client, "task", fake_task)
    monkeypatch.setattr(client, "mark_seen", no_mark)
    monkeypatch.setattr(client, "approvals", no_approvals)
    detail = asyncio.run(task_tools.task_detail(1))
    assert [(e["n"], e.get("status")) for e in detail["suggested_edits"]] == [(1, "applied"), (2, None)]
    assert detail["suggested_edits"][0]["result"] == "Added to the to-do list: Try the new push self-test"


def test_the_edit_tool_is_the_voice_brains_alone():
    names = {t.name for t in tools.all_tools()}
    assert "task_edits" in names and "task_edits" not in worker.READ_ONLY_TOOLS


def test_the_task_tool_says_what_it_noted_or_what_to_fix(data, monkeypatch):
    monkeypatch.setattr(reporting, "create_sdk_mcp_server", lambda name, tools: {t.name: t.handler for t in tools})
    r = runner.Runner()
    task = r.store.create("code_change", "t", "i", "video-agent")
    propose = reporting._task_server(task["id"], r)["propose_data_edit"]
    ok = asyncio.run(propose(TODO))
    assert "is_error" not in ok and ok["content"][0]["text"].startswith("Suggested as edit 1: add a to-do: Try the new")
    bad = asyncio.run(propose({"action": "todo_add", "reason": "x"}))
    assert bad["is_error"] and bad["content"][0]["text"] == "Not suggested: todo_add needs item."
    assert len(edits(r.store.get(task["id"]))) == 1


def test_every_task_kind_can_suggest_edits(data, monkeypatch):
    use_task_cache(monkeypatch, data["tmp"] / "cache")
    for task in (worker_task(), worker_task(in_place=1), worker_task(kind="code_question"),
                 worker_task(kind="research", project=None)):
        opts = worker._options(task, NullReporter())
        assert "mcp__task__propose_data_edit" in opts.allowed_tools
        prompt = opts.system_prompt if isinstance(opts.system_prompt, str) else opts.system_prompt["append"]
        assert "propose_data_edit" in prompt and f"rather than leaving {profile.name()} edits to make by hand" in prompt


def test_task_edits_lets_jarvis_make_small_edits_and_ask_about_bigger_ones():
    edits_rules = tools.get("task_edits").description
    assert "needs_yes" in edits_rules and "Apply small, low-risk ones without asking" in edits_rules
    assert "Ask first" in edits_rules and "prompted by an email or web page" in edits_rules
    assert "suggested edits" in persona.persona()  # the persona's untrusted-content rule covers them
    description = tools.get("project_register").description
    assert "Only after the user says yes" not in description and "routine" in description
    assert "go ahead" in description and "wait for a yes" not in description
