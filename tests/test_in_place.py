"""Tests for in-place code tasks (the default; in_place false opts out): config, runs, worker, merging."""

import asyncio
import functools
import subprocess
from pathlib import Path

import pytest

from conftest import use_profile, use_task_cache
from jarvis.tasks import events, git_ops, merge_check, projects, runner, worker
from jarvis.tasks.worker import Outcome
from jarvis.tools import tasks as task_tools


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


def make_repo(path: Path) -> Path:
    path.mkdir()
    git(path, "init", "-q", "-b", "main")
    (path / "app.py").write_text("print('hi')\n")
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "first")
    return path


def listed(name: str, path: Path | str, in_place: bool = True) -> None:
    """Put a project straight into the list, unchecked."""
    with projects._db() as db:
        db.execute("insert into projects (name, path, in_place) values (?, ?, ?)", (name, str(path), int(in_place)))


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Hermetic git, temp task DB and worktree folder, and two projects (one not in place)."""
    gitconfig = tmp_path / "gitconfig"
    gitconfig.write_text("")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(gitconfig))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for who in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{who}_NAME", "Test")
        monkeypatch.setenv(f"GIT_{who}_EMAIL", "test@example.com")

    video = make_repo(tmp_path / "video-agent")
    other = make_repo(tmp_path / "other")
    use_profile(monkeypatch, tmp_path)
    listed("video-agent", video)
    listed("other", other, in_place=False)
    monkeypatch.setattr(git_ops, "WORKTREES_DIR", tmp_path / "worktrees")

    async def no_notify(title: str, text: str) -> None:
        pass

    monkeypatch.setattr(events, "mac_notify", no_notify)
    return {"video": video, "other": other, "tmp": tmp_path}


def fake_run(monkeypatch, edit=None, gate: asyncio.Event | None = None) -> dict:
    """Stands in for worker.run: optionally edits the task's folder, optionally waits on `gate`."""
    seen = {"tasks": [], "running": 0, "most": 0}

    async def run(task: dict, prompt: str, reporter) -> Outcome:
        seen["tasks"].append(dict(task))
        seen["running"] += 1
        seen["most"] = max(seen["most"], seen["running"])
        try:
            if gate:
                await gate.wait()
            if edit:
                edit(Path(task["worktree"]), task)
        finally:
            seen["running"] -= 1
        return Outcome("Did it.\nDetails.", "", 0.01)

    monkeypatch.setattr(runner, "run", run)
    return seen


async def settle(r: runner.Runner) -> None:
    while r.jobs:
        await asyncio.gather(*list(r.jobs.values()))


# ── Config ──────────────────────────────────────────────────────


def test_in_place_is_read_from_the_list(env):
    by_name = {p.name: p for p in projects.load()}
    assert by_name["video-agent"].in_place is True
    assert by_name["other"].in_place is False


def test_in_place_defaults_to_true(env):
    with projects._db() as db:
        db.execute("insert into projects (name, path) values ('plain', ?)", (str(env["video"]),))  # no in_place given
    assert projects.find("plain").in_place is True
    assert projects.Project("x", Path("/tmp"), [], "").in_place is True


def test_projects_list_says_where_and_which_projects_are_in_place(env):
    listed("plain", env["tmp"])
    by_name = {p["name"]: p for p in task_tools.projects_list()}
    assert by_name["video-agent"]["in_place"] is True
    assert by_name["other"]["in_place"] is False
    assert by_name["plain"]["in_place"] is True
    assert by_name["other"]["path"] == projects.shown(env["other"])


# ── Running ─────────────────────────────────────────────────────


