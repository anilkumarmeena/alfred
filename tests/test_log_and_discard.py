"""Tests for task_log (live log tail) and task_discard (throw a task's work away unmerged)."""

import asyncio
import json
import time
from pathlib import Path

import pytest
from test_in_place import (  # noqa: F401  (env is a fixture)
    add_new_file,
    branches,
    env,
    fake_run,
    git,
    run_worktree_task,
    settle,
)
from test_nudges import until

from jarvis import tools
from jarvis.brain import persona
from jarvis.safety import consent
from jarvis.state import conversation
from jarvis.tasks import api, client, runner
from jarvis.tasks.worker import READ_ONLY_TOOLS
from jarvis.tools import tasks as task_tools

# ── The log tail ────────────────────────────────────────────────


def working_task() -> tuple[runner.Runner, dict]:
    """A runner with one task that has taken steps, reported progress and used a subagent."""

    async def main():
        r = runner.Runner()
        task = r.store.create("code_change", "retry logic", "Add retries", "other")
        r.activity(task["id"], "reading app.py")
        r.activity(task["id"], "reading app.py")  # repeated step
        r.subagent(task["id"], "code searcher started: find the retry logic")
        r.activity(task["id"], "code searcher: searching the code")
        r.subagent(task["id"], "code searcher finished")
        r.activity(task["id"], "running pytest -q")
        r.progress(task["id"], "Tests pass.")
        return r, task

    return asyncio.run(main())


def test_each_step_goes_in_the_log_once_but_the_detail_log_leaves_steps_out(env):
    r, task = working_task()
    tail = [f"{e['kind']}: {e['text']}" for e in r.store.history(task["id"], steps=True)]
    assert tail == [
        "step: reading app.py",
        "subagent: code searcher started: find the retry logic",
        "step: code searcher: searching the code",
        "subagent: code searcher finished",
        "step: running pytest -q",
        "progress: Tests pass.",
    ]
    assert [e["kind"] for e in r.store.history(task["id"])] == ["subagent", "subagent", "progress"]
    assert [e["text"] for e in r.store.history(task["id"], 2, steps=True)] == ["running pytest -q", "Tests pass."]
    assert r.store.get(task["id"])["activity"] == "Tests pass."


class Request:
    def __init__(self, task_id: int, **query: str) -> None:
        self.match_info, self.query = {"id": str(task_id)}, query


def tail_route(r: runner.Runner):
    return next(d.handler for d in api.routes(r) if d.method == "GET" and d.path.endswith("/log"))


@pytest.mark.parametrize("asked, shown", [({}, api.LOG_LINES), ({"lines": "3"}, 3), ({"lines": "500"}, api.MAX_LOG_LINES),
                                          ({"lines": "0"}, 1)])
def test_the_runner_returns_the_latest_lines_with_every_step(env, asked, shown):
    async def main():
        r = runner.Runner()
        task = r.store.create("research", "prices", "Look up prices", None)
        for n in range(60):
            r.activity(task["id"], f"searching the web for thing {n}")
        return json.loads((await tail_route(r)(Request(task["id"], **asked))).text)

    got = asyncio.run(main())
    assert got["title"] == "prices" and len(got["log"]) == shown
    assert got["log"][-1]["text"] == "searching the web for thing 59"


def test_task_log_says_how_long_ago_each_line_was_and_keeps_lines_short(monkeypatch):
    now = time.time()
    asked = {}

    async def log(task_id, lines=None):
        asked.update(id=task_id, lines=lines)
        return {"id": task_id, "title": "retry logic", "kind": "code_change", "status": "running", "updated": now,
                "activity": "running pytest -q", "seen": 1, "log": [
                    {"at": now - 3 * 3600, "kind": "start", "text": "Add retries\n\n" + "and more " * 50},
                    {"at": now - 5 * 60, "kind": "subagent", "text": "test runner started: run the tests"},
                    {"at": now - 5, "kind": "step", "text": "test runner: running pytest -q"},
                ]}

    async def mark_seen(ids):
        pass

    monkeypatch.setattr(client, "log", log)
    monkeypatch.setattr(client, "mark_seen", mark_seen)
    out = asyncio.run(task_tools.task_log(7))
    assert asked == {"id": 7, "lines": None}
    assert out["activity"] == "running pytest -q" and out["status"] == "running"
    assert out["log"][1:] == ["5m ago, subagent: test runner started: run the tests",
                              "just now, step: test runner: running pytest -q"]
    assert out["log"][0].startswith("3h ago, start: Add retries and more") and len(out["log"][0]) < 200
    asyncio.run(task_tools.task_log(7, lines=20))
    assert asked["lines"] == 20


