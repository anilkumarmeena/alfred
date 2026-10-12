"""What a machine does for the main computer: copies of projects, command jobs and Claude task runs.

Copies (one folder per run, under data/worktrees/, so the walls let its commands write there):
  copy.sync    bring a copy up to the main computer's files: git history from a bundle (into a per-project mirror the
               copies share), then every file whose hash differs, and remove what's gone there
  copy.back    what the run changed since, as one archive; copy.backed once the main computer has it
  copy.forget  remove a copy
Command jobs (as `start_job` runs them on the main computer: a login shell, without JARVIS's keys):
  job.start / job.poll / job.stop   numbered by the main computer's task id
Claude task runs (jarvis.tasks.worker.run, unchanged: the same sandbox, file gate and host walls as there):
  task.run / task.stop / task.running
  with JARVIS's skills as the main computer has them switched on: each version fetched once (skills.pack) and kept
  in data/node/skills/, checked against the digest it was offered with
A run's reports go through the journal (`NetReporter`); questions the gate must ask (has this command been approved?
is there a message for the task?) go to the main computer live, and get the safe answer when it can't be reached:
not approved, nothing new.

Every command here is checked again: secrets and environment reads are refused, and a risky command needs the main
computer's word that the user said yes.
"""

import asyncio
import base64
import contextlib
import json
import logging
import re
import secrets
import shutil
import time
from pathlib import Path

from jarvis.brain import usage
from jarvis.core import proc
from jarvis.core.config import DATA_DIR, ROOT, WORKTREES_DIR
from jarvis.core.files import write_atomic
from jarvis.node import home, sync, transfer
from jarvis.node.agent import Agent
from jarvis.node.journal import Journal
from jarvis.node.link import Link, LinkDown, Refused
from jarvis.safety import commands
from jarvis.skills import home as skills
from jarvis.tasks import command_jobs, git_ops, projects, schedules, stall
from jarvis.tasks.host_tools import TaskJobs
from jarvis.tasks.worker import Outcome, run as run_task

log = logging.getLogger("jarvis.node.work")

KEY = re.compile(r"[a-z0-9][a-z0-9_.-]{0,80}")  # a copy's name, as the main computer gives it
POLL_BYTES = 256 * 1024  # most job output sent per poll
ALIVE_SECONDS = 20.0  # how often a run's signs of life reach the main computer
COPY_DAYS = 14  # copies untouched this long are removed
# Prompt for a run resumed in its own session after this machine restarted
RESUMED = ("The computer this task runs on restarted while you were working on it, cutting your last step off. Carry "
           "on with the task from where you were: first check what that step left behind, as it may not have "
           "finished, then end with your summary of the whole task as before.")


class NetReporter:
    """The Reporter a run here uses: reports go through the journal; the gate's questions go live, with safe answers
    when the main computer can't be reached.

    `task_id` is the task as the main computer knows it. A scheduled task begun here while it was away has a stand-in
    id (below 0) until the main computer takes it up and gives it its own; the run itself keeps the stand-in."""

    def __init__(self, work: "Work", task_id: int, run: str) -> None:
        self.work, self.task_id, self.run = work, task_id, run
        self.host_jobs = work.host_jobs
        self.pulse = stall.Pulse()
        self.sent_alive = 0.0

    def _note(self, kind: str, **body) -> None:
        self.work.journal.add(kind, self.task_id, self.run, body)

    def session(self, task_id: int, session_id: str) -> None:
        self._note("session", session_id=session_id)
        self.work.kept_session(task_id, session_id)

    def alive(self, task_id: int, msg: object = None) -> None:
        self.pulse.saw(task_id, msg)
        if (now := time.time()) - self.sent_alive >= ALIVE_SECONDS and self.task_id > 0:
            self.sent_alive = now
            busy = self.pulse.busy(task_id, now) or ("a long job it started is still running" if any(
                j["state"] == "running" for j in self.host_jobs.status(task=task_id)) else "")
            self.work.tell("alive", {"task": self.task_id, "busy": busy, "limited": self.pulse.limit(task_id, now)})

    def activity(self, task_id: int, text: str) -> None:
        self._note("activity", text=text)

    def progress(self, task_id: int, text: str) -> None:
        self._note("progress", text=text)

    def subagent(self, task_id: int, text: str) -> None:
        self._note("subagent", text=text)

    def ask(self, task_id: int, question: str) -> None:
        self._note("ask", question=question)

    def flag(self, task_id: int, text: str) -> None:
        self._note("flag", text=text)

    async def _ask_main(self, kind: str, body: dict, timeout: float = 30) -> dict | None:
        """The main computer's answer, or None if it can't be reached (the journal is sent first, to keep order)."""
        up = self.work.agent.link
        if up is None or not up.up:
            return None
        try:
            await self.work.caught_up(timeout)
            if self.task_id < 0:  # not taken up by the main computer yet
                return None
            return await up.call(kind, {"task": self.task_id, "run": self.run} | body, timeout)
        except (LinkDown, TimeoutError):
            return None

    async def take_nudges(self, task_id: int) -> list[str]:
        got = await self._ask_main("task.nudges", {})
        return [str(m) for m in (got or {}).get("messages") or []]

    async def cleared(self, task_id: int, item: dict) -> bool:
        try:
            got = await self._ask_main("task.cleared", {"item": item})
        except Refused:
            return False
        return bool(got and got.get("cleared"))

    async def request_approval(self, task_id: int, item: dict) -> dict:
        try:
            got = await self._ask_main("task.approval", {"item": item})
        except Refused as e:
            raise ValueError(str(e)) from None
        if got is None:  # asked as soon as the link is back
            self._note("approval", item=item)
            return {"id": "waiting to be sent"}
        return got

    async def propose(self, task_id: int, args: dict) -> dict:
        try:
            got = await self._ask_main("task.propose", {"args": args})
        except Refused as e:
            raise ValueError(str(e)) from None
        if got is None:
            raise ValueError("JARVIS's main computer can't be reached right now; mention the edit in your summary")
        return got

    async def request_access(self, task_id: int, names: list[str], reason: str) -> list[str]:
        try:
            got = await self._ask_main("task.access", {"names": names, "reason": reason})
        except Refused as e:
            raise ValueError(str(e)) from None
        if got is None:
            self._note("access", names=names, reason=reason)
            return names
        return [str(n) for n in got.get("asked") or []]


