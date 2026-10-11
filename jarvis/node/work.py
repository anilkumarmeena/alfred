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
import shutil
import time
from pathlib import Path

from jarvis.core import proc
from jarvis.core.config import DATA_DIR, ROOT, WORKTREES_DIR
from jarvis.core.files import write_atomic
from jarvis.node import home, sync, transfer
from jarvis.node.agent import Agent
from jarvis.node.journal import Journal
from jarvis.node.link import Link, LinkDown, Refused
from jarvis.safety import commands
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
    when the main computer can't be reached."""

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
        if (now := time.time()) - self.sent_alive >= ALIVE_SECONDS:
            self.sent_alive = now
            busy = self.pulse.busy(task_id, now) or ("a long job it started is still running" if any(
                j["state"] == "running" for j in self.host_jobs.status(task=task_id)) else "")
            self.work.tell("alive", {"task": task_id, "busy": busy, "limited": self.pulse.limit(task_id, now)})

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
        self.runs: dict[int, tuple[str, asyncio.Task]] = {}  # task id -> (run id, its coroutine)
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
                    await up.call("report", event, timeout=60)
                except (LinkDown, TimeoutError):
                    return
                except Refused as e:  # the main computer won't take it (say, the task is gone): drop it
                    log.warning("the main computer refused %s for task %s: %s", event["kind"], event["task"], e)
                self.journal.done(event["seq"])

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

    # ── Scheduled commands of projects living here ───────────────
    # The main computer sends the definitions (schedule.sync); this machine runs them on time whether or not the
    # main computer is on, keeps its own record of when each last ran (it wins on that; the main computer wins on
    # definitions), honours do-not-disturb as last sent, and reports each run through the journal.

    def _schedules(self) -> dict:
        try:
            found = json.loads((self.state / "schedules.json").read_text())
        except (OSError, ValueError):
            found = {}
        return {"jobs": [], "hold": None, "ran": {}} | found

    def _keep_schedules(self, kept: dict) -> None:
        self.state.mkdir(parents=True, exist_ok=True)
        write_atomic(self.state / "schedules.json", json.dumps(kept))

    async def schedule_sync(self, body: dict) -> dict:
        self._keep_schedules(self._schedules() | {"jobs": list(body.get("jobs") or []), "hold": body.get("hold")})
        return {}

    async def schedule_tick(self, now: float | None = None) -> list[int]:
        """Report runs that ended, stop ones past their limit, start jobs that are due. Returns the jobs started."""
        now = now or time.time()
        kept = self._schedules()
        jobs = {str(j["id"]): j for j in kept["jobs"]}
        for key, ran in kept["ran"].items():
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
        """{"task": its row, "prompt", "run", "key"}: run it here in the copy, reporting through the journal."""
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
        self.runs[task_id] = (run, asyncio.create_task(self._run(task, prompt, run)))
        return {"started": True}

    def _run_file(self, task_id: int) -> Path:
        return self.state / "runs" / f"{task_id}.json"

    def kept_session(self, task_id: int, session_id: str) -> None:
        """Note a run's session, so it can carry on in it if this machine restarts."""
        if (kept := self._run_file(task_id)).exists():
            write_atomic(kept, json.dumps(json.loads(kept.read_text()) | {"session_id": session_id}))

    async def _run(self, task: dict, prompt: str, run: str) -> None:
        """One run, reported through the journal. Kept on disk while it goes, so a restart here can resume it."""
        task_id = task["id"]
        self._run_file(task_id).parent.mkdir(parents=True, exist_ok=True)
        write_atomic(self._run_file(task_id), json.dumps({"task": task, "run": run,
                                                          "session_id": task.get("session_id")}))
        reporter = NetReporter(self, task_id, run)
        self.agent.awake.hold(f"task {task_id}")
        outcome: Outcome | None = None
        try:
            outcome = await self.run_task(task, prompt, reporter)
        except asyncio.CancelledError:
            if task_id not in self.stopped:  # this machine is shutting down: resumed when it starts again
                self.runs.pop(task_id, None)
                raise
        except Exception as e:
            log.exception("task %s failed here", task_id)
            outcome = Outcome("", f"{type(e).__name__}: {e}"[:200], 0, "error")
        finally:
            self.agent.awake.release(f"task {task_id}")
        self.stopped.discard(task_id)
        self.runs.pop(task_id, None)
        self._run_file(task_id).unlink(missing_ok=True)
        ended = None if outcome is None else {"result": outcome.result, "error": outcome.error, "cost": outcome.cost,
                                              "kind": outcome.kind}
        self.journal.add("ended", task_id, run, {"outcome": ended})

    def resume(self) -> list[int]:
        """At start-up: runs cut off by a restart here carry on in their sessions; one that never got a session is
        reported cut off. Returns the tasks resumed."""
        resumed = []
        for kept in sorted((self.state / "runs").glob("*.json")):
            found = json.loads(kept.read_text())
            task, run = found["task"], found["run"]
            if not found.get("session_id"):
                kept.unlink()
                self.journal.add("ended", task["id"], run, {"outcome": None})
                continue
            task["session_id"] = found["session_id"]
            self.runs[task["id"]] = (run, asyncio.create_task(self._run(task, RESUMED, run)))
            resumed.append(task["id"])
        return resumed

    async def task_stop(self, body: dict) -> dict:
        if found := self.runs.get(int(body["task"])):
            self.stopped.add(int(body["task"]))
            found[1].cancel()
        stopped = self.host_jobs.stop_all(int(body["task"]))
        return {"stopped": bool(found), "jobs": stopped}

    async def task_running(self, _: dict) -> dict:
        """Runs going here, once the journal has gone out (so a run that ended is reported, not listed)."""
        with contextlib.suppress(TimeoutError):
            await self.caught_up(60)
        return {"runs": {str(t): run for t, (run, _) in self.runs.items()}}
