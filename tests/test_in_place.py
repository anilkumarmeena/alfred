"""Tests for in-place code tasks (the default; in_place false opts out): config, runs, worker, merging."""

import asyncio
import shutil
import subprocess
from pathlib import Path

import pytest

from alfred import projects
from alfred.tasks import events, git_ops, merge_check, runner, warm, worker
from alfred.tasks.worker import Outcome
from alfred.tools import tasks as task_tools
from conftest import use_profile


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
    refs = git(video, "for-each-ref", "--format=%(refname)", "refs/alfred/").splitlines()
    assert len(refs) == 1 and refs[0].startswith(f"refs/alfred/task-{task['id']}-")
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
    assert git(video, "for-each-ref", "refs/alfred/") == ""
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
    assert task["branch"] == f"alfred/{task['id']}-add-a-thing"
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
    def session(self, *a): ...
    def alive(self, *a): ...
    def activity(self, *a): ...
    def progress(self, *a): ...
    def subagent(self, *a): ...
    def ask(self, *a): ...
    def flag(self, *a): ...
    def tokens(self, *a): ...
    def commit_ordered(self, *a): return False


class Asking(NullReporter):
    """Records approvals a task asks for; approves those in `agreed`."""

    def __init__(self, agreed: list[str] = ()) -> None:
        self.asked: list[dict] = []
        self.agreed = list(agreed)

    def cleared(self, task_id: int, item: dict) -> bool:
        if item["command"] in self.agreed:
            self.agreed.remove(item["command"])
            return True
        return False

    def request_approval(self, task_id: int, item: dict) -> dict:
        self.asked.append(item)
        return {"id": 12, "commands": [item]}


def worker_task(**fields) -> dict:
    return {"id": 9, "kind": "code_change", "project": "video-agent", "session_id": None, "in_place": 0,
            "worktree": "/repo", "branch": "alfred/9-x", "base": "abcdef1234"} | fields


def test_in_place_worker_runs_in_the_repo_and_refuses_git_writes(env):
    opts = worker._options(worker_task(in_place=1, worktree=str(env["video"]), branch="main"), NullReporter())
    assert opts.cwd == str(env["video"])
    for command in ("push", "reset", "checkout", "stash", "clean", "branch"):
        assert f"Bash(git {command}:*)" in opts.disallowed_tools
    assert "Bash(git commit:*)" not in opts.disallowed_tools  # one the user's request asks for (test_in_place_commits)
    prompt = opts.system_prompt["append"]
    assert "on branch main" in prompt and "uncommitted" in prompt and "worktree" in prompt
    assert "fresh git worktree" not in prompt


def test_worktree_worker_is_unchanged(env):
    worktree = env["tmp"] / "worktree"
    opts = worker._options(worker_task(worktree=str(worktree)), NullReporter())
    assert opts.cwd == str(worktree) and not any(d.startswith("Bash(git") for d in opts.disallowed_tools)
    assert "fresh git worktree of video-agent, on branch alfred/9-x" in opts.system_prompt["append"]


def test_brief_describes_in_place_tasks_without_a_task_branch():
    base = {"id": 1, "title": "t", "kind": "code_change", "status": "done", "updated": 0, "project": "video-agent"}
    inplace = task_tools._brief(base | {"in_place": 1, "branch": "main", "changes": "uncommitted: no changes"})
    assert "branch" not in inplace
    assert "checked-out branch main" in inplace["in_place"] and "uncommitted" in inplace["in_place"]
    normal = task_tools._brief(base | {"in_place": 0, "branch": "alfred/1-t"})
    assert normal["branch"] == "alfred/1-t" and "in_place" not in normal


# ── Alfred's own repo ───────────────────────────────────────────


@pytest.fixture
def alfred(env, monkeypatch):
    """env plus Alfred's own repo listed as an in-place project."""
    repo = make_repo(env["tmp"] / "alfred")
    monkeypatch.setattr(projects, "ALFRED_REPO", repo)
    listed("alfred", repo)
    return repo


def test_alfred_is_never_in_place_whatever_its_entry_says(alfred):
    assert projects.find("alfred").in_place is False
    with projects._db() as db:
        db.execute("update projects set path = ? where name = 'alfred'", (f"{alfred}/",))  # with a trailing slash
    assert projects.find("alfred").in_place is False
    assert {p["name"]: p["in_place"] for p in task_tools.projects_list()} == {"video-agent": True, "other": False, "alfred": False}


