"""Runner mixin for git and project-list changes.

  worktree tasks  own worktree + branch jarvis/<id>-<slug>; committed at run end, then merged or discarded; a
                  merge under way stops cleanly (halt), leaving the folder as it was
  in-place tasks  user's uncommitted work saved under refs/jarvis/ before each run; changes reported after
                  (non-git folders: a file mtime/size snapshot instead)
  projects        register / git init, serialised by projects_lock

The git steps themselves are OPS, functions of folder paths: for a project that lives on another machine
(jarvis.node) they run there, sent over its link, and here otherwise.
"""

import asyncio
import contextlib
import logging
import os
import re
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

from jarvis.core import proc
from jarvis.core.config import ASSISTANT_NAME, WORKTREES_DIR
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
SQUASH_BODY = 1200  # characters of a task's summary kept in its squashed commit
MERGE_STOP_SECONDS = 60  # a stopped merge cleans up (its test run, a temporary checkout) before saying how it ended


async def _git(cwd: str | Path, *args: str, timeout: float = 60) -> str:
    return await proc.run("git", "-C", str(cwd), *args, timeout=timeout)


async def _git_try(cwd: str | Path, *args: str, timeout: float = 60) -> tuple[int, str]:
    """Run git, tolerating failure: (exit code, stdout+stderr)."""
    return await proc.run_try("git", "-C", str(cwd), *args, timeout=timeout)


def _snapshot(folder: Path) -> dict[str, list[int]]:
    """relpath -> [mtime_ns, size] for every file, to diff a non-git folder."""
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
            files[os.path.relpath(os.path.join(top, name), folder)] = [st.st_mtime_ns, st.st_size]
    return files


def _file_changes(before: dict, after: dict) -> str:
    groups = [("created", sorted(after.keys() - before.keys())),
              ("modified", sorted(k for k in after.keys() & before.keys() if after[k] != before[k])),
              ("deleted", sorted(before.keys() - after.keys()))]
    return "not a git repo; " + ("; ".join(f"{label} {few(names)}" for label, names in groups if names) or "no changes")


# ── The git steps, by folder path (here, or on the machine a project lives on) ──


async def worktree_op(body: dict) -> dict:
    """{"repo", "task", "title", "name"}: a worktree and branch for the task, from the repo's HEAD."""
    repo = Path(body["repo"])
    if problem := await asyncio.to_thread(projects.worktree_problem, projects.Project(body["name"], repo, [], "")):
        raise ValueError(problem)
    slug = re.sub(r"[^a-z0-9]+", "-", str(body["title"]).lower()).strip("-")[:40] or "task"
    branch = f"jarvis/{body['task']}-{slug}"
    worktree = WORKTREES_DIR / f"{re.sub(r'[^\w.-]', '-', body['name'])}-{body['task']}"
    base, base_ref = (await _git(repo, "rev-parse", "HEAD", "--abbrev-ref", "HEAD")).split("\n")
    WORKTREES_DIR.mkdir(parents=True, exist_ok=True)
    await _git(repo, "worktree", "add", "-q", "-b", branch, str(worktree), base, timeout=300)
    return {"worktree": str(worktree), "branch": branch, "base": base, "base_ref": base_ref}


async def enter_op(body: dict) -> dict:
    """{"folder", "task", "snapshot": bool}: before an in-place run, save uncommitted work under refs/jarvis/.

    Returns {"branch", "base", "at", "saved"} for a git folder; {"snapshot"} (when asked) for one without git. Repos
    with no commits use git's empty tree as base."""
    folder, task_id = Path(body["folder"]), body["task"]
    state = await asyncio.to_thread(projects.git_state, folder)
    if state == projects.NO_GIT:
        return {"git": False} | ({"snapshot": await asyncio.to_thread(_snapshot, folder)} if body.get("snapshot") else {})
    saved = ""
    if state == projects.NO_COMMITS:
        branch = await _git(folder, "symbolic-ref", "--short", "-q", "HEAD")
        base, at = await _git(folder, "hash-object", "-t", "tree", "/dev/null"), "with no commits yet"
    else:
        head, branch = (await _git(folder, "rev-parse", "HEAD", "--abbrev-ref", "HEAD")).split("\n")
        # stash create: commit of tracked changes without touching files, index or stash list
        if stashed := await _git(folder, "stash", "create", f"JARVIS: uncommitted work before task {task_id}"):
            saved = f"refs/jarvis/task-{task_id}-{int(time.time())}"
            await _git(folder, "update-ref", saved, stashed)
        base, at = stashed or head, f"at {head[:7]}"
    return {"git": True, "branch": None if branch == "HEAD" else branch, "base": base, "at": at, "saved": saved}