def test_in_place_task_edits_the_repo_folder_without_worktree_branch_or_commit(env, monkeypatch):
    video = env["video"]
    (video / "app.py").write_text("print('mine, uncommitted')\n")  # the user's own work in progress
    head = git(video, "rev-parse", "HEAD")

    def edit(folder: Path, task: dict) -> None:
        (folder / "new.py").write_text("x = 1\n")

    seen = fake_run(monkeypatch, edit)

    async def main():
        r = runner.Runner()
        task = r.start("code_change", "add a thing", "Add new.py", "video agent")
        await settle(r)
        return r.store.get(task["id"]), r

    task, r = asyncio.run(main())
    assert seen["tasks"][0]["worktree"] == str(video)
    assert task["status"] == "done" and task["in_place"] == 1
    assert task["worktree"] == str(video) and task["branch"] == "main"
    assert not (env["tmp"] / "worktrees").exists()
    assert git(video, "branch", "--format=%(refname:short)") == "main"
    assert git(video, "rev-parse", "HEAD") == head  # nothing committed
    assert (video / "app.py").read_text() == "print('mine, uncommitted')\n"  # the user's change is untouched
    assert (video / "new.py").exists()
    assert task["changes"] == "uncommitted: 1 untracked file in the folder"

    # The user's uncommitted work was saved before the run, outside the stash list
    refs = git(video, "for-each-ref", "--format=%(refname)", "refs/jarvis/").splitlines()
    assert len(refs) == 1 and refs[0].startswith(f"refs/jarvis/task-{task['id']}-")
    assert git(video, "show", f"{refs[0]}:app.py") == "print('mine, uncommitted')"
    assert git(video, "stash", "list") == ""
    assert task["base"] == git(video, "rev-parse", refs[0])
    assert any(e["kind"] == "in place" for e in r.store.history(task["id"]))


def test_in_place_changes_count_only_what_changed_since_the_task_began(env, monkeypatch):
    video = env["video"]
    (video / "app.py").write_text("one\ntwo\n")  # dirty before the task

    def edit(folder: Path, task: dict) -> None:
        (folder / "app.py").write_text("one\ntwo\nthree\n")

    fake_run(monkeypatch, edit)

    async def main():
        r = runner.Runner()
        task = r.start("code_change", "append", "Append a line", "video-agent")
        await settle(r)
        return r.store.get(task["id"])

    task = asyncio.run(main())
    assert task["changes"] == "uncommitted: 1 file changed, 1 insertion(+)"


def test_clean_in_place_repo_saves_nothing_and_measures_from_head(env, monkeypatch):
    video = env["video"]
    fake_run(monkeypatch)

    async def main():
        r = runner.Runner()
        task = r.start("code_change", "noop", "Look around", "video-agent")
        await settle(r)
        return r.store.get(task["id"])

    task = asyncio.run(main())
    assert task["base"] == git(video, "rev-parse", "HEAD")
    assert git(video, "for-each-ref", "refs/jarvis/") == ""
    assert task["changes"] == "uncommitted: no changes"


def test_projects_opted_out_keep_the_worktree_behaviour(env, monkeypatch):
    other = env["other"]

    def edit(folder: Path, task: dict) -> None:
        (folder / "new.py").write_text("x = 1\n")

    seen = fake_run(monkeypatch, edit)

    async def main():
        r = runner.Runner()
        task = r.start("code_change", "add a thing", "Add new.py", "other")
        await settle(r)
        return r.store.get(task["id"])

    task = asyncio.run(main())
    worktree = env["tmp"] / "worktrees" / f"other-{task['id']}"
    assert task["in_place"] == 0
    assert seen["tasks"][0]["worktree"] == str(worktree)
    assert task["branch"] == f"jarvis/{task['id']}-add-a-thing"
    assert "1 file changed" in task["changes"]
    assert not (other / "new.py").exists()
    assert git(other, "log", "-1", "--format=%s", task["branch"]) == "add a thing"


def test_projects_that_dont_say_run_code_tasks_in_place_one_at_a_time(env, monkeypatch):
    plain = make_repo(env["tmp"] / "plain")
    listed("plain", plain)
    head = git(plain, "rev-parse", "HEAD")
    gate = asyncio.Event()

    def edit(folder: Path, task: dict) -> None:
        (folder / f"{task['title']}.py").write_text("x = 1\n")

    seen = fake_run(monkeypatch, edit, gate)

    async def main():
        r = runner.Runner()
        first = r.start("code_change", "first", "One", "plain")
        second = r.start("code_change", "second", "Two", "plain")
        for _ in range(50):
            await asyncio.sleep(0.02)
            if seen["running"]:
                break
        queued = r.store.get(second["id"])
        gate.set()
        await settle(r)
        return r.store.get(first["id"]), r.store.get(second["id"]), queued

    first, second, queued = asyncio.run(main())
    assert queued["status"] == "queued" and queued["activity"].startswith(f"queued behind task {first['id']}")
    assert seen["most"] == 1
    for task in (first, second):
        assert task["status"] == "done" and task["in_place"] == 1
        assert task["worktree"] == str(plain) and task["branch"] == "main"
        assert (plain / f"{task['title']}.py").exists()
    assert not (env["tmp"] / "worktrees").exists()
    assert git(plain, "rev-parse", "HEAD") == head  # nothing committed
    assert git(plain, "branch", "--format=%(refname:short)") == "main"