def test_alfred_code_tasks_get_their_own_worktree_and_branch(alfred, env, monkeypatch):
    r, task = run_worktree_task(monkeypatch, add_new_file, project="alfred")
    assert task["in_place"] == 0 and task["branch"] == f"alfred/{task['id']}-add-a-thing"
    assert task["worktree"] == str(env["tmp"] / "worktrees" / f"alfred-{task['id']}")
    assert not (alfred / "new.py").exists()


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
    assert git(other, "log", "-1", "--format=%B") == (f"add a thing\n\nDid it.\nDetails.\n\nAlfred-Task: {task['id']}\n"
                                                      "Co-Authored-By: Alfred")
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
    head, tip = git(other, "rev-parse", "HEAD"), git(other, "rev-parse", task["branch"])

    with pytest.raises(ValueError, match="(?s)failed, so nothing changed.*It conflicts with main in app.py"):
        asyncio.run(r.merge(task["id"]))
    assert git(other, "rev-parse", "HEAD") == head and git(other, "status", "--porcelain") == ""
    assert task["branch"] in branches(other) and Path(task["worktree"]).exists()
    assert not (other / ".git" / "SQUASH_MSG").exists()  # nor a stale message for the user's next commit
    wt = Path(task["worktree"])  # its rebase was tried and undone: the branch and worktree are as they were
    assert git(other, "rev-parse", task["branch"]) == tip and git(wt, "status", "--porcelain") == ""
    assert git(wt, "rev-parse", "--abbrev-ref", "HEAD") == task["branch"]
    assert not Path(git(wt, "rev-parse", "--git-path", "rebase-merge")).exists()


def test_a_branch_behind_is_rebased_onto_main_so_its_tests_run_on_what_is_merged(alfred, monkeypatch):
    tested = []

    async def passing(folder, venv, tests=()):
        tested.append((folder / "main.py").exists())
        return set()

    monkeypatch.setattr(merge_check, "failures", passing)
    r, task = run_worktree_task(monkeypatch, add_new_file, project="alfred")
    (alfred / "main.py").write_text("m = 1\n")
    git(alfred, "add", "-A")
    git(alfred, "commit", "-qm", "meanwhile")
    head = git(alfred, "rev-parse", "HEAD")

    merged = asyncio.run(r.merge(task["id"]))
    assert tested[0] is True  # the worktree had main's newer commit when its tests ran
    said = "merged into main as one commit, rebased onto main's newer commits first, its tests passing"
    assert said in merged["spoken"]
    assert git(alfred, "log", "-1", "--format=%P") == head and branches(alfred) == ["main"]
    assert git(alfred, "show", "--name-only", "--format=", "HEAD") == "new.py"


def test_a_rebase_moves_no_other_branch_whatever_the_users_settings(env, monkeypatch):
    other = env["other"]
    r, task = run_worktree_task(monkeypatch, add_new_file)
    git(other, "branch", "mine", task["branch"])  # the user's own branch, on the task's commit
    git(other, "config", "rebase.updateRefs", "true")  # alone, it would move "mine" along with the rebase
    mine = git(other, "rev-parse", "mine")
    (other / "main.py").write_text("m = 1\n")
    git(other, "add", "-A")
    git(other, "commit", "-qm", "meanwhile")

    merged = asyncio.run(r.merge(task["id"]))
    assert "rebased onto main's newer commits first" in merged["spoken"]
    assert git(other, "rev-parse", "mine") == mine and branches(other) == ["main", "mine"]


def test_only_a_tasks_own_branch_in_its_own_worktree_is_ever_rebased(env, monkeypatch):
    other = env["other"]
    _, task = run_worktree_task(monkeypatch, add_new_file)
    wt, tip = Path(task["worktree"]), git(other, "rev-parse", task["branch"])
    (other / "main.py").write_text("m = 1\n")
    git(other, "add", "-A")
    git(other, "commit", "-qm", "meanwhile")
    head = git(other, "rev-parse", "HEAD")

    def catch_up(folder: Path | None, task_id: int) -> tuple[str, list[str]]:
        return asyncio.run(git_ops._catch_up(other, folder and str(folder), task["branch"], task_id, head))

    assert catch_up(wt, task["id"] + 1) == ("", [])  # not the task's own branch
    assert catch_up(other, task["id"]) == ("", [])  # the user's own folder
    assert catch_up(None, task["id"]) == ("", [])
    assert git(other, "rev-parse", task["branch"]) == tip
    assert catch_up(wt, task["id"]) == (tip, [])
    assert git(other, "rev-parse", f"{task['branch']}~1") == head
    assert catch_up(wt, task["id"]) == ("", [])  # not behind any more