async def changes_op(body: dict) -> dict:
    """{"folder", "base", "snapshot"}: an in-place task's uncommitted changes since it began, as said."""
    folder = Path(body["folder"])
    if not folder.exists():
        return {"said": "project folder missing"}
    if not body.get("base"):  # wasn't a git repo at start
        if (before := body.get("snapshot")) is None:
            return {"said": "not a git repo; its files weren't recorded before this run, so the changes aren't known"}
        return {"said": _file_changes(before, await asyncio.to_thread(_snapshot, folder))}
    parts = [await _git(folder, "diff", "--shortstat", body["base"])]
    if untracked := len((await _git(folder, "ls-files", "--others", "--exclude-standard")).splitlines()):
        parts.append(f"{untracked} untracked file{'s' if untracked > 1 else ''} in the folder")
    changed = "; ".join(p for p in parts if p) or "no changes"
    # After a commit the user ordered, the diff from base spans it too. Any commit counts, theirs as well: say so plainly.
    code, made = await _git_try(folder, "rev-list", "--count", f"{body['base']}..HEAD")
    if code:  # base is the empty tree of a repo that had no commits: every commit is new
        code, made = await _git_try(folder, "rev-list", "--count", "HEAD")
    if code == 0 and made.strip().isdigit() and (n := int(made)):
        return {"said": f"{n} new commit{'s' if n > 1 else ''} since it began; changed since then: {changed}"}
    return {"said": "uncommitted: " + changed}


async def commit_op(body: dict) -> dict:
    """{"worktree", "base", "title", "summary", "finished", "task"}: commit the run's changes; returns the branch's
    shortstat against its base."""
    wt = body["worktree"]
    if not Path(wt).exists():
        return {"said": "worktree missing"}
    await _git(wt, "add", "-A", *PATHSPEC)
    if await _git(wt, "diff", "--cached", "--name-only"):
        subject = body["title"] if body["finished"] else f"{body['title']} (unfinished)"
        message = f"{subject}\n\n{str(body['summary'])[:3000]}".strip() + f"\n\nJARVIS-Task: {body['task']}"
        # --no-verify: hooks often need tools a fresh worktree lacks; the branch is reviewed anyway
        await _git(wt, "commit", "-q", "--no-verify", "-m", message)
    return {"said": await _git(wt, "diff", "--shortstat", body["base"], "HEAD") or "no changes"}


def squash_message(title: str, summary: str, task: int) -> str:
    """The one commit a task's branch becomes: its title, the opening of its summary (whole paragraphs up to
    SQUASH_BODY characters), and trailers ending with the assistant as co-author."""
    body = ""
    for para in re.split(r"\n\s*\n", summary.strip()):
        if body and len(body) + 2 + len(para) > SQUASH_BODY:
            break
        body += f"\n\n{para}" if body else para
    if len(body) > SQUASH_BODY:  # a single long opening paragraph: cut at a word
        body = body[:SQUASH_BODY].rsplit(" ", 1)[0] + "…"
    return "\n\n".join(p for p in (" ".join(title.split()) or f"Task {task}", body,
                                   f"JARVIS-Task: {task}\nCo-Authored-By: {ASSISTANT_NAME}") if p)