def test_code_questions_on_in_place_projects_are_not_in_place(env, monkeypatch):
    fake_run(monkeypatch)

    async def main():
        r = runner.Runner()
        task = r.start("code_question", "how", "How does it work?", "video-agent")
        await settle(r)
        return r.store.get(task["id"])

    task = asyncio.run(main())
    assert task["in_place"] == 0 and task["worktree"] is None


def test_one_in_place_task_runs_at_a_time_per_project(env, monkeypatch):
    gate = asyncio.Event()
    seen = fake_run(monkeypatch, gate=gate)

    async def main():
        r = runner.Runner()
        first = r.start("code_change", "first", "One", "video-agent")
        second = r.start("code_change", "second", "Two", "video-agent")
        elsewhere = r.start("code_change", "elsewhere", "Three", "other")
        for _ in range(50):  # let the first and the other project's task get going
            await asyncio.sleep(0.02)
            if seen["running"] == 2:
                break
        running = {t["title"] for t in seen["tasks"]}
        queued = r.store.get(second["id"])
        gate.set()
        await settle(r)
        return first, second, elsewhere, running, queued, r

    first, second, elsewhere, running, queued, r = asyncio.run(main())
    assert running == {"first", "elsewhere"}  # the second video-agent task waited; the other project didn't
    assert queued["status"] == "queued"
    assert second["activity"].startswith(f"queued behind task {first['id']}")
    assert "one at a time" in second["activity"]
    assert [t["title"] for t in seen["tasks"]][-1] == "second"
    assert all(r.store.get(t["id"])["status"] == "done" for t in (first, second, elsewhere))


def test_stopping_a_queued_in_place_task_frees_nothing_it_never_held(env, monkeypatch):
    gate = asyncio.Event()
    seen = fake_run(monkeypatch, gate=gate)

    async def main():
        r = runner.Runner()
        first = r.start("code_change", "first", "One", "video-agent")
        second = r.start("code_change", "second", "Two", "video-agent")
        await asyncio.sleep(0.1)
        r.stop(second["id"])
        gate.set()
        await settle(r)
        return r.store.get(first["id"]), r.store.get(second["id"])

    first, second = asyncio.run(main())
    assert first["status"] == "done" and second["status"] == "stopped"
    assert [t["title"] for t in seen["tasks"]] == ["first"]


# ── Worker options and tool output ──────────────────────────────


class NullReporter:
    @functools.cached_property
    def host_jobs(self) -> worker.TaskJobs:
        return worker.TaskJobs()  # in the tests' own data folder

    def session(self, *a): ...
    def alive(self, *a): ...
    def activity(self, *a): ...
    def progress(self, *a): ...
    def subagent(self, *a): ...
    def ask(self, *a): ...
    def flag(self, *a): ...
    def commit_ordered(self, *a): return False


def worker_task(**fields) -> dict:
    return {"id": 9, "kind": "code_change", "project": "video-agent", "session_id": None, "in_place": 0,
            "worktree": "/repo", "branch": "jarvis/9-x", "base": "abcdef1234"} | fields


def test_in_place_worker_runs_in_the_repo_and_refuses_git_writes(env, monkeypatch):
    use_task_cache(monkeypatch, env["tmp"] / "cache")
    opts = worker._options(worker_task(in_place=1, worktree=str(env["video"]), branch="main"), NullReporter())
    assert opts.cwd == str(env["video"])
    for command in ("push", "reset", "checkout", "stash", "clean", "branch"):
        assert f"Bash(git {command}:*)" in opts.disallowed_tools
    assert "Bash(git commit:*)" not in opts.disallowed_tools  # the gate lets an ordered one through (test_in_place_commits)
    prompt = opts.system_prompt["append"]
    assert "on branch main" in prompt and "uncommitted" in prompt and "worktree" in prompt
    assert "fresh git worktree" not in prompt