def test_a_rebased_branch_that_isnt_merged_after_all_goes_back_as_it_was(alfred, monkeypatch):
    async def failing(folder, venv, tests=()):
        return {"tests/test_x.py::test_y"} if folder != alfred else set()

    monkeypatch.setattr(merge_check, "failures", failing)
    r, task = run_worktree_task(monkeypatch, add_new_file, project="alfred")
    tip = git(alfred, "rev-parse", task["branch"])
    (alfred / "main.py").write_text("m = 1\n")
    git(alfred, "add", "-A")
    git(alfred, "commit", "-qm", "meanwhile")
    head = git(alfred, "rev-parse", "HEAD")

    with pytest.raises(ValueError, match="1 test fails on it that pass on main"):
        asyncio.run(r.merge(task["id"]))
    wt = Path(task["worktree"])
    assert git(alfred, "rev-parse", "HEAD") == head and git(alfred, "rev-parse", task["branch"]) == tip
    assert git(wt, "status", "--porcelain") == "" and not (wt / "main.py").exists()


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
    assert paras[-1] == "Alfred-Task: 7\nCo-Authored-By: Alfred"
    cut = git_ops.squash_message("t", long, 7).split("\n\n")[1]
    assert cut.endswith("word…") and len(cut) <= git_ops.SQUASH_BODY + 1
    two = git_ops.squash_message("t", "a" * 600 + "\n\n" + "b" * 599, 7).split("\n\n")
    assert two[1:-1] == ["a" * 600]  # 1201 characters joined: the second paragraph is left out, not cut
    assert git_ops.squash_message("Fix\n  the thing ", "", 7).startswith("Fix the thing\n\n")
    assert git_ops.squash_message("t", "", 7) == "t\n\nAlfred-Task: 7\nCo-Authored-By: Alfred"


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


def test_merge_works_for_alfred_itself(alfred, monkeypatch):
    async def passing(folder, venv, tests=()):  # its tests run first: tests/test_merge_check.py
        return set()

    monkeypatch.setattr(merge_check, "failures", passing)
    r, task = run_worktree_task(monkeypatch, add_new_file, project="alfred")
    merged = asyncio.run(r.merge(task["id"]))
    assert (alfred / "new.py").exists() and branches(alfred) == ["main"]
    assert "merged into main as one commit, its tests passing" in merged["spoken"]


# ── Worktrees pre-warmed with the main checkout's dependencies ──


def with_dependencies(repo: Path) -> None:
    """Ignored node_modules (top, web/, and in a folder git doesn't have) and a .venv whose scripts name the repo."""
    (repo / ".gitignore").write_text("node_modules/\n.venv/\n")
    (repo / "web").mkdir()
    (repo / "web" / "app.js").write_text("")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "deps ignored")
    for folder in ("node_modules/pkg", "web/node_modules/pkg", "scratch/node_modules/pkg"):
        (repo / folder).mkdir(parents=True)
        (repo / folder / "index.js").write_text("module.exports = 1\n")
    (repo / "node_modules" / ".bin").mkdir()
    (repo / "node_modules" / ".bin" / "pkg").symlink_to("../pkg/index.js")
    bin_, site = repo / ".venv" / "bin", repo / ".venv" / "lib" / "python3.12" / "site-packages"
    bin_.mkdir(parents=True)
    site.mkdir(parents=True)
    (bin_ / "python").symlink_to("/usr/bin/python3")
    (bin_ / "pytest").write_text(f"#!{repo}/.venv/bin/python\nimport pytest\n")
    (bin_ / "activate").write_text(f"VIRTUAL_ENV='{repo}/.venv'\n")
    (site / "_project.pth").write_text(f"{repo}/src\n{repo}-old/src\nimport os\n")


def plain_copy(monkeypatch) -> None:
    """Clones as a plain copy: the tests' filesystem may not clone (Linux tmpfs, ext4)."""

    def clone(source: Path, dest: Path) -> bool:
        shutil.copytree(source, dest, symlinks=True)
        return True

    monkeypatch.setattr(warm, "clone", clone)


