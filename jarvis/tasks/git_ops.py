"""Runner mixin for git and project-list changes.

  worktree tasks  own worktree + branch jarvis/<id>-<slug>; committed at run end, then merged or discarded
  in-place tasks  user's uncommitted work saved under refs/jarvis/ before each run; changes reported after
                  (non-git folders: a file mtime/size snapshot instead)
  projects        register / git init, serialised by projects_lock
"""

import asyncio
import logging
import os
import re
import time
from pathlib import Path

from jarvis.core import proc
from jarvis.core.config import WORKTREES_DIR
from jarvis.core.database import ACTIVE
from jarvis.safety import remembered
from jarvis.state import activity as bell
from jarvis.tasks import merge_check, projects
from jarvis.tasks.approvals import session_key
from jarvis.tasks.host_tools import few
from jarvis.tasks.runner_base import RunnerBase

log = logging.getLogger("jarvis.tasks.runner")
# Excluded from task commits even if .gitignore misses them
JUNK = [*(f"{d}/" for d in projects.CACHE_DIRS), "*.pyc", ".DS_Store"]
PATHSPEC = ["--", ".", *(f":(exclude,glob)**/{p}" for p in JUNK)]
# Skipped when snapshotting a non-git folder
SNAPSHOT_SKIP = {".git", *projects.CACHE_DIRS}


async def _git(cwd: str | Path, *args: str, timeout: float = 60) -> str:
    return await proc.run("git", "-C", str(cwd), *args, timeout=timeout)


async def _git_try(cwd: str | Path, *args: str, timeout: float = 60) -> tuple[int, str]:
    """Run git, tolerating failure: (exit code, stdout+stderr)."""
    return await proc.run_try("git", "-C", str(cwd), *args, timeout=timeout)


def _snapshot(folder: Path) -> dict[str, tuple[int, int]]:
    """relpath -> (mtime_ns, size) for every file, to diff a non-git folder."""
    files = {}
    for top, dirs, names in os.walk(folder):
        dirs[:] = [d for d in dirs if d not in SNAPSHOT_SKIP]
        for name in names:
            if name == ".DS_Store":
                continue
            try:
                st = os.lstat(os.path.join(top, name))
            except OSError:
                continue
            files[os.path.relpath(os.path.join(top, name), folder)] = (st.st_mtime_ns, st.st_size)
    return files


def _file_changes(before: dict, after: dict) -> str:
    groups = [("created", sorted(after.keys() - before.keys())),
              ("modified", sorted(k for k in after.keys() & before.keys() if after[k] != before[k])),
              ("deleted", sorted(before.keys() - after.keys()))]
    return "not a git repo; " + ("; ".join(f"{label} {few(names)}" for label, names in groups if names) or "no changes")