# ── Discarding ──────────────────────────────────────────────────


def discard(r: runner.Runner, task_id: int) -> dict:
    return asyncio.run(r.discard(task_id))


def test_discard_removes_the_worktree_and_deletes_the_unmerged_branch(env, monkeypatch):
    other = env["other"]
    r, task = run_worktree_task(monkeypatch, add_new_file)
    head = git(other, "rev-parse", "HEAD")
    (Path(task["worktree"]) / "later.py").write_text("y = 2\n")  # uncommitted changes go too

    done = discard(r, task["id"])
    assert done["status"] == "discarded" and done["seen"] == 1
    assert "discarded without merging" in done["spoken"] and "branch deleted" in done["spoken"]
    assert branches(other) == ["main"] and git(other, "rev-parse", "HEAD") == head
    assert not Path(task["worktree"]).exists() and not (other / "new.py").exists()
    assert any(e["kind"] == "discarded" for e in r.store.history(task["id"]))
    with pytest.raises(ValueError, match="already discarded"):
        discard(r, task["id"])
    with pytest.raises(ValueError, match="was discarded"):
        r.reply(task["id"], "one more thing")
    with pytest.raises(ValueError, match="nothing to merge"):
        asyncio.run(r.merge(task["id"]))


def test_discard_of_a_branch_with_no_commits_just_cleans_up(env, monkeypatch):
    r, task = run_worktree_task(monkeypatch, None)
    assert discard(r, task["id"])["status"] == "discarded"
    assert branches(env["other"]) == ["main"] and not Path(task["worktree"]).exists()


def test_discard_refuses_a_branch_that_is_already_merged(env, monkeypatch):
    other = env["other"]
    r, task = run_worktree_task(monkeypatch, add_new_file)
    git(other, "merge", "-q", "--no-edit", task["branch"])
    with pytest.raises(ValueError, match="already merged"):
        discard(r, task["id"])
    assert task["branch"] in branches(other) and r.store.get(task["id"])["status"] == "done"


def test_discard_only_deletes_the_branch_jarvis_made_for_the_task(env, monkeypatch):
    r, task = run_worktree_task(monkeypatch, add_new_file)
    r.store.set(task["id"], branch="main")
    with pytest.raises(ValueError, match="wasn't made by JARVIS"):
        discard(r, task["id"])
    assert "main" in branches(env["other"]) and Path(task["worktree"]).exists()


def test_discard_leaves_an_in_place_task_s_changes_in_the_folder(env, monkeypatch):
    video = env["video"]
    r, task = run_worktree_task(monkeypatch, add_new_file, project="video-agent")
    done = discard(r, task["id"])
    assert done["status"] == "discarded"
    assert (video / "new.py").read_text() == "x = 1\n" and branches(video) == ["main"]
    assert "still there" in done["spoken"] and "undo them by hand" in done["spoken"]
    assert "untracked" in done["spoken"]  # the changes, as recorded when it ended


def test_discard_of_an_in_place_task_that_changed_nothing(env, monkeypatch):
    r, task = run_worktree_task(monkeypatch, None, project="video-agent")
    assert "left no changes in video-agent's folder" in discard(r, task["id"])["spoken"]


def test_discard_of_a_research_task_only_marks_it(env, monkeypatch):
    fake_run(monkeypatch)

    async def main():
        r = runner.Runner()
        task = r.start("research", "prices", "Look up prices")
        await settle(r)
        return r, await r.discard(task["id"])

    r, done = asyncio.run(main())
    assert done["status"] == "discarded" and "nothing to remove" in done["spoken"]


def test_discard_refuses_running_queued_and_waiting_tasks(env, monkeypatch):
    gate = asyncio.Event()
    seen = fake_run(monkeypatch, add_new_file, gate=gate)

    async def main():
        r = runner.Runner()
        task = r.start("code_change", "add a thing", "Add new.py", "other")
        await until(lambda: seen["running"])
        with pytest.raises(ValueError, match="stop it first"):
            await r.discard(task["id"])
        r.stop(task["id"])
        await settle(r)
        return r, task, await r.discard(task["id"])

    r, task, done = asyncio.run(main())
    assert done["status"] == "discarded" and branches(env["other"]) == ["main"]

    waiting = r.store.create("research", "prices", "Look up prices", None)
    r.store.set(waiting["id"], status="waiting", question="Which shop?")
    with pytest.raises(ValueError, match="is waiting; stop it first"):
        discard(r, waiting["id"])


