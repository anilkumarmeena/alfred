"""Tests for commits in the user's own checkout: the user's words that order one, what an in-place task is told, and
what a run reports."""

import asyncio

import pytest
from test_approvals import pushes, running_task  # noqa: F401  (pushes is a fixture)
from test_in_place import NullReporter, env, git, worker_task  # noqa: F401  (env is a fixture)

from alfred.llm.claude import ATTRIBUTION
from alfred.safety.commands import orders
from alfred.storage import profile
from alfred.tasks import git_ops, runner, worker

# ── What orders a commit ────────────────────────────────────────


@pytest.mark.parametrize("said", ["Fix the login bug and commit it.", "Commit that.", "Okay, commit and push.",
                                  "Make a commit.", "Fix it, then commit.", "Commit the changes please."])
def test_words_that_ask_for_a_commit_order_one(said):
    assert "commit" in orders(said)


@pytest.mark.parametrize("said", [
    "The last commit broke the build, fix it.", "Look at that commit.", "Fix the bug from commit a1b2c3.",
    "Revert commit abc123.", "Find what commit broke login and fix it.", "Improve commit messages.",
    "Fix the pre-commit hook.", "Add a commit-msg hook.", "Git commit hangs on the hook, fix it.",
    "Fix it and I'll commit it myself.",  # a commit named, or the user's own
    "Don't commit.", "Should you commit it?", "Fix it, but don't commit yet.", "Fix the login bug.",
])
def test_a_commit_named_doubted_or_unasked_orders_none(said):
    assert "commit" not in orders(said)


# ── What an in-place task is told ───────────────────────────────


def test_an_in_place_task_is_told_to_commit_only_when_asked_and_how(env):  # noqa: F811
    opts = worker._options(worker_task(in_place=1, worktree=str(env["video"]), branch="main"), NullReporter())
    prompt = opts.system_prompt["append"]
    assert f"Commit only when {profile.name()}'s own request asks you to" in prompt
    assert f'end the message with "{ATTRIBUTION["commit"]}"' in prompt
    assert "Bash(git push:*)" in opts.disallowed_tools and "Bash(git commit:*)" not in opts.disallowed_tools


# ── The runner: orders kept on the task ─────────────────────────


def test_a_change_of_mind_takes_back_the_order_to_commit(env, pushes):  # noqa: F811
    r = runner.Runner()
    task = running_task(r, "fix typo")
    r.reply(task["id"], "Fix it and commit and push.", order="Fix it and commit and push.")
    assert any("commit" in orders(o["said"]) for o in r._orders(task["id"]))
    r.reply(task["id"], "Actually, don't commit yet.", order="Actually, don't commit yet.")
    assert not any("commit" in orders(o["said"]) for o in r._orders(task["id"]))
    assert any("no longer order a commit" in e["text"] for e in r.store.history(task["id"]))


# ── What the run reports ────────────────────────────────────────


def test_a_commit_it_made_is_reported_rather_than_called_uncommitted(env):
    folder = env["video"]
    base = git(folder, "rev-parse", "HEAD")
    (folder / "app.py").write_text("print('hello')\n")
    said = asyncio.run(git_ops.changes_op({"folder": str(folder), "base": base}))["said"]
    assert said.startswith("uncommitted: 1 file changed")
    git(folder, "commit", "-qam", "Fix greeting")
    said = asyncio.run(git_ops.changes_op({"folder": str(folder), "base": base}))["said"]
    assert said.startswith("1 new commit since it began; changed since then: 1 file changed")


def test_a_first_commit_in_a_repo_that_had_none_is_reported_too(tmp_path, env):
    folder = tmp_path / "fresh"
    folder.mkdir()
    git(folder, "init", "-q")
    began = asyncio.run(git_ops.enter_op({"folder": str(folder), "task": 1}))
    (folder / "app.py").write_text("print('hi')\n")
    git(folder, "add", "app.py")
    git(folder, "commit", "-qm", "First")
    said = asyncio.run(git_ops.changes_op({"folder": str(folder), "base": began["base"]}))["said"]
    assert said.startswith("1 new commit since it began")