class GitOps(RunnerBase):
    """Worktrees, in-place folders, commits, merges, discards and project registration."""

    async def register_project(self, name: str, path: str | None = None, aliases: list[str] | None = None,
                               about: str = "", in_place: bool = projects.IN_PLACE_DEFAULT) -> dict:
        async with self.projects_lock:
            return await asyncio.to_thread(projects.register, name, path, aliases, about, in_place)

    async def merge(self, task_id: int) -> dict:
        """Merge a worktree task's branch into the project's checked-out branch, then remove worktree and branch.

        An already-merged branch is only cleaned up."""
        task = self._get(task_id)
        if task["status"] == "discarded":
            raise ValueError(f"Task {task_id} was discarded, so there's nothing to merge.")
        if task["kind"] != "code_change" or task["in_place"] or not task["branch"]:
            raise ValueError(f"Task {task_id} has no branch of its own to merge; in-place changes are already in the project's folder.")
        if task_id in self.jobs or task["status"] in ACTIVE:
            raise ValueError(f"Task {task_id} isn't finished yet; merge it once it is.")
        async with self.projects_lock:
            project = projects.find(task["project"])
            if busy := self._jobs_on(project.name, in_place=True):
                raise ValueError(f"Task {' and '.join(map(str, busy))} is working in {project.name}'s folder; merge once it's finished.")
            async with self._folder_lock(project.name):  # in-place tasks wait for the merge
                did, cleaned = await self._merge(task, project)
        spoken = f"Task {task_id}, {task['title']}, {did}. {cleaned}."
        self.store.log(task_id, "merged", spoken)
        bell.add(spoken, "ok", task_id)
        if task["node"]:  # its copy on the machine has served its purpose
            await self.offload.forget(task["node"], self.offload.key(task_id))
        return self.store.update(task_id, spoken=spoken)

    async def _merge(self, task: dict, project: projects.Project) -> tuple[str, str]:
        """Do the merge; returns (what it did, what it cleaned up)."""
        task_id = task["id"]
        repo, branch, wt = project.path, task["branch"], task["worktree"]
        if wt and Path(wt).exists() and await _git(wt, "status", "--porcelain", *PATHSPEC):
            raise ValueError(f"Task {task_id}'s worktree has changes that aren't committed to {branch}, so it wasn't merged.")
        into = await _git(repo, "rev-parse", "--abbrev-ref", "HEAD")
        if into == "HEAD":
            raise ValueError(f"{project.name}'s folder has no branch checked out, so there's nothing to merge into.")
        exists = (await _git_try(repo, "rev-parse", "--verify", "-q", f"refs/heads/{branch}"))[0] == 0
        if not exists:
            did = f"its branch {branch} was already gone"
        elif (await _git_try(repo, "merge-base", "--is-ancestor", branch, "HEAD"))[0] == 0:
            did = f"{branch} was already merged into {into}"
        else:
            if await _git(repo, "status", "--porcelain", "--untracked-files=no"):
                raise ValueError(f"{project.name}'s folder has uncommitted changes; commit or put them aside before merging.")
            # JARVIS's own branches merge only once their tests pass
            tested = projects.is_jarvis(repo)
            if tested and (why := await merge_check.refusal(repo, branch, wt, into)):
                raise ValueError(f"Task {task_id}'s branch {branch} wasn't merged, since {why}.")
            code, out = await _git_try(repo, "merge", "--no-ff", "--no-edit", branch, timeout=300)
            if code:
                await _git_try(repo, "merge", "--abort")
                raise ValueError(f"Merging {branch} into {into} failed, so nothing changed and the branch is kept: {out}"[:400])
            did = f"merged into {into}" + (", its tests passing" if tested else "")
        return did, await self._remove_branch(repo, wt, branch if exists else None)

    async def discard(self, task_id: int) -> dict:
        """Mark an ended task discarded, removing a worktree task's worktree and branch.

        In-place changes are left untouched: they're mixed in with the user's own."""
        task = self._get(task_id)
        if task["status"] == "discarded":
            raise ValueError(f"Task {task_id} was already discarded.")
        if task_id in self.jobs or task["status"] in ACTIVE:
            raise ValueError(f"Task {task_id} is {task['status']}; stop it first, then discard it.")
        if task["kind"] != "code_change":
            did = "It changed no files, so there was nothing to remove"
        elif task["in_place"]:
            folder = f"{task['project']}'s folder"
            if not task["worktree"] or (task["changes"] or "").endswith("no changes"):
                did = f"It left no changes in {folder}"
            else:
                did = (f"Its changes in {folder} ({task['changes']}) are still there: they're uncommitted and mixed in "
                       "with anything else there, so review or undo them by hand")
        elif not task["branch"]:
            did = "It never got a branch, so there was nothing to remove"
        else:
            async with self.projects_lock:
                did = await self._discard_branch(task, projects.find(task["project"]))
        spoken = f"Task {task_id}, {task['title']}, discarded without merging. {did}."
        self.store.log(task_id, "discarded", spoken)
        bell.add(spoken, "warn", task_id)
        self._close_approvals(task_id, "discarded")
        remembered.end_session(session_key(task_id))
        if task["node"]:
            await self.offload.forget(task["node"], self.offload.key(task_id))
        return self.store.update(task_id, status="discarded", spoken=spoken, activity=None, question=None,
                                 wait_kind=None, seen=1)

    async def _discard_branch(self, task: dict, project: projects.Project) -> str:
        """Remove a worktree task's worktree and branch; ValueError if not JARVIS's branch or already merged."""
        repo, branch, wt = project.path, task["branch"], task["worktree"]
        if wt and Path(wt).resolve() == repo.resolve():
            raise ValueError(f"Task {task['id']}'s worktree is {project.name}'s own folder, so nothing was removed.")
        if not branch.startswith(f"jarvis/{task['id']}-"):  # only the branch the runner made
            raise ValueError(f"Task {task['id']}'s branch {branch} wasn't made by JARVIS, so nothing was removed.")
        exists = (await _git_try(repo, "rev-parse", "--verify", "-q", f"refs/heads/{branch}"))[0] == 0
        if exists and await _git(repo, "rev-list", "--count", f"{task['base']}..{branch}") != "0" \
                and (await _git_try(repo, "merge-base", "--is-ancestor", branch, "HEAD"))[0] == 0:
            raise ValueError(f"Task {task['id']}'s branch {branch} is already merged, so discarding it wouldn't undo "
                             "anything; task_merge just cleans it up.")
        return await self._remove_branch(repo, wt, branch if exists else None, force=True)

    async def _remove_branch(self, repo: Path, worktree: str | None, branch: str | None, force: bool = False) -> str:
        """Remove worktree and branch. `force` (discard) also drops unmerged commits; returns what was done."""
        done = []
        if worktree and Path(worktree).exists():
            await _git(repo, "worktree", "remove", "--force", worktree)  # --force: only caches like node_modules remain
            done.append("worktree removed")
        await _git_try(repo, "worktree", "prune")
        if branch:
            code, out = await _git_try(repo, "branch", "-D" if force else "-d", branch)
            done.append("branch deleted" if code == 0 else f"branch kept: {out}"[:200])
        return (" and ".join(done) or "nothing left to clean up").capitalize()

    async def init_git(self, name: str) -> dict:
        async with self.projects_lock:
            project = projects.find(name)
            if busy := self._jobs_on(project.name):
                raise ValueError(f"Task {' and '.join(map(str, busy))} is working on {project.name}; "
                                 "set up git once it's finished.")
            async with self._folder_lock(project.name):  # in-place tasks wait for git init
                return await asyncio.to_thread(projects.init_git, project.name)

    def _jobs_on(self, project: str, in_place: bool = False) -> list[int]:
        """Running or queued task ids on a project (only in-place ones if `in_place`)."""
        tasks = (self.store.get(i) for i in self.jobs)
        return sorted(t["id"] for t in tasks if t and t["project"] == project and (t["in_place"] or not in_place))

    def _folder_lock(self, project: str) -> asyncio.Lock:
        """Per-project lock held by anything changing its folder: in-place tasks, merges, git init."""
        return self.folders.setdefault(project, asyncio.Lock())

    async def _make_worktree(self, task: dict) -> None:
        project = projects.find(task["project"])
        if problem := await asyncio.to_thread(projects.worktree_problem, project):
            raise ValueError(problem)
        slug = re.sub(r"[^a-z0-9]+", "-", task["title"].lower()).strip("-")[:40] or "task"
        branch = f"jarvis/{task['id']}-{slug}"
        folder = re.sub(r"[^\w.-]", "-", project.name)
        worktree = WORKTREES_DIR / f"{folder}-{task['id']}"
        base, base_ref = (await _git(project.path, "rev-parse", "HEAD", "--abbrev-ref", "HEAD")).split("\n")
        WORKTREES_DIR.mkdir(exist_ok=True)
        await _git(project.path, "worktree", "add", "-q", "-b", branch, str(worktree), base, timeout=300)
        self.store.set(task["id"], worktree=str(worktree), branch=branch, base=base)
        self.store.log(task["id"], "branch", f"{branch} from {base_ref} at {base[:7]}, in {worktree}")

    async def _enter_folder(self, task: dict) -> None:
        """Before each in-place run: save uncommitted work to refs/jarvis/; on the first run record branch and base.

        Non-git folders get a file snapshot (no base); repos with no commits use git's empty tree as base."""
        folder = Path(task["worktree"]) if task["worktree"] else projects.find(task["project"]).path
        state = await asyncio.to_thread(projects.git_state, folder)
        if state == projects.NO_GIT:
            if task["id"] not in self.snapshots:
                self.snapshots[task["id"]] = await asyncio.to_thread(_snapshot, folder)
            if not task["worktree"]:
                self.store.set(task["id"], worktree=str(folder), branch=None, base=None)
                self.store.log(task["id"], "in place", f"working in {folder}, which isn't a git repo; "
                                                       "the files it changes are listed when it ends")
            return
        if state == projects.NO_COMMITS:
            branch = await _git(folder, "symbolic-ref", "--short", "-q", "HEAD")
            base, at = await _git(folder, "hash-object", "-t", "tree", "/dev/null"), "with no commits yet"
        else:
            head, branch = (await _git(folder, "rev-parse", "HEAD", "--abbrev-ref", "HEAD")).split("\n")
            # stash create: commit of tracked changes without touching files, index or stash list
            saved = await _git(folder, "stash", "create", f"JARVIS: uncommitted work before task {task['id']}")
            if saved:
                ref = f"refs/jarvis/task-{task['id']}-{int(time.time())}"
                await _git(folder, "update-ref", ref, saved)
                self.store.log(task["id"], "saved", f"uncommitted changes in {folder} saved to {ref} before this run")
            base, at = saved or head, f"at {head[:7]}"
        if not task["worktree"]:
            branch = None if branch == "HEAD" else branch  # HEAD means detached
            self.store.set(task["id"], worktree=str(folder), branch=branch, base=base)
            self.store.log(task["id"], "in place", f"working in {folder} on {branch or 'a detached HEAD'} {at}, "
                                                   "without a worktree; changes stay uncommitted")

    async def _uncommitted(self, task: dict) -> str:
        """Summary of an in-place task's uncommitted changes since it began."""
        folder = task["worktree"]
        if not Path(folder).exists():
            return "project folder missing"
        try:
            if not task["base"]:  # wasn't a git repo at start
                if (before := self.snapshots.get(task["id"])) is None:
                    return "not a git repo; its files weren't recorded before this run, so the changes aren't known"
                return _file_changes(before, await asyncio.to_thread(_snapshot, Path(folder)))
            parts = [await _git(folder, "diff", "--shortstat", task["base"])]
            if untracked := len((await _git(folder, "ls-files", "--others", "--exclude-standard")).splitlines()):
                parts.append(f"{untracked} untracked file{'s' if untracked > 1 else ''} in the folder")
            return "uncommitted: " + ("; ".join(p for p in parts if p) or "no changes")
        except Exception as e:
            log.exception("couldn't read changes for task %s", task["id"])
            return f"couldn't read changes: {e}"[:200]

    async def _commit(self, task: dict, summary: str, finished: bool) -> str:
        """Commit the run's changes; returns the branch's shortstat against its base."""
        wt = task["worktree"]
        if not Path(wt).exists():
            return "worktree missing"
        try:
            await _git(wt, "add", "-A", *PATHSPEC)
            if await _git(wt, "diff", "--cached", "--name-only"):
                subject = task["title"] if finished else f"{task['title']} (unfinished)"
                message = f"{subject}\n\n{summary[:3000]}".strip() + f"\n\nJARVIS-Task: {task['id']}"
                # --no-verify: hooks often need tools a fresh worktree lacks; the branch is reviewed anyway
                await _git(wt, "commit", "-q", "--no-verify", "-m", message)
            return await _git(wt, "diff", "--shortstat", task["base"], "HEAD") or "no changes"
        except Exception as e:
            log.exception("commit failed for task %s", task["id"])
            return f"couldn't commit: {e}"[:200]