async def merge_op(body: dict) -> dict:
    """{"repo", "branch", "worktree", "task", "name", "title", "summary"}: squash the branch into one commit on what's
    checked out, then remove it. Returns {"did", "cleaned"}; ValueError (nothing changed) if it can't.

    Cancelling it stops the merge: its tests stop, a squash under way is undone, and CancelledError comes out with the
    folder as it was and the branch kept. Once merged, it's too late: the cleanup finishes and it returns as usual."""
    task_id, name = body["task"], body["name"]
    repo, branch, wt = Path(body["repo"]), body["branch"], body.get("worktree")
    if wt and Path(wt).exists() and await _git(wt, "status", "--porcelain", *PATHSPEC):
        raise ValueError(f"Task {task_id}'s worktree has changes that aren't committed to {branch}, so it wasn't merged.")
    into = await _git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    if into == "HEAD":
        raise ValueError(f"{name}'s folder has no branch checked out, so there's nothing to merge into.")
    exists = (await _git_try(repo, "rev-parse", "--verify", "-q", f"refs/heads/{branch}"))[0] == 0
    if not exists:
        did = f"its branch {branch} was already gone"
    elif (await _git_try(repo, "merge-base", "--is-ancestor", branch, "HEAD"))[0] == 0:
        did = f"{branch} was already merged into {into}"
    else:
        if await _git(repo, "status", "--porcelain", "--untracked-files=no"):
            raise ValueError(f"{name}'s folder has uncommitted changes; commit or put them aside before merging.")
        # JARVIS's own branches merge only once their tests pass
        tested = projects.is_jarvis(repo)
        if tested and (why := await merge_check.refusal(repo, branch, wt, into)):
            raise ValueError(f"Task {task_id}'s branch {branch} wasn't merged, since {why}.")
        message = squash_message(body.get("title") or "", body.get("summary") or "", task_id)
        before = await _git(repo, "rev-parse", "HEAD")
        squashing = asyncio.ensure_future(_squash(repo, branch, into, message))
        try:
            squashed = await asyncio.shield(squashing)
        except asyncio.CancelledError:  # killing git mid-squash could leave the folder half-merged: let it end, undo it
            with contextlib.suppress(ValueError):  # it failed, and put the folder back itself
                if await squashing:
                    await _git_try(repo, "reset", "-q", "--merge", before)
            raise
        if squashed:
            did = f"merged into {into} as one commit" + (", its tests passing" if tested else "")
        else:
            did = f"{branch} had no changes that weren't already in {into}"
    cleaning = asyncio.ensure_future(_clean_up(repo, wt, branch if exists else None))
    try:
        cleaned = await asyncio.shield(cleaning)
    except asyncio.CancelledError:  # merged already: too late to stop, so it finishes
        cleaned = await cleaning
        if task := asyncio.current_task():
            task.uncancel()
    return {"did": did, "cleaned": cleaned}


async def _clean_up(repo: Path, worktree: str | None, branch: str | None) -> str:
    """_remove_branch after a merge, which is done whatever happens here, so a failure is only reported."""
    try:
        return await _remove_branch(repo, worktree, branch)
    except RuntimeError as e:
        return f"Its worktree and branch weren't removed: {e}"[:300]


async def _squash(repo: Path, branch: str, into: str, message: str) -> bool:
    """Squash `branch` into one commit on `into` (checked out, clean); False if it had nothing to add. ValueError
    if it can't, with `into` put back as it was."""
    try:
        # --ff: a merge.ff=false or =only setting would otherwise refuse or fail any squash
        code, out = await _git_try(repo, "merge", "--squash", "--ff", branch, timeout=300)
        if not code:
            if (await _git_try(repo, "diff", "--cached", "--quiet"))[0] == 0:
                await _git_try(repo, "reset", "-q", "--merge")  # drops the SQUASH_MSG git left for a commit by hand
                return False
            # --no-verify: no pre-commit hook, as for a merge commit (and in commit_op); --cleanup=whitespace keeps "#"
            # lines, such as markdown headings in the summary
            code, out = await _git_try(repo, "commit", "-q", "--no-verify", "--cleanup=whitespace", "-m", message,
                                       timeout=300)
            if not code:
                return True
    except BaseException:  # a timeout or cancellation mid-squash: don't leave it staged
        await _git_try(repo, "reset", "-q", "--merge")
        raise
    undone = (await _git_try(repo, "reset", "-q", "--merge"))[0] == 0
    state = "nothing changed" if undone else f"{into} may still have its changes staged"
    raise ValueError(f"Merging {branch} into {into} failed, so {state} and the branch is kept: {out}"[:400])