class Work:
    """The work handlers, registered on `agent`."""

    def __init__(self, agent: Agent, journal: Journal | None = None, run=run_task) -> None:
        self.agent = agent
        self.journal = journal or Journal(home.JOURNAL_FILE)
        self.run_task = run  # worker.run; a test gives a stand-in
        self.state = DATA_DIR / "node"
        self.jobs = command_jobs.Runs(self.state / "jobs")
        self.scheduled = command_jobs.Runs(self.state / "scheduled")  # runs of the jobs this machine owns
        self.host_jobs = TaskJobs()
        self.runs: dict[int, tuple[str, asyncio.Task]] = {}  # task id (as the main computer knows it) -> (run, coroutine)
        self.reporters: dict[str, NetReporter] = {}  # run -> its reporter
        self.aliases: dict[int, int] = {}  # task id -> the stand-in id its run began with here, while it was away
        self.stopped: set[int] = set()  # runs the main computer stopped (others cut off by a shutdown here resume)
        self.locks: dict[str, asyncio.Lock] = {}
        self._sent = asyncio.Event()  # the journal is empty, as far as the sender knows
        self._sent.set()
        agent.handlers |= {
            "copy.sync": self.copy_sync, "copy.back": self.copy_back, "copy.backed": self.copy_backed,
            "copy.forget": self.copy_forget, "job.start": self.job_start, "job.poll": self.job_poll,
            "job.stop": self.job_stop, "task.run": self.task_run, "task.stop": self.task_stop,
            "task.running": self.task_running, "folder.check": self.folder_check, "schedule.sync": self.schedule_sync,
            **{f"folder.{op}": self._folder_step(op) for op in git_ops.OPS}}
        agent.busy = self.busy
        agent.on_link.append(self.on_link)

    def busy(self) -> int:
        """Runs and jobs going here (an update waits for none)."""
        return len(self.runs) + len(self._running_jobs())

    def _running_jobs(self) -> list[int]:
        return [r["id"] for r in self.jobs._records() if self.jobs.how(r) is None]

    # ── The journal ──────────────────────────────────────────────

    async def on_link(self, up: Link) -> None:
        asyncio.create_task(self._send(up))

    async def _send(self, up: Link) -> None:
        """Send the journal in order, each event once acknowledged, for as long as the link lasts."""
        while up.up:
            pending = self.journal.pending()
            if not pending:
                self._sent.set()
                self.journal.added.clear()
                if self.journal.pending():  # added between the read and the clear
                    continue
                waiter = asyncio.create_task(self.journal.added.wait())
                closed = asyncio.create_task(up.closed.wait())
                await asyncio.wait([waiter, closed], return_when=asyncio.FIRST_COMPLETED)
                waiter.cancel()
                closed.cancel()
                continue
            self._sent.clear()
            for event in pending:
                try:
                    got = await up.call("report", event, timeout=60)
                except (LinkDown, TimeoutError):
                    return
                except Refused as e:  # the main computer won't take it (say, the task is gone): drop it
                    log.warning("the main computer refused %s for task %s: %s", event["kind"], event["task"], e)
                    got = {}
                self.journal.done(event["seq"])
                if event["kind"] == "away" and got.get("task"):  # the rest of the run's events now carry its id
                    self._adopted(event["task"], int(got["task"]), event["run"])
                    break

    async def caught_up(self, timeout: float) -> None:
        """Wait until the journal has been sent, so a live question can't overtake what came before it."""
        if self.journal.pending():
            self._sent.clear()
            self.journal.added.set()
        await asyncio.wait_for(self._sent.wait(), timeout)

    def tell(self, kind: str, body: dict) -> None:
        """A one-way note that may be lost (signs of life)."""
        if self.agent.link is not None and self.agent.link.up:
            asyncio.create_task(self.agent.report(kind, body))

    # ── Copies ───────────────────────────────────────────────────

    def _folder(self, key: str) -> Path:
        if not KEY.fullmatch(key):
            raise Refused("that isn't a copy's name")
        return WORKTREES_DIR / f"node-{key}"

    def _base_file(self, key: str) -> Path:
        return self.state / "copies" / f"{key}.json"

    def _mirror(self, mirror: str) -> Path:
        if not KEY.fullmatch(mirror):
            raise Refused("that isn't a project's name")
        return WORKTREES_DIR / f"node-mirror-{mirror}.git"

    async def copy_sync(self, body: dict) -> dict:
        """{"key", "mirror", "manifest": offer, "git": {"head"} | None} -> {"copied", "bytes", "removed"}."""
        key = str(body["key"])
        folder = self._folder(key)
        async with self.locks.setdefault(key, asyncio.Lock()):
            up = self._link()
            if git := body.get("git"):
                await self._history(up, key, folder, self._mirror(str(body["mirror"])), str(git["head"]))
            folder.mkdir(parents=True, exist_ok=True)
            spool = self.state / "spool"
            theirs = json.loads((await transfer.fetch(up, body["manifest"], spool / f"{key}.manifest")).read_text())
            (spool / f"{key}.manifest").unlink(missing_ok=True)
            ours = await asyncio.to_thread(sync.manifest, folder, self.state / "hashes" / f"{key}.json")
            needed = [r for r in sync.differing(theirs, ours) if sync.allowed(r)]
            gone = await asyncio.to_thread(sync.remove, folder, sorted(set(ours) - set(theirs)))
            if needed:
                offer = await up.call("copy.pack", {"key": key, "files": needed}, timeout=600)
                archive = await transfer.fetch(up, offer, spool / f"{key}.tar", timeout=600)
                try:
                    await asyncio.to_thread(sync.unpack, archive, folder)
                finally:
                    archive.unlink(missing_ok=True)
            self._base_file(key).parent.mkdir(parents=True, exist_ok=True)
            write_atomic(self._base_file(key), json.dumps(theirs))
        return {"copied": len(needed), "bytes": sync.total(theirs, needed), "removed": len(gone), "folder": str(folder)}

    async def _history(self, up: Link, key: str, folder: Path, mirror: Path, head: str) -> None:
        """Make `folder` a git checkout of `head`, its history fetched into the project's mirror as needed."""
        if not mirror.exists():
            mirror.parent.mkdir(parents=True, exist_ok=True)
            await self._git(mirror.parent, "init", "-q", "--bare", str(mirror))
        if (await proc.run_try("git", "-C", str(mirror), "cat-file", "-e", f"{head}^{{commit}}"))[0] != 0:
            code, out = await proc.run_try("git", "-C", str(mirror), "for-each-ref", "--format=%(objectname)")
            have = out.split()[:50] if code == 0 else []
            offer = await up.call("copy.bundle", {"key": key, "have": have}, timeout=600)
            bundle = await transfer.fetch(up, offer, self.state / "spool" / f"{key}.bundle", timeout=600)
            try:
                await self._git(mirror, "fetch", "-q", "--no-tags", str(bundle), f"+HEAD:refs/jarvis/{key}")
            finally:
                bundle.unlink(missing_ok=True)
        else:
            await self._git(mirror, "update-ref", f"refs/jarvis/{key}", head)
        if not (folder / ".git").exists():
            if folder.exists():
                shutil.rmtree(folder)
            await self._git(mirror.parent, "clone", "-q", "--shared", "--no-checkout", str(mirror), str(folder))
        await self._git(folder, "fetch", "-q", "origin", f"+refs/jarvis/{key}:refs/jarvis/base")
        await self._git(folder, "checkout", "-q", "-f", "--detach", head)

    @staticmethod
    async def _git(cwd: Path, *args: str) -> str:
        code, out = await proc.run_try("git", "-C", str(cwd), *args, timeout=600)
        if code:
            raise RuntimeError(f"git {args[0]} failed: {out}"[:300])
        return out

    async def copy_back(self, body: dict) -> dict:
        """{"key"} -> {"changed", "deleted", "archive": offer | None}: what the run changed since the copy was made."""
        key = str(body["key"])
        folder = self._folder(key)
        async with self.locks.setdefault(key, asyncio.Lock()):
            base = json.loads(self._base_file(key).read_text()) if self._base_file(key).exists() else {}
            now = await asyncio.to_thread(sync.manifest, folder, self.state / "hashes" / f"{key}.json")
            changed = sync.differing(now, base)
            deleted = sorted(set(base) - set(now))
            archive = None
            if changed:
                packed = await asyncio.to_thread(sync.pack, folder, changed, self.state / "spool" / f"{key}-back.tar")
                archive = self.agent.outbox.offer(packed, remove=True)
            write_atomic(self.state / "copies" / f"{key}.now.json", json.dumps(now))
        return {"changed": changed, "deleted": deleted, "archive": archive, "entries": {r: now[r] for r in changed}}

    async def copy_backed(self, body: dict) -> dict:
        """The main computer has the changes: they're the copy's new starting point."""
        key = str(body["key"])
        self._folder(key)
        if (now := self.state / "copies" / f"{key}.now.json").exists():
            now.replace(self._base_file(key))
        return {}

    async def copy_forget(self, body: dict) -> dict:
        key = str(body["key"])
        folder = self._folder(key)
        async with self.locks.setdefault(key, asyncio.Lock()):
            await asyncio.to_thread(shutil.rmtree, folder, True)
            for leftover in (self._base_file(key), self.state / "hashes" / f"{key}.json",
                             self.state / "copies" / f"{key}.now.json"):
                leftover.unlink(missing_ok=True)
        self.locks.pop(key, None)
        return {}

    def prune(self, days: float = COPY_DAYS) -> list[str]:
        """Remove copies nobody has touched in `days` (the main computer forgets them when tasks end; this catches
        the rest)."""
        gone = []
        for folder in WORKTREES_DIR.glob("node-*"):
            if folder.name.startswith("node-mirror-") or not folder.is_dir():
                continue
            if time.time() - folder.stat().st_mtime > days * 86400 and folder.name.removeprefix("node-") not in self.locks:
                shutil.rmtree(folder, ignore_errors=True)
                gone.append(folder.name)
        for folder in (self.state / "skills").glob("*@*"):  # skill versions no run has used lately
            if time.time() - folder.stat().st_mtime > days * 86400:
                shutil.rmtree(folder, ignore_errors=True)
                gone.append(folder.name)
        return gone

    def _link(self) -> Link:
        if self.agent.link is None or not self.agent.link.up:
            raise LinkDown("the main computer went away")
        return self.agent.link

    def _inside(self, key: str, rel: str) -> Path:
        folder = self._folder(key)
        target = (folder / rel).resolve() if rel else folder.resolve()
        if not target.is_relative_to(folder.resolve()) or not target.is_dir():
            raise Refused(f"{rel or 'the copy'} isn't a folder in the copy")
        return target

    # ── Projects that live here ──────────────────────────────────

    @staticmethod
    def own_folder(path: object, worktrees: bool = False) -> Path:
        """A project folder the main computer names here: in this home, outside JARVIS's own folder (but for task
        worktrees, with `worktrees`), never a secret. Refused otherwise."""
        folder = Path(str(path)).expanduser().resolve()
        if worktrees and folder.is_relative_to(WORKTREES_DIR.resolve()):
            return folder
        if commands.secret_path(folder) or folder.is_relative_to(ROOT.resolve()):
            raise Refused(f"{folder} holds secrets or JARVIS's own files, so it can't be a project's folder")
        try:
            return projects._check_folder(str(folder))
        except ValueError as e:
            raise Refused(str(e)) from None

    async def folder_check(self, body: dict) -> dict:
        """{"path", "name"}: a folder for a project living here, made if missing -> {"path", "state"}."""
        path = str(body.get("path") or "") or str(projects._default_path(str(body["name"])))
        folder = self.own_folder(path)
        try:
            await asyncio.to_thread(projects._check_own_repo, folder)
        except ValueError as e:
            raise Refused(str(e)) from None
        folder.mkdir(parents=True, exist_ok=True)
        return {"path": str(folder), "state": await asyncio.to_thread(projects.git_state, folder)}

    def _folder_step(self, op: str):
        """A git step (git_ops.OPS) on a project living here, every folder it names checked first."""
        async def step(body: dict) -> dict:
            for key in ("repo", "folder"):
                if key in body:
                    body[key] = str(self.own_folder(body[key]))
            if body.get("worktree"):
                body["worktree"] = str(self.own_folder(body["worktree"], worktrees=True))
            return await git_ops.OPS[op](body)
        return step

    # ── Scheduled jobs of projects living here ───────────────────
    # The main computer sends the definitions (schedule.sync); this machine starts them on time whether or not the
    # main computer is on, keeps its own record of when each last ran (it wins on that; the main computer wins on
    # definitions), and honours do-not-disturb as last sent. A command it runs itself, reported through the journal.
    # A Claude task it hands to the main computer when the link is up (schedule.due: it runs as any task does, here);
    # while the main computer is away, it begins the task here alone, under a stand-in id, and reports it ("away")
    # for the main computer to take up on its return. A run asked for but not answered is never begun alone: it's
    # asked again once the link is back, so it can't run twice.

    def _schedules(self) -> dict:
        try:
            found = json.loads((self.state / "schedules.json").read_text())
        except (OSError, ValueError):
            found = {}
        return {"jobs": [], "hold": None, "ran": {}, "skills": [], "stand_in": 0} | found

    def _keep_schedules(self, kept: dict) -> None:
        self.state.mkdir(parents=True, exist_ok=True)
        write_atomic(self.state / "schedules.json", json.dumps(kept))

    async def schedule_sync(self, body: dict) -> dict:
        offered = list(body.get("skills") or [])
        await self.skills(offered)  # fetched now, for tasks begun while the main computer is away
        self._keep_schedules(self._schedules() | {"jobs": list(body.get("jobs") or []), "hold": body.get("hold"),
                                                  "skills": offered})
        return {}

    async def _start_task(self, kept: dict, job: dict, due: float, now: float) -> dict | None:
        """A scheduled Claude task that's due, handed to the main computer or begun here alone. Returns what to keep
        of the run, or None to try again on the next tick."""
        if usage.hold_until(now):  # this machine's own Claude account is (nearly) used up
            return None
        token, nxt = f"{job['id']}-{int(due)}", schedules.following(job, now)
        if self.agent.link is not None and self.agent.link.up:
            return await self._hand_over(job["id"], token, nxt, now)
        try:
            return await self._run_alone(kept, job, token, nxt, now)
        except (Refused, ValueError, RuntimeError, OSError) as e:
            log.warning("scheduled task %s couldn't begin here: %s", job["id"], e)
            return {"at": now, "claude": "failed", "next_run": nxt, "reported": True}

    async def _hand_over(self, job_id: int, token: str, nxt: float | None, now: float) -> dict:
        """Ask the main computer to start the run; one asked but not answered waits to be asked again."""
        try:
            got = await self._link().call("schedule.due", {"job": job_id, "due": token, "at": now}, timeout=60)
        except Refused as e:
            log.warning("the main computer didn't start scheduled task %s: %s", job_id, e)
            return {"at": now, "claude": "refused", "next_run": nxt, "reported": True}
        except (LinkDown, TimeoutError):
            return {"at": now, "claude": "asked", "due": token, "next_run": nxt}
        return {"at": now, "claude": "main", "task": got.get("task"), "next_run": got.get("next_run", nxt),
                "reported": True}

    async def _run_alone(self, kept: dict, job: dict, token: str, nxt: float | None, now: float) -> dict:
        """Begin a scheduled task here while the main computer is away, as it would: on a branch in a worktree, or in
        the project's folder with uncommitted work saved first."""
        folder = self.own_folder(job["folder"])
        kept["stand_in"] = stand_in = int(kept.get("stand_in") or 0) - 1
        name = f"away{-stand_in}"
        task = {"id": stand_in, "kind": job["task_kind"], "title": job["title"], "instruction": job["prompt"],
                "project": job["project"], "in_place": int(bool(job["in_place"])), "branch": None, "base": None,
                "session_id": None, "job": job["id"], "grants": None, "todo": None, "status": "running",
                "worktree": str(folder)}
        saved = ""
        if job["task_kind"] == "code_change" and not job["in_place"]:
            made = await git_ops.worktree_op({"repo": str(folder), "task": name, "title": job["title"],
                                              "name": job["project"]})
            task |= {k: made[k] for k in ("worktree", "branch", "base")}
        elif job["in_place"]:
            found = await git_ops.enter_op({"folder": str(folder), "task": name})
            task |= {"branch": found.get("branch"), "base": found.get("base")}
            saved = found.get("saved") or ""
        task["skills"] = await self.skills(kept.get("skills") or [])
        run = secrets.token_hex(6)
        self.journal.add("away", stand_in, run, {
            "job": job["id"], "due": token, "started": now, "next_run": nxt, "saved": saved,
            "task": {k: task[k] for k in ("instruction", "in_place", "worktree", "branch", "base")}})
        self.runs[stand_in] = (run, asyncio.create_task(self._run(task, job["prompt"], run)))
        return {"at": now, "claude": "alone", "run": run, "next_run": nxt}

    async def schedule_tick(self, now: float | None = None) -> list[int]:
        """Report runs that ended, stop ones past their limit, start jobs that are due. Returns the jobs started."""
        now = now or time.time()
        kept = self._schedules()
        jobs = {str(j["id"]): j for j in kept["jobs"]}
        for key, ran in kept["ran"].items():
            if "claude" in ran and not ran.get("reported"):  # a scheduled task: begun alone, or asked for
                if ran["claude"] == "asked" and self.agent.link is not None and self.agent.link.up:
                    kept["ran"][key] = await self._hand_over(int(key), ran["due"], ran["next_run"], ran["at"])
                elif ran["claude"] == "alone":
                    ran["reported"] = all(r != ran["run"] for r, _ in self.runs.values())
                continue
            if ran.get("reported") or (record := self.scheduled.get(ran["run"])) is None:
                continue
            if (ended := self.scheduled.how(record)) is None:
                limit = (jobs.get(key) or {}).get("timeout_minutes")
                if limit and now - ran["at"] > limit * 60:
                    with contextlib.suppress(ValueError):
                        await self.scheduled.stop(ran["run"])
                continue
            self.journal.add("scheduled", 0, "", {
                "job": int(key), "started": ran["at"], "ended": now, "code": ended[1], "next_run": ran["next_run"],
                "output": command_jobs.tail(self.scheduled.log_file(ran["run"]), 4000),
                "away": self.agent.link is None or not self.agent.link.up})
            ran["reported"] = True
            self.agent.awake.release(f"scheduled {key}")
        hold = kept.get("hold")
        held = bool(hold) and (hold.get("until") is None or hold["until"] > now)
        started = []
        for key, job in jobs.items():
            ran = kept["ran"].get(key)
            due = ran["next_run"] if ran and ran["at"] > (job.get("last_run") or 0) else job.get("next_run")
            if job["status"] != "active" or held or not due or due > now or (ran and not ran.get("reported")):
                continue
            if job.get("kind") == "task":
                if begun := await self._start_task(kept, job, due, now):
                    kept["ran"][key] = begun
                    started.append(job["id"])
                continue
            try:
                folder = self.own_folder(job["folder"])
            except Refused as e:
                log.warning("scheduled job %s can't run here: %s", key, e)
                continue
            if why := self.refusal(job["text"], "made through the gate"):  # weighed when it was made: secrets never
                log.warning("scheduled job %s refused: %s", key, why)
                continue
            record = self.scheduled.launch(job["text"], job["title"], folder, extra={"schedule": job["id"]})
            self.agent.awake.hold(f"scheduled {key}")
            kept["ran"][key] = {"at": now, "run": record["id"], "next_run": schedules.following(job, now)}
            started.append(job["id"])
        self._keep_schedules(kept)
        return started

    async def schedule_loop(self, every: float = 30.0) -> None:
        while True:
            try:
                await self.schedule_tick()
            except Exception:
                log.exception("the scheduled jobs here couldn't be checked")
            await asyncio.sleep(every)

    # ── Command jobs ─────────────────────────────────────────────

    @staticmethod
    def refusal(command: str, approved: str) -> str:
        """Why the main computer couldn't have let this run (secrets, environment reads, or a risky command without
        the user's yes), or ""."""
        if commands.names_secret(command) or commands.reads_environment(command):
            return "it touches secrets, which stay out of reach even with a yes"
        if (why := commands.risky(command)) and not approved:
            return f"it {why}, and the main computer didn't say the user agreed to it"
        return ""

    async def job_start(self, body: dict) -> dict:
        """{"task", "key", "rel", "command", "title", "approved"} -> {"started"}, or {"refused": why}. Runs as the
        main computer runs a job: outside the sandbox in a login shell, without JARVIS's keys."""
        task, command = int(body["task"]), str(body["command"]).strip()
        if why := self.refusal(command, str(body.get("approved") or "")):
            return {"refused": why}
        if body.get("path"):  # in a project living here
            folder = self.own_folder(body["path"])
            if not folder.is_dir():
                raise Refused(f"{folder} isn't a folder here")
        else:
            folder = self._inside(str(body["key"]), str(body.get("rel") or ""))
        if (old := self.jobs.get(task)) is not None:
            if self.jobs.how(old) is None:
                raise Refused(f"job {task} is already running here")
            for leftover in self.jobs.folder.glob(f"{task}.*"):
                leftover.unlink()
        record = self.jobs.start(task, command, str(body.get("title") or command[:40]), folder)
        self.agent.awake.hold(f"job {task}")
        asyncio.create_task(self._awake_until_done(task))
        return {"started": record["started"]}

    async def _awake_until_done(self, task: int) -> None:
        while (record := self.jobs.get(task)) is not None and self.jobs.how(record) is None:
            await asyncio.sleep(self.jobs.poll)
        self.agent.awake.release(f"job {task}")

    async def job_poll(self, body: dict) -> dict:
        """{"task", "offset"} -> {"data": base64 output from offset, "ended": [outcome, code] | None}."""
        task, offset = int(body["task"]), int(body.get("offset") or 0)
        record = self.jobs.get(task)
        if record is None:
            return {"data": "", "ended": [command_jobs.LOST, None], "gone": True}
        ended = self.jobs.how(record)
        try:
            with open(self.jobs.log_file(task), "rb") as f:
                f.seek(offset)
                data = f.read(POLL_BYTES)
        except OSError:
            data = b""
        more = len(data) == POLL_BYTES
        return {"data": base64.b64encode(data).decode(), "ended": None if more else ended}

    async def job_stop(self, body: dict) -> dict:
        with contextlib.suppress(ValueError):  # already ended
            await self.jobs.stop(int(body["task"]))
        return {}

    # ── Claude task runs ─────────────────────────────────────────

    async def task_run(self, body: dict) -> dict:
        """{"task": its row, "prompt", "run", "key" or "folder", "skills"}: run it here in the copy (or the project's
        own folder), with the main computer's skills, reporting through the journal."""
        task, prompt, run = dict(body["task"]), str(body["prompt"]), str(body["run"])
        task_id = int(task["id"])
        if task_id in self.runs:
            raise Refused(f"task {task_id} is already running here")
        if task["kind"] not in ("code_change", "code_question"):
            raise Refused(f"a {task['kind']} task runs on the main computer")
        if body.get("folder"):  # a project living here: its own folder, or its task worktree here
            task["worktree"] = str(self.own_folder(body["folder"], worktrees=True))
        else:
            task["worktree"] = str(self._inside(str(body["key"]), ""))
        task["skills"] = await self.skills(body.get("skills") or [])
        self.runs[task_id] = (run, asyncio.create_task(self._run(task, prompt, run)))
        return {"started": True}

    # ── JARVIS's skills, as the main computer has them switched on ──

    async def skills(self, offered: list[dict]) -> list[str]:
        """Folders here of the skills the main computer sent ({"name", "digest"} each), fetching the versions this
        machine lacks. A version that arrives altered is left out."""
        wanted = {}
        for s in offered:
            name, digest = str(s.get("name") or ""), str(s.get("digest") or "")
            if skills.NAME.fullmatch(name) and re.fullmatch(r"[0-9a-f]{64}", digest):
                wanted[name] = (digest, self.state / "skills" / f"{name}@{digest[:16]}")
        if missing := [n for n, (_, folder) in wanted.items() if not folder.is_dir()]:
            try:
                await self._fetch_skills(missing, wanted)
            except (LinkDown, Refused, TimeoutError, ValueError, OSError) as e:  # the run goes on without them
                log.warning("skills %s couldn't be fetched: %s", ", ".join(missing), e)
        found = []
        for _, folder in wanted.values():
            if folder.is_dir():
                folder.touch()  # in use: kept from pruning
                found.append(str(folder))
        return found

    async def _fetch_skills(self, names: list[str], wanted: dict[str, tuple[str, Path]]) -> None:
        up = self._link()
        offer = await up.call("skills.pack", {"names": names}, timeout=300)
        staging = self.state / "skills" / f".staging-{secrets.token_hex(4)}"
        archive = await transfer.fetch(up, offer, staging.with_suffix(".tar"), timeout=300)
        try:
            await asyncio.to_thread(sync.unpack, archive, staging)
            for name in names:
                digest, folder = wanted[name]
                if (staging / name).is_dir() and await asyncio.to_thread(skills.whole_digest, staging / name) == digest:
                    (staging / name).replace(folder)
                else:
                    log.warning("skill %s arrived altered or not at all, so runs here go without it", name)
        finally:
            archive.unlink(missing_ok=True)
            shutil.rmtree(staging, ignore_errors=True)

    def _run_file(self, task_id: int) -> Path:
        return self.state / "runs" / f"{task_id}.json"

    def kept_session(self, task_id: int, session_id: str) -> None:
        """Note a run's session, so it can carry on in it if this machine restarts."""
        if (kept := self._run_file(task_id)).exists():
            write_atomic(kept, json.dumps(json.loads(kept.read_text()) | {"session_id": session_id}))

    async def _run(self, task: dict, prompt: str, run: str, known_as: int | None = None) -> None:
        """One run, reported through the journal (as task `known_as` when the main computer knows it by another id).
        Kept on disk while it goes, so a restart here can resume it."""
        own_id = task["id"]
        self._run_file(own_id).parent.mkdir(parents=True, exist_ok=True)
        write_atomic(self._run_file(own_id), json.dumps({"task": task, "run": run, "session_id": task.get("session_id")}
                                                        | ({"as": known_as} if known_as else {})))
        reporter = self.reporters[run] = NetReporter(self, known_as or own_id, run)
        self.agent.awake.hold(f"task {own_id}")
        outcome: Outcome | None = None
        try:
            outcome = await self.run_task(task, prompt, reporter)
        except asyncio.CancelledError:
            if reporter.task_id not in self.stopped:  # this machine is shutting down: resumed when it starts again
                self.runs.pop(reporter.task_id, None)
                self.reporters.pop(run, None)
                raise
        except Exception as e:
            log.exception("task %s failed here", own_id)
            outcome = Outcome("", f"{type(e).__name__}: {e}"[:200], 0, "error")
        finally:
            self.agent.awake.release(f"task {own_id}")
        self.stopped.discard(reporter.task_id)
        self.runs.pop(reporter.task_id, None)
        self.reporters.pop(run, None)
        self._run_file(own_id).unlink(missing_ok=True)
        ended = None if outcome is None else {"result": outcome.result, "error": outcome.error, "cost": outcome.cost,
                                              "kind": outcome.kind}
        self.journal.add("ended", reporter.task_id, run, {"outcome": ended})

    def _adopted(self, stand_in: int, task_id: int, run: str) -> None:
        """The main computer took up a run begun here alone: from now on it's task `task_id` there (the run itself
        keeps its stand-in id here)."""
        self.journal.db.execute("update events set task = ? where task = ? and run = ?", (task_id, stand_in, run))
        if reporter := self.reporters.get(run):
            reporter.task_id = task_id
        if found := self.runs.pop(stand_in, None):
            self.runs[task_id] = found
        if (kept := self._run_file(stand_in)).exists():
            write_atomic(kept, json.dumps(json.loads(kept.read_text()) | {"as": task_id}))
        self.aliases[task_id] = stand_in

    def resume(self) -> list[int]:
        """At start-up: runs cut off by a restart here carry on in their sessions; one that never got a session is
        reported cut off. Returns the tasks resumed."""
        resumed = []
        for kept in sorted((self.state / "runs").glob("*.json")):
            found = json.loads(kept.read_text())
            task, run, known_as = found["task"], found["run"], found.get("as")
            task_id = known_as or task["id"]
            if not found.get("session_id"):
                kept.unlink()
                self.journal.add("ended", task_id, run, {"outcome": None})
                continue
            task["session_id"] = found["session_id"]
            if known_as:
                self.aliases[known_as] = task["id"]
            self.runs[task_id] = (run, asyncio.create_task(self._run(task, RESUMED, run, known_as)))
            resumed.append(task_id)
        return resumed

    async def task_stop(self, body: dict) -> dict:
        task_id = int(body["task"])
        if found := self.runs.get(task_id):
            self.stopped.add(task_id)
            found[1].cancel()
        stopped = self.host_jobs.stop_all(self.aliases.get(task_id, task_id))
        return {"stopped": bool(found), "jobs": stopped}

    async def task_running(self, _: dict) -> dict:
        """Runs going here, once the journal has gone out (so a run that ended is reported, not listed)."""
        with contextlib.suppress(TimeoutError):
            await self.caught_up(60)
        return {"runs": {str(t): run for t, (run, _) in self.runs.items()}}
