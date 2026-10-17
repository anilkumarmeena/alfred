"""Tests for commits in the user's own checkout: an in-place task commits only when the user's own words order it."""

import asyncio

import pytest
from test_approvals import pushes, running_task  # noqa: F401  (pushes is a fixture)
from test_in_place import NullReporter, env, git, worker_task  # noqa: F401  (env is a fixture)
from test_nudges import fake_run, settle

from conftest import use_task_cache
from jarvis.llm.claude import ATTRIBUTION
from jarvis.safety.commands import in_place_git, orders, refused
from jarvis.storage import profile
from jarvis.tasks import git_ops, runner, worker
from jarvis.tasks.gate import TaskGate
from jarvis.tasks.worker import Outcome

HEREDOC = "git add app.py && git commit -m \"$(cat <<'EOF'\nFix the typo, it's done\n\nCo-Authored-By: JARVIS\nEOF\n)\""


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


# ── Which git commands an ordered commit lets through ───────────


@pytest.mark.parametrize("command", ["git commit -m 'Fix typo'", "git add app.py && git commit -m x", HEREDOC,
                                     "git -C . commit -qm x"])
def test_a_plain_commit_is_found_as_just_a_commit(command):
    assert in_place_git(command) == ["commit"]


@pytest.mark.parametrize("command, found", [
    ("git commit --amend --no-edit", ["commit --amend"]), ("git commit --am -m x", ["commit --amend"]),
    ("git commit -m x && git push", ["commit", "push"]), ("git commit -m x; git reset --hard HEAD~1", ["commit", "reset"]),
    ("git checkout -b fix && git commit -m x", ["checkout", "commit"]), ("git status && npm test", []),
    ("cd app&&git commit -m \"$(cat <<'EOF'\nit's\nEOF\n)\"; echo \"x", ["commit"]),  # unparsable: still seen
    ("bash <<EOF\ngit commit -m x\nEOF", ["commit"]),  # a heredoc a shell runs is commands
])
def test_what_else_a_commit_comes_with_is_found_too(command, found):
    assert in_place_git(command) == found


def test_a_commit_message_s_lines_are_not_read_as_commands():
    message = HEREDOC.replace("Fix the typo", "Make git push retry on 503\ngit reset no longer needed")
    assert in_place_git(message) == ["commit"]


def test_on_the_computer_itself_commits_stay_refused_to_in_place_tasks():
    assert "leave to them" in refused(HEREDOC, in_place=True)  # an unparsable heredoc message included
    assert refused(HEREDOC) == ""


# ── The task's gate ─────────────────────────────────────────────


def bash(gate: TaskGate, command: str) -> dict:
    return asyncio.run(gate.pre_tool_use({"tool_name": "Bash", "tool_input": {"command": command}}, "t1", None))


def denied(out: dict) -> str:
    return out.get("hookSpecificOutput", {}).get("permissionDecisionReason", "")


@pytest.mark.parametrize("ordered", [lambda: True, lambda: asyncio.sleep(0, True)])  # here, or asked over a link
def test_a_task_commits_in_place_once_the_user_ordered_it(tmp_path, ordered):
    gate = TaskGate(tmp_path, [tmp_path], [tmp_path], commit_ordered=ordered)
    assert bash(gate, "git commit -m 'Fix typo'") == {}
    assert "--amend" in denied(bash(gate, "git commit --amend --no-edit"))
    assert "git push" in denied(bash(gate, "git commit -m x && git push"))


def test_without_an_order_a_commit_is_denied_saying_why(tmp_path):
    asked = []
    gate = TaskGate(tmp_path, [tmp_path], [tmp_path], commit_ordered=lambda: asked.append(1) or False)
    assert f"only when {profile.name()}'s own words ask for one" in denied(bash(gate, HEREDOC))
    assert bash(gate, "git status && npm test") == {} and len(asked) == 1  # only a commit asks about the order
    assert "git stash" in denied(bash(gate, "git -C . stash"))  # what the CLI's deny rules miss


def test_a_worktree_task_s_shell_is_left_to_the_sandbox(tmp_path):
    assert bash(TaskGate(tmp_path, [tmp_path], [tmp_path]), "git commit -m x && git push") == {}


def test_only_an_in_place_task_asks_about_commit_orders(env, monkeypatch):
    use_task_cache(monkeypatch, env["tmp"] / "cache")

    class Ordered(NullReporter):
        def commit_ordered(self, task_id: int) -> bool:
            return task_id == 9

    opts = worker._options(worker_task(in_place=1, worktree=str(env["video"]), branch="main"), Ordered())
    assert opts.can_use_tool.commit_ordered() is True
    assert f'end the message with "{ATTRIBUTION["commit"]}"' in opts.system_prompt["append"]
    assert worker._options(worker_task(), Ordered()).can_use_tool.commit_ordered is None


# ── The runner: orders kept on the task ─────────────────────────


def test_the_users_order_to_commit_reaches_the_task_from_its_start_or_a_reply(env, monkeypatch):
    seen: list[bool] = []

    def checks(summary: str):
        async def body(task, prompt, reporter) -> Outcome:
            reporter.session(task["id"], "s1")
            seen.append(reporter.commit_ordered(task["id"]))
            return Outcome(summary, "", 0.01)
        return body

    fake_run(monkeypatch, checks("Fixed."), checks("Fixed."), checks("Committed."))

    async def main():
        r = runner.Runner()
        r.start("code_change", "fix typo", "Fix the typo and commit it", "video-agent", order="Fix the typo and commit it.")
        await settle(r)
        unasked = r.start("code_change", "fix typo", "Fix the typo", "video-agent")
        await settle(r)
        r.reply(unasked["id"], "Commit it now.", order="Commit it now.")
        await settle(r)

    asyncio.run(main())
    assert seen == [True, False, True]


def test_a_change_of_mind_takes_back_the_order_to_commit(env, pushes):  # noqa: F811
    r = runner.Runner()
    task = running_task(r, "fix typo")
    r.reply(task["id"], "Fix it and commit and push.", order="Fix it and commit and push.")
    assert r.commit_ordered(task["id"])
    r.reply(task["id"], "Actually, don't commit yet.", order="Actually, don't commit yet.")
    assert not r.commit_ordered(task["id"])
    assert any("no longer order a commit" in e["text"] for e in r.store.history(task["id"]))


def test_a_failed_check_for_an_order_keeps_the_commit_from_running(tmp_path):
    def broken() -> bool:
        raise RuntimeError("database is locked")

    assert "only when" in denied(bash(TaskGate(tmp_path, [tmp_path], [tmp_path], commit_ordered=broken), "git commit -m x"))


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