async def discard_op(body: dict) -> dict:
    """{"repo", "branch", "worktree", "base", "task", "name"}: remove a worktree task's worktree and branch.
    ValueError if it isn't JARVIS's branch or is already merged."""
    task_id, repo, branch, wt = body["task"], Path(body["repo"]), body["branch"], body.get("worktree")
    if wt and Path(wt).resolve() == repo.resolve():
        raise ValueError(f"Task {task_id}'s worktree is {body['name']}'s own folder, so nothing was removed.")
    if not branch.startswith(f"jarvis/{task_id}-"):  # only the branch the runner made
        raise ValueError(f"Task {task_id}'s branch {branch} wasn't made by JARVIS, so nothing was removed.")
    exists = (await _git_try(repo, "rev-parse", "--verify", "-q", f"refs/heads/{branch}"))[0] == 0
    if exists and await _git(repo, "rev-list", "--count", f"{body['base']}..{branch}") != "0" \
            and (await _git_try(repo, "merge-base", "--is-ancestor", branch, "HEAD"))[0] == 0:
        raise ValueError(f"Task {task_id}'s branch {branch} is already merged, so discarding it wouldn't undo "
                         "anything; task_merge just cleans it up.")
    return {"did": await _remove_branch(repo, wt, branch if exists else None)}


async def _remove_branch(repo: Path, worktree: str | None, branch: str | None) -> str:
    """Remove worktree and branch, whose work is merged or being discarded; returns what was done."""
    done = []
    if worktree and Path(worktree).exists():
        await _git(repo, "worktree", "remove", "--force", worktree)  # --force: only caches like node_modules remain
        done.append("worktree removed")
    await _git_try(repo, "worktree", "prune")
    if branch:  # -D: a squashed branch isn't an ancestor of HEAD, so -d would refuse it
        code, out = await _git_try(repo, "branch", "-D", branch)
        done.append("branch deleted" if code == 0 else f"branch kept: {out}"[:200])
    said = " and ".join(done) or "nothing left to clean up"
    return said[0].upper() + said[1:]  # not capitalize(): it would lowercase git's own words in "branch kept: …"


OPS: dict[str, Callable[[dict], Awaitable[dict]]] = {
    "worktree": worktree_op, "enter": enter_op, "changes": changes_op, "commit": commit_op, "merge": merge_op,
    "discard": discard_op}