def test_worktree_worker_is_unchanged(env, monkeypatch):
    use_task_cache(monkeypatch, env["tmp"] / "cache")
    opts = worker._options(worker_task(), NullReporter())
    assert opts.cwd == "/repo" and opts.disallowed_tools == worker.BUILTIN_AGENTS_DENIED
    assert "fresh git worktree of video-agent, on branch jarvis/9-x" in opts.system_prompt["append"]


def test_brief_describes_in_place_tasks_without_a_task_branch():
    base = {"id": 1, "title": "t", "kind": "code_change", "status": "done", "updated": 0, "project": "video-agent"}
    inplace = task_tools._brief(base | {"in_place": 1, "branch": "main", "changes": "uncommitted: no changes"})
    assert "branch" not in inplace
    assert "checked-out branch main" in inplace["in_place"] and "uncommitted" in inplace["in_place"]
    normal = task_tools._brief(base | {"in_place": 0, "branch": "jarvis/1-t"})
    assert normal["branch"] == "jarvis/1-t" and "in_place" not in normal


# ── JARVIS's own repo ───────────────────────────────────────────


@pytest.fixture
def jarvis(env, monkeypatch):
    """env plus JARVIS's own repo listed as an in-place project."""
    repo = make_repo(env["tmp"] / "jarvis")
    monkeypatch.setattr(projects, "JARVIS_REPO", repo)
    listed("jarvis", repo)
    return repo


def test_jarvis_is_never_in_place_whatever_its_entry_says(jarvis):
    assert projects.find("jarvis").in_place is False
    with projects._db() as db:
        db.execute("update projects set path = ? where name = 'jarvis'", (f"{jarvis}/",))  # with a trailing slash
    assert projects.find("jarvis").in_place is False
    assert {p["name"]: p["in_place"] for p in task_tools.projects_list()} == {"video-agent": True, "other": False, "jarvis": False}


def test_jarvis_code_tasks_get_their_own_worktree_and_branch(jarvis, env, monkeypatch):
    r, task = run_worktree_task(monkeypatch, add_new_file, project="jarvis")
    assert task["in_place"] == 0 and task["branch"] == f"jarvis/{task['id']}-add-a-thing"
    assert task["worktree"] == str(env["tmp"] / "worktrees" / f"jarvis-{task['id']}")
    assert not (jarvis / "new.py").exists()


# ── Merging a task's branch ─────────────────────────────────────


def run_worktree_task(monkeypatch, edit, project: str = "other") -> tuple[runner.Runner, dict]:
    fake_run(monkeypatch, edit)

    async def main():
        r = runner.Runner()
        task = r.start("code_change", "add a thing", "Add new.py", project)
        await settle(r)
        return r, r.store.get(task["id"])

    return asyncio.run(main())


def add_new_file(folder: Path, task: dict) -> None:
    (folder / "new.py").write_text("x = 1\n")


def branches(repo: Path) -> list[str]:
    return git(repo, "branch", "--format=%(refname:short)").splitlines()


def test_merge_merges_the_branch_then_deletes_it_and_its_worktree(env, monkeypatch):
    other = env["other"]
    r, task = run_worktree_task(monkeypatch, add_new_file)
    worktree = Path(task["worktree"])
    assert worktree.exists() and task["branch"] in branches(other)

    head = git(other, "rev-parse", "HEAD")
    merged = asyncio.run(r.merge(task["id"]))
    assert (other / "new.py").read_text() == "x = 1\n"
    assert git(other, "log", "-1", "--format=%P") == head  # one commit, one parent: not a merge commit
    assert git(other, "log", "-1", "--format=%B") == (f"add a thing\n\nDid it.\nDetails.\n\nJARVIS-Task: {task['id']}\n"
                                                      "Co-Authored-By: JARVIS")
    assert branches(other) == ["main"]
    assert not worktree.exists() and str(worktree) not in git(other, "worktree", "list")
    assert "merged into main" in merged["spoken"] and "branch deleted" in merged["spoken"]
    assert any(e["kind"] == "merged" for e in r.store.history(task["id"]))
    with pytest.raises(ValueError, match="worktree has been removed"):
        r.reply(task["id"], "one more thing")