def test_a_worktree_starts_with_the_main_checkouts_dependencies(env, monkeypatch):
    other = env["other"]
    with_dependencies(other)
    plain_copy(monkeypatch)
    r, task = run_worktree_task(monkeypatch, add_new_file)
    worktree = Path(task["worktree"])
    assert (worktree / "node_modules/pkg/index.js").is_file() and (worktree / "web/node_modules/pkg/index.js").is_file()
    assert (worktree / "node_modules/.bin/pkg").is_symlink()
    assert not (worktree / "scratch").exists()  # a folder the worktree hasn't got isn't made for it
    # The .venv is the task's own: its scripts run its own python, its .pth files lead into the worktree
    assert (worktree / ".venv/bin/pytest").read_text().startswith(f"#!{worktree}/.venv/bin/python\n")
    assert (worktree / ".venv/bin/activate").read_text() == f"VIRTUAL_ENV='{worktree}/.venv'\n"
    assert (worktree / ".venv/lib/python3.12/site-packages/_project.pth").read_text() == (
        f"{worktree}/src\n{other}-old/src\nimport os\n")  # another folder whose name only starts the same is left
    assert (worktree / ".venv/bin/python").is_symlink()
    assert (other / ".venv/bin/pytest").read_text().startswith(f"#!{other}/.venv/bin/python\n")  # the main one's untouched
    assert "1 file changed" in task["changes"]  # none of it committed
    [warmed] = [e["text"] for e in r.store.history(task["id"]) if e["kind"] == "warmed"]
    assert warmed == f".venv, node_modules, web/node_modules cloned in from {other}"
    asyncio.run(r.merge(task["id"]))
    assert not worktree.exists() and (other / ".venv/bin/pytest").is_file()


def test_folders_git_doesnt_ignore_arent_cloned(env, monkeypatch):
    other = env["other"]
    (other / "node_modules/pkg").mkdir(parents=True)  # not in .gitignore: it would be committed
    plain_copy(monkeypatch)
    r, task = run_worktree_task(monkeypatch, add_new_file)
    assert not (Path(task["worktree"]) / "node_modules").exists()
    assert not [e for e in r.store.history(task["id"]) if e["kind"] == "warmed"]


def test_a_venv_that_cant_be_pointed_at_itself_isnt_kept(env, monkeypatch):
    other = env["other"]
    with_dependencies(other)
    plain_copy(monkeypatch)

    def fails(venv, repo, worktree):
        raise PermissionError("no")

    monkeypatch.setattr(warm, "moved", fails)
    _, task = run_worktree_task(monkeypatch, add_new_file)
    assert not (Path(task["worktree"]) / ".venv").exists() and (Path(task["worktree"]) / "node_modules").is_dir()


def test_a_clone_is_copy_on_write_or_nothing(tmp_path, monkeypatch):
    source = tmp_path / "node_modules"
    (source / "pkg").mkdir(parents=True)
    (source / "pkg" / "index.js").write_text("x")
    cloned = warm.clone(source, tmp_path / "copy")
    assert cloned == (tmp_path / "copy").exists()
    if warm.platform.MAC:  # APFS
        assert cloned and (tmp_path / "copy/pkg/index.js").read_text() == "x"
    assert not warm.clone(source, tmp_path / "copy")  # never over something there

    def half_done(source: Path, dest: Path) -> bool:
        (dest / "pkg").mkdir(parents=True)
        raise OSError(18, "Cross-device link")

    monkeypatch.setattr(warm, "_clonefile", half_done)
    monkeypatch.setattr(warm, "_reflink", half_done)
    assert not warm.clone(source, tmp_path / "other") and not (tmp_path / "other").exists()


def test_linux_never_falls_back_to_a_full_copy(tmp_path, monkeypatch):
    source = tmp_path / "node_modules"
    (source / "pkg").mkdir(parents=True)
    (source / "pkg" / "index.js").write_text("x")
    ran = []

    def run(argv, **kw):
        ran.append(argv)
        return subprocess.CompletedProcess(argv, 1)  # the filesystem can't reflink

    monkeypatch.setattr(warm.subprocess, "run", run)
    assert not warm._reflink(source, tmp_path / "copy")
    assert ran == [["cp", "--reflink=always", "--", str(source / "pkg" / "index.js"), str(tmp_path / ".copy.reflink")]]