class GitOps(RunnerBase):
    """Worktrees, in-place folders, commits, merges, discards and project registration."""

    async def _op(self, node: int | None, op: str, body: dict) -> dict:
        """A git step here, or on `node` when the project lives there (its errors come back as ValueError)."""
        if node is None:
            return await OPS[op](body)
        return await self.offload.folder_op(node, op, body)

    async def register_project(self, name: str, path: str | None = None, aliases: list[str] | None = None,
                               about: str = "", in_place: bool = projects.IN_PLACE_DEFAULT, machine: str = "") -> dict:
        """Add a project; with `machine`, one that lives on that machine, whose folder it checks (and makes)."""
        if machine:
            return await self.offload.register_there(name, path, aliases, about, in_place, machine)
        async with self.projects_lock:
            return await asyncio.to_thread(projects.register, name, path, aliases, about, in_place)

    async def merge(self, task_id: int) -> dict:
        """Merge a worktree task's branch into the project's checked-out branch, then remove worktree and branch.

        An already-merged branch is only cleaned up. While it runs, stop_merge stops it: ValueError then says so."""
        task = self._get(task_id)
        if task["status"] == "discarded":
            raise ValueError(f"Task {task_id} was discarded, so there's nothing to merge.")
        if task["kind"] != "code_change" or task["in_place"] or not task["branch"]:
            raise ValueError(f"Task {task_id} has no branch of its own to merge; in-place changes are already in the project's folder.")
        if task_id in self.jobs or task["status"] in ACTIVE:
            raise ValueError(f"Task {task_id} isn't finished yet; merge it once it is.")
        if task_id in self.merges:
            raise ValueError(f"Task {task_id} is being merged already.")
        merging = self.merges[task_id] = asyncio.ensure_future(self._merge(task))
        try:
            merged, project = await merging
        except asyncio.CancelledError:
            if (me := asyncio.current_task()) and me.cancelling():  # the runner is stopping, not the merge
                raise
            raise ValueError(self._get(task_id)["spoken"]) from None
        finally:
            self.merges.pop(task_id, None)
        await self._forget_copy(task, project)
        return merged

    async def _merge(self, task: dict) -> tuple[dict, projects.Project]:
        """The merge itself; its outcome is recorded by the time it ends, stopped or not."""
        task_id = task["id"]
        try:
            async with self.projects_lock:
                project = projects.find(task["project"])
                if busy := self._jobs_on(project.name, in_place=True):
                    raise ValueError(f"Task {' and '.join(map(str, busy))} is working in {project.name}'s folder; merge once it's finished.")
                async with self._folder_lock(project.name):  # in-place tasks wait for the merge
                    done = await self._op(project.node, "merge", {"repo": str(project.path), "branch": task["branch"],
                                                                  "worktree": task["worktree"], "task": task_id,
                                                                  "name": project.name, "title": task["title"],
                                                                  "summary": task["summary"]})
        except asyncio.CancelledError:
            spoken = (f"Task {task_id}, {task['title']}, wasn't merged: its merge was stopped, so nothing changed and "
                      "its branch is kept.")
            self.store.log(task_id, "merge stopped", spoken)
            bell.add(spoken, "warn", task_id)
            self.store.update(task_id, spoken=spoken)
            raise
        spoken = f"Task {task_id}, {task['title']}, {done['did']}. {done['cleaned']}."
        self.store.log(task_id, "merged", spoken)
        bell.add(spoken, "ok", task_id)
        return self.store.update(task_id, spoken=spoken), project

    async def stop_merge(self, task_id: int) -> dict:
        """Stop a merge under way and wait for it to end: the task, its spoken line saying how it ended (stopped, or
        merged when the stop came too late)."""
        merging = self.merges[task_id]
        merging.cancel()
        await asyncio.wait({merging}, timeout=MERGE_STOP_SECONDS)
        task = self._get(task_id)
        if not merging.done():
            return task | {"spoken": f"Task {task_id}'s merge is stopping; the bell says how it ended once it has."}
        return task

    async def _forget_copy(self, task: dict, project: projects.Project | None = None) -> None:
        """A task copied to a machine (not one whose project lives there): its copy has served its purpose."""
        if task["node"] and not (project or self._project(task)).node:
            await self.offload.forget(task["node"], self.offload.key(task["id"]))

    @staticmethod
    def _project(task: dict) -> projects.Project:
        try:
            return projects.find(task["project"])
        except ValueError:
            return projects.Project(task["project"] or "", Path("."), [], "")

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
                did = (f"Its changes in {folder} ({task['changes']}) are still there, mixed in with anything else "
                       "there, so review or undo them by hand")
        elif not task["branch"]:
            did = "It never got a branch, so there was nothing to remove"
        else:
            async with self.projects_lock:
                project = projects.find(task["project"])
                did = (await self._op(project.node, "discard", {
                    "repo": str(project.path), "branch": task["branch"], "worktree": task["worktree"],
                    "base": task["base"], "task": task_id, "name": project.name}))["did"]
        spoken = f"Task {task_id}, {task['title']}, discarded without merging. {did}."
        self.store.log(task_id, "discarded", spoken)
        bell.add(spoken, "warn", task_id)
        self._close_approvals(task_id, "discarded")
        remembered.end_session(session_key(task_id))
        await self._forget_copy(task)
        return self.store.update(task_id, status="discarded", spoken=spoken, activity=None, question=None,
                                 wait_kind=None, seen=1)

    async def init_git(self, name: str) -> dict:
        async with self.projects_lock:
            project = projects.find(name)
            if project.node:
                raise ValueError(f"{project.name} lives on {self.offload.name(project.node)}: set up git for it there.")
            if busy := self._jobs_on(project.name):
                raise ValueError(f"Task {' and '.join(map(str, busy))} is working on {project.name}; "
                                 "set up git once it's finished.")
            async with self._folder_lock(project.name):  # in-place tasks wait for git init
                return await asyncio.to_thread(projects.init_git, project.name)

    def _jobs_on(self, project: str, in_place: bool = False) -> list[int]:
        """Running or queued task ids on a project (only in-place ones if `in_place`). One waiting for a usage limit's
        reset doesn't count: it holds no folder, and its wait may last days."""
        tasks = (self.store.get(i) for i in self.jobs if i not in self.limit_waits)
        return sorted(t["id"] for t in tasks if t and t["project"] == project and (t["in_place"] or not in_place))

    def _folder_lock(self, project: str) -> asyncio.Lock:
        """Per-project lock held by anything changing its folder: in-place tasks, merges, git init."""
        return self.folders.setdefault(project, asyncio.Lock())

    def _lives_on(self, task: dict) -> int | None:
        """The machine a task's project lives on (its git steps run there), else None."""
        return self._project(task).node if task["project"] else None

    async def _make_worktree(self, task: dict) -> None:
        project = projects.find(task["project"])
        made = await self._op(project.node, "worktree", {"repo": str(project.path), "task": task["id"],
                                                         "title": task["title"], "name": project.name})
        self.store.set(task["id"], worktree=made["worktree"], branch=made["branch"], base=made["base"])
        self.store.log(task["id"], "branch", f"{made['branch']} from {made['base_ref']} at {made['base'][:7]}, in "
                                             f"{made['worktree']}")

    async def _enter_folder(self, task: dict) -> None:
        """Before each in-place run: save uncommitted work to refs/jarvis/; on the first run record branch and base.

        Non-git folders get a file snapshot (no base); repos with no commits use git's empty tree as base."""
        folder = task["worktree"] or str(projects.find(task["project"]).path)
        found = await self._op(self._lives_on(task), "enter", {"folder": folder, "task": task["id"],
                                                               "snapshot": task["id"] not in self.snapshots})
        if not found["git"]:
            if "snapshot" in found:
                self.snapshots[task["id"]] = found["snapshot"]
            if not task["worktree"]:
                self.store.set(task["id"], worktree=folder, branch=None, base=None)
                self.store.log(task["id"], "in place", f"working in {folder}, which isn't a git repo; "
                                                       "the files it changes are listed when it ends")
            return
        if found["saved"]:
            self.store.log(task["id"], "saved", f"uncommitted changes in {folder} saved to {found['saved']} before "
                                                "this run")
        if not task["worktree"]:
            branch = found["branch"]
            self.store.set(task["id"], worktree=folder, branch=branch, base=found["base"])
            self.store.log(task["id"], "in place", f"working in {folder} on {branch or 'a detached HEAD'} "
                                                   f"{found['at']}, without a worktree; changes stay uncommitted")

    async def _uncommitted(self, task: dict) -> str:
        """Summary of an in-place task's uncommitted changes since it began."""
        try:
            return (await self._op(self._lives_on(task), "changes", {
                "folder": task["worktree"], "base": task["base"], "snapshot": self.snapshots.get(task["id"])}))["said"]
        except Exception as e:
            log.exception("couldn't read changes for task %s", task["id"])
            return f"couldn't read changes: {e}"[:200]

    async def _commit(self, task: dict, summary: str, finished: bool) -> str:
        """Commit the run's changes; returns the branch's shortstat against its base."""
        try:
            return (await self._op(self._lives_on(task), "commit", {
                "worktree": task["worktree"], "base": task["base"], "title": task["title"], "summary": summary,
                "finished": finished, "task": task["id"]}))["said"]
        except Exception as e:
            log.exception("commit failed for task %s", task["id"])
            return f"couldn't commit: {e}"[:200]