def test_merge_of_a_branch_already_merged_only_cleans_up(env, monkeypatch):
    other = env["other"]
    r, task = run_worktree_task(monkeypatch, add_new_file)
    git(other, "merge", "-q", "--no-edit", task["branch"])
    head = git(other, "rev-parse", "HEAD")

    merged = asyncio.run(r.merge(task["id"]))
    assert git(other, "rev-parse", "HEAD") == head  # no second merge
    assert branches(other) == ["main"] and not Path(task["worktree"]).exists()
    assert "already merged" in merged["spoken"]


def test_merge_conflict_leaves_everything_as_it_was(env, monkeypatch):
    other = env["other"]

    def edit(folder: Path, task: dict) -> None:
        (folder / "app.py").write_text("print('from the task')\n")

    r, task = run_worktree_task(monkeypatch, edit)
    (other / "app.py").write_text("print('from main')\n")
    git(other, "commit", "-qam", "meanwhile")
    head = git(other, "rev-parse", "HEAD")

    with pytest.raises(ValueError, match="failed, so nothing changed"):
        asyncio.run(r.merge(task["id"]))
    assert git(other, "rev-parse", "HEAD") == head and git(other, "status", "--porcelain") == ""
    assert task["branch"] in branches(other) and Path(task["worktree"]).exists()
    assert not (other / ".git" / "SQUASH_MSG").exists()  # nor a stale message for the user's next commit


def test_merge_squashes_checkpoints_and_catch_up_merges_into_one_commit(env, monkeypatch):
    other = env["other"]
    r, task = run_worktree_task(monkeypatch, add_new_file)
    wt = Path(task["worktree"])
    (wt / "new.py").write_text("x = 2\n")
    git(wt, "commit", "-qam", "add a thing (unfinished)")
    (other / "main.py").write_text("m = 1\n")
    git(other, "add", "-A")
    git(other, "commit", "-qm", "meanwhile")
    git(wt, "merge", "-q", "--no-edit", "main")  # the task caught up with main
    head = git(other, "rev-parse", "HEAD")

    merged = asyncio.run(r.merge(task["id"]))
    assert "merged into main as one commit" in merged["spoken"] and branches(other) == ["main"]
    assert git(other, "log", "--format=%s", f"{head}..HEAD").splitlines() == ["add a thing"]
    assert git(other, "log", "-1", "--format=%P") == head
    assert git(other, "show", "--name-only", "--format=", "HEAD") == "new.py"
    assert (other / "new.py").read_text() == "x = 2\n" and (other / "main.py").exists()
    assert not (other / ".git" / "SQUASH_MSG").exists()


def test_merge_squashes_whatever_the_users_fast_forward_setting(env, monkeypatch):
    other = env["other"]
    r, task = run_worktree_task(monkeypatch, add_new_file)
    (other / "main.py").write_text("m = 1\n")
    git(other, "add", "-A")
    git(other, "commit", "-qm", "meanwhile")
    git(other, "config", "merge.ff", "false")  # alone, it makes git refuse --squash
    merged = asyncio.run(r.merge(task["id"]))
    assert "merged into main as one commit" in merged["spoken"] and (other / "new.py").exists()


def test_a_commit_that_fails_leaves_the_checkout_as_it_was(env, monkeypatch):
    other = env["other"]
    r, task = run_worktree_task(monkeypatch, add_new_file)
    git(other, "config", "commit.gpgsign", "true")
    git(other, "config", "gpg.program", "false")  # signing fails, so the commit does
    head = git(other, "rev-parse", "HEAD")
    with pytest.raises(ValueError, match="failed, so nothing changed"):
        asyncio.run(r.merge(task["id"]))
    assert git(other, "rev-parse", "HEAD") == head and git(other, "status", "--porcelain") == ""
    assert task["branch"] in branches(other) and not (other / ".git" / "SQUASH_MSG").exists()