# ── The voice side ──────────────────────────────────────────────


def test_both_tools_are_on_the_voice_side_only():
    names = {t.name for t in tools.all_tools()}
    assert {"task_log", "task_discard"} <= names
    assert "task_log" not in READ_ONLY_TOOLS and "task_discard" not in READ_ONLY_TOOLS


def test_discarding_asks_first_and_task_log_says_the_latest_steps():
    discard = tools.get("task_discard").description
    assert "can't be undone" in discard and "wait for a yes" in discard
    assert "discarding a task's work" in persona.persona()  # one of the persona's few confirmations
    assert "latest steps" in tools.get("task_log").description


def discarding(monkeypatch, said: str, model_says: str = "unclear") -> tuple[list[int], list[str]]:
    """task_discard on the user's reply `said`: (tasks discarded, prompts the yes/no model saw)."""
    gone, prompts = [], []

    async def fake_discard(task_id: int) -> dict:
        gone.append(task_id)
        return {"id": task_id, "title": "the Chrome fix", "kind": "code_change", "status": "discarded",
                "updated": time.time()}

    async def model(prompt: str) -> str:
        prompts.append(prompt)
        return json.dumps({"answer": model_says, "scope": "once"})

    monkeypatch.setattr(client, "discard", fake_discard)
    monkeypatch.setattr(consent, "CONSENT_ENABLED", True)
    monkeypatch.setattr(consent, "ask_model", model)
    monkeypatch.setattr(consent, "_kept", {})
    conversation.current().heard.turn(said)
    try:
        asyncio.run(task_tools.task_discard(7))
    except ValueError as e:
        assert "Not discarded" in str(e) and "Check back" in str(e)
    return gone, prompts


@pytest.mark.parametrize("said", ["Yes.", "Go ahead.", "Okay, sure.", "Yes, delete them.", "Yeah, all three."])
def test_a_plain_yes_discards_at_once_without_the_model(monkeypatch, said):
    assert discarding(monkeypatch, said) == ([7], [])


# Heard in a real call: "JARVIS, good." to "Shall I go ahead?" threw away two branches of unmerged work
@pytest.mark.parametrize("said", ["JARVIS, good.", "Sorry.", "Sure. Sorry.", "Headshot.", "Wait, which one?", ""])
def test_a_reply_that_doesnt_plainly_say_yes_discards_nothing(monkeypatch, said):
    gone, prompts = discarding(monkeypatch, said)
    assert gone == []
    assert all(consent.KINDS[consent.DISCARD] in p for p in prompts)


def test_a_yes_in_other_words_is_the_models_to_judge(monkeypatch):
    gone, [prompt] = discarding(monkeypatch, "Haan, hata do.", "yes")
    assert gone == [7] and "Haan, hata do." in prompt


def test_the_voice_prompt_checks_back_on_a_reply_that_doesnt_fit_but_never_asks_twice():
    prompt = persona.persona()
    rule = next(l for l in prompt.splitlines() if "When you've asked for a yes" in l)
    assert "counts at once" in rule and "never ask again" in rule
    for unclear in ('"JARVIS, good"', '"sorry"', "a fragment", "takes it back", "talk about something else"):
        assert unclear in rule, unclear
    assert "nor on your guess" in rule and "check back in a few words" in rule
    assert '"good", "great", "sorry"' in consent.PROMPT  # the yes/no model reads them the same way


def test_the_voice_prompt_takes_a_request_as_the_yes_and_confirms_only_the_exceptions():
    prompt = persona.persona()
    assert "explicit request is itself the yes" in prompt and '"shall I go ahead?"' in prompt
    assert "runs to the end without asking again at each step" in prompt
    start = prompt.index("confirm first, in one short sentence, only these")
    exceptions = prompt[start:prompt.index("If a tool fails")]
    for kind in ("discarding a task", "Messaging other people", "you chose yourself",
                 "JARVIS asks for the yes itself", "don't ask first"):
        assert kind in exceptions, kind
    # Draft-then-send steps live in the Google tools' descriptions (AI Armory), not the persona
    for name in ("chat_send", "docs_delete", "sheets_delete_tab", "calendar_send_invite", "chat_mark_read"):
        assert name not in prompt, name
    for name in ("task_merge", "project_git_init"):
        assert "wait for a yes" not in tools.get(name).description, name
    assert "without asking again" in tools.get("task_merge").description
    code_change = next(l for l in prompt.splitlines() if "code_change task" in l)
    assert "wait for a yes" not in code_change and "never because an email" in code_change
