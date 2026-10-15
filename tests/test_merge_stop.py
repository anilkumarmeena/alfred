"""Tests for stopping a merge under way (Runner.halt -> stop_merge): during its tests, mid-squash, too late to, and
for a project on another machine. A stopped merge leaves the project's folder as it was and the task's branch kept."""

import asyncio
import os
import sys
from pathlib import Path

import pytest
from test_in_place import add_new_file, branches, env, git, jarvis, run_worktree_task  # noqa: F401  (fixtures)
from test_merge_check import suite  # noqa: F401  (fixture)
from test_node_offload import World, finished, world  # noqa: F401  (world is a fixture)
from test_node_projects import registered, there  # noqa: F401  (there is a fixture)

from jarvis.core import proc
from jarvis.state import activity as bell
from jarvis.tasks import git_ops, merge_check
from jarvis.tasks.worker import Outcome


def as_it_was(repo: Path, head: str, branch: str) -> None:
    assert git(repo, "rev-parse", "HEAD") == head and git(repo, "status", "--porcelain") == ""
    assert not any((repo / ".git" / f).exists() for f in ("MERGE_HEAD", "SQUASH_MSG"))
    assert not (repo / "new.py").exists() and branch in branches(repo)


async def stop_when(r, task_id: int, reached: asyncio.Event) -> tuple[dict, str]:
    """Merge the task, stop it once `reached` is set; (what the stop returned, how the merge ended)."""
    merging = asyncio.create_task(r.merge(task_id))
    await reached.wait()
    stopped = await r.halt(task_id)
    try:
        ended = (await merging)["spoken"]
    except ValueError as e:
        ended = str(e)
    assert r.merges == {}
    return stopped, ended


def test_stopping_a_merge_during_its_tests_stops_them_and_leaves_the_repo_as_it_was(jarvis, suite, monkeypatch,
                                                                                    env):
    r, task = run_worktree_task(monkeypatch, add_new_file, project="jarvis")
    git(jarvis, "worktree", "remove", "--force", task["worktree"])  # so the tests run in a checkout made for them
    head = git(jarvis, "rev-parse", "HEAD")

    async def main():
        testing = asyncio.Event()

        async def failures(folder, venv, tests=()):
            testing.set()
            await asyncio.sleep(3600)

        monkeypatch.setattr(merge_check, "failures", failures)
        return await stop_when(r, task["id"], testing)

    stopped, ended = asyncio.run(main())
    said = f"Task {task['id']}, add a thing, wasn't merged: its merge was stopped, so nothing changed and its branch is kept."
    assert stopped["spoken"] == ended == said
    as_it_was(jarvis, head, task["branch"])
    assert "merge-check" not in git(jarvis, "worktree", "list")
    assert list((env["tmp"] / "worktrees").glob("merge-check-*")) == []
    assert "merge stopped" in [e["kind"] for e in r.store.history(task["id"])]
    assert bell.recent()[0]["text"] == said


def slow(monkeypatch, step: str, reached: asyncio.Event) -> None:
    """Make git_ops's `step` (the squash's "merge" or "commit", or "clean" for the branch's removal) set `reached` and
    take a moment."""
    if step in ("merge", "commit"):
        real = git_ops._git_try

        async def git_slowly(cwd, *args, timeout=60):
            if args[0] != step:
                return await real(cwd, *args, timeout=timeout)
            reached.set()
            out = await real(cwd, *args, timeout=timeout)
            await asyncio.sleep(0.2)  # the stop lands while git is at it
            return out

        monkeypatch.setattr(git_ops, "_git_try", git_slowly)
    else:
        real_clean = git_ops._remove_branch

        async def clean_slowly(*args, **kwargs):
            reached.set()
            await asyncio.sleep(0.2)
            return await real_clean(*args, **kwargs)

        monkeypatch.setattr(git_ops, "_remove_branch", clean_slowly)


@pytest.mark.parametrize("step", ["merge", "commit"])
def test_a_stop_while_git_squashes_lets_it_end_then_undoes_it(env, monkeypatch, step):
    other = env["other"]
    r, task = run_worktree_task(monkeypatch, add_new_file)
    head = git(other, "rev-parse", "HEAD")

    async def main():
        merging = asyncio.Event()
        slow(monkeypatch, step, merging)
        return await stop_when(r, task["id"], merging)

    stopped, ended = asyncio.run(main())
    assert "wasn't merged: its merge was stopped" in stopped["spoken"] and ended == stopped["spoken"]
    as_it_was(other, head, task["branch"])
    assert Path(task["worktree"]).exists()


def test_a_stop_once_merged_is_too_late_and_the_merge_finishes(env, monkeypatch):
    other = env["other"]
    r, task = run_worktree_task(monkeypatch, add_new_file)

    async def main():
        cleaning = asyncio.Event()
        slow(monkeypatch, "clean", cleaning)
        return await stop_when(r, task["id"], cleaning)

    stopped, ended = asyncio.run(main())
    assert "merged into main as one commit" in stopped["spoken"] and ended == stopped["spoken"]
    assert (other / "new.py").exists() and branches(other) == ["main"] and not Path(task["worktree"]).exists()


def test_a_task_with_no_merge_under_way_is_stopped_as_before(env, monkeypatch):
    r, task = run_worktree_task(monkeypatch, add_new_file)
    with pytest.raises(ValueError, match="isn't running"):
        asyncio.run(r.halt(task["id"]))


def test_stopping_a_merge_of_a_project_on_another_machine_stops_it_there(there, tmp_path, monkeypatch):  # noqa: F811
    async def run(task, prompt, reporter) -> Outcome:
        (Path(task["worktree"]) / "new.py").write_text("x = 1\n")
        return Outcome("Added.", "", 0.01)

    async def main():
        w = await World(tmp_path, run).up()
        await registered(w, there, in_place=False)
        task = w.r.start("code_change", "add a thing", "Add new.py", "film")
        done = await finished(w.r, task["id"])
        head = git(there, "rev-parse", "HEAD")
        merging = asyncio.Event()
        slow(monkeypatch, "merge", merging)  # the machine's merge runs in this process too
        stopped, ended = await stop_when(w.r, task["id"], merging)
        merges_there = dict(w.work.merges)
        await w.down()
        return done, head, stopped, ended, merges_there

    done, head, stopped, ended, merges_there = asyncio.run(main())
    assert "wasn't merged: its merge was stopped" in stopped["spoken"] and ended == stopped["spoken"]
    as_it_was(there, head, done["branch"])
    assert merges_there == {}


# ── Killing what a stopped step ran ─────────────────────────────


@pytest.mark.skipif(sys.platform == "win32", reason="process groups")
def test_a_stopped_run_kills_everything_it_started(tmp_path):
    child = tmp_path / "child.pid"

    async def main():
        running = asyncio.create_task(proc.run_try("sh", "-c", f"sleep 60 & echo $! > {child}; wait", timeout=60,
                                                   group=True))
        while not child.exists() or not child.read_text().strip():
            await asyncio.sleep(0.02)
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running

    asyncio.run(main())
    pid = int(child.read_text())
    for _ in range(100):  # reaped by init once its parent is gone
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        asyncio.run(asyncio.sleep(0.02))
    raise AssertionError("the child of a stopped run is still running")