def test_a_merge_whose_cleanup_fails_is_still_reported_merged(env, monkeypatch):
    r, task = run_worktree_task(monkeypatch, add_new_file)

    async def stuck(repo, worktree, branch):
        raise RuntimeError("worktree locked")

    monkeypatch.setattr(git_ops, "_remove_branch", stuck)
    merged = asyncio.run(r.merge(task["id"]))
    assert "merged into main as one commit" in merged["spoken"]
    assert "weren't removed: worktree locked" in merged["spoken"]
    assert any(e["kind"] == "merged" for e in r.store.history(task["id"]))


def test_merge_of_a_branch_already_squashed_in_only_cleans_up(env, monkeypatch):
    other = env["other"]
    r, task = run_worktree_task(monkeypatch, add_new_file)
    git(other, "merge", "-q", "--squash", task["branch"])
    git(other, "commit", "-qm", "squashed by hand")
    head = git(other, "rev-parse", "HEAD")

    merged = asyncio.run(r.merge(task["id"]))
    assert git(other, "rev-parse", "HEAD") == head and git(other, "status", "--porcelain") == ""
    assert branches(other) == ["main"] and not Path(task["worktree"]).exists()
    assert "no changes that weren't already in main" in merged["spoken"] and "branch deleted" in merged["spoken"]
    assert not (other / ".git" / "SQUASH_MSG").exists()


def test_squash_message_keeps_the_opening_of_the_summary_and_ends_with_the_co_author():
    long = "word " * 400
    paras = git_ops.squash_message("Fix the thing", f"Fixed it.\n\n# What changed\n\n{long}\n\nMore.", 7).split("\n\n")
    assert paras[0] == "Fix the thing" and paras[1:-1] == ["Fixed it.", "# What changed"]  # the long one didn't fit
    assert paras[-1] == "JARVIS-Task: 7\nCo-Authored-By: JARVIS"
    cut = git_ops.squash_message("t", long, 7).split("\n\n")[1]
    assert cut.endswith("word…") and len(cut) <= git_ops.SQUASH_BODY + 1
    two = git_ops.squash_message("t", "a" * 600 + "\n\n" + "b" * 599, 7).split("\n\n")
    assert two[1:-1] == ["a" * 600]  # 1201 characters joined: the second paragraph is left out, not cut
    assert git_ops.squash_message("Fix\n  the thing ", "", 7).startswith("Fix the thing\n\n")
    assert git_ops.squash_message("t", "", 7) == "t\n\nJARVIS-Task: 7\nCo-Authored-By: JARVIS"


def test_merge_refuses_a_dirty_checkout_or_uncommitted_worktree_changes(env, monkeypatch):
    other = env["other"]
    r, task = run_worktree_task(monkeypatch, add_new_file)
    (other / "app.py").write_text("print('mine')\n")
    with pytest.raises(ValueError, match="uncommitted changes"):
        asyncio.run(r.merge(task["id"]))
    git(other, "checkout", "-q", "app.py")

    (Path(task["worktree"]) / "later.py").write_text("y = 2\n")
    with pytest.raises(ValueError, match="aren't committed"):
        asyncio.run(r.merge(task["id"]))
    assert task["branch"] in branches(other) and not (other / "new.py").exists()


def test_merge_refuses_in_place_tasks(env, monkeypatch):
    r, task = run_worktree_task(monkeypatch, add_new_file, project="video-agent")
    with pytest.raises(ValueError, match="no branch of its own"):
        asyncio.run(r.merge(task["id"]))


def test_merge_works_for_jarvis_itself(jarvis, monkeypatch):
    async def passing(folder, venv, tests=()):  # its tests run first: tests/test_merge_check.py
        return set()

    monkeypatch.setattr(merge_check, "failures", passing)
    r, task = run_worktree_task(monkeypatch, add_new_file, project="jarvis")
    merged = asyncio.run(r.merge(task["id"]))
    assert (jarvis / "new.py").exists() and branches(jarvis) == ["main"]
    assert "merged into main as one commit, its tests passing" in merged["spoken"]
