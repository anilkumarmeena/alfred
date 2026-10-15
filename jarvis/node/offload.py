"""Sending work to the user's machines and bringing it back; the task runner holds one Offload.

Copies (jarvis.node.sync over the link): a project's files go to a copy on the machine, its git history as a bundle;
after the run, what changed comes back into the folder here. A file that also changed here meanwhile isn't
overwritten: the machine's version is saved beside it (name.from-<machine>) and the task's log says so.

Commands: copied over, started there as a job, its output mirrored into the local log every COMMAND_POLL seconds, its
files brought back, then its exit status written here, so the runner sums it up, triages and fixes it as if it had
run here. Claude tasks: copied over, run there (jarvis.node.work), their reports applied here in order as they arrive,
their changes brought back before the runner commits or reports them. The runner's database stays the one record.

What a machine sends is checked: it may only report on its own tasks, and a command it asks approval for is described
here, from the command itself, never from the machine's own words.
"""

import asyncio
import base64
import contextlib
import hashlib
import json
import logging
import re
import secrets
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from jarvis.core import profile
from jarvis.core.config import DATA_DIR, NODE_LOST_MINUTES, NODE_SYNC_MAX_GB, NODE_WAIT_MINUTES
from jarvis.core.database import ACTIVE, COMMAND
from jarvis.core.files import write_atomic
from jarvis.node import bundles, machines, placement, sync, transfer
from jarvis.node.hub import Hub
from jarvis.node.link import Link, LinkDown, Refused
from jarvis.safety import commands, remembered
from jarvis.skills import home as skills
from jarvis.state import activity as bell
from jarvis.tasks import command_jobs, hold, projects, scheduler, schedules
from jarvis.tasks.host_tools import HOST_COMMANDS
from jarvis.tasks.worker import Outcome

if TYPE_CHECKING:
    from jarvis.tasks.runner import Runner

log = logging.getLogger("jarvis.node.offload")

POLL = 2.0  # seconds between polls of a remote command's output
BACK_SECONDS = 1800.0  # most a run's changes may take to come back
UNREACHABLE_NOTE = 120.0  # seconds a machine with work may be gone before the bell says so
SUMMARY_WAIT = 5.0  # seconds without a new report from a machine back from away before its one summary
# What a machine needs of a scheduled job to run it itself
SCHEDULE_FIELDS = ("id", "title", "text", "folder", "at", "days", "every", "until", "status", "next_run",
                   "last_run", "timeout_minutes")
MACHINE_KINDS = ("code_change", "code_question")  # scheduled Claude tasks a machine can run (research needs accounts)
STATE = DATA_DIR / "node"
# Task fields a machine needs to run one, nothing more
TASK_FIELDS = ("id", "kind", "title", "instruction", "project", "in_place", "branch", "base", "session_id", "job",
               "grants", "todo", "status")


class Offline(RuntimeError):
    """The task's machine didn't come back in time."""


class TurnedDown(RuntimeError):
    """The machine's own check refused the command: it runs nowhere."""


@dataclass
class Copy:
    node: int
    key: str
    folder: Path
    sent: dict  # the manifest the copy was made from


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9_.-]+", "-", text.lower()).strip("-")[:60] or "x"


class Offload:
    def __init__(self, runner: "Runner", hub: Hub) -> None:
        self.runner, self.hub = runner, hub
        self.copies: dict[str, Copy] = {}
        self.waiting: dict[int, tuple[str, asyncio.Future]] = {}  # task -> (run, its ending)
        self.ended: dict[int, tuple[str, dict | None]] = {}  # runs that ended with nobody waiting (while away)
        self.busy: dict[int, tuple[str, float | None, float]] = {}  # task -> (doing, usage reset, when told)
        self.following: dict[int, asyncio.Task] = {}  # remote command task -> its output mirror
        self.seen: dict[int, int] = {}  # machine -> last report applied
        self.synced: dict[int, str] = {}  # machine -> the scheduled jobs it was last sent
        self.away: dict[int, list[str]] = {}  # machine -> its runs reported from while away, for one summary
        self.summing: set[int] = set()
        self.adopted: dict[int, int] = {}  # task a machine ran alone while away, taken up here -> the machine
        self.stops_file = STATE / "stops.json"  # stops for machines that were away, sent when they're back
        self.adoptions_file = STATE / "adopted.json"  # "machine:run" -> the task each run alone became here
        hub.handlers |= {"copy.pack": self._pack, "copy.bundle": self._bundle, "skills.pack": self._skills_pack,
                         "schedule.due": self._due, "report": self._report, "alive": self._alive, "task.nudges": self._nudges, "task.cleared": self._cleared,
                         "task.commit": self._commit, "task.approval": self._approval, "task.propose": self._propose, "task.access": self._access}
        hub.on_connect.append(self._connected)
        hub.on_lost.append(self._gone)

    # ── Names ────────────────────────────────────────────────────

    @staticmethod
    def key(task_id: int) -> str:
        """The copy a task (or command) works in on its machine."""
        return f"{_slug(profile.current().id)}-t{task_id}"

    @staticmethod
    def mirror(project: str) -> str:
        """A project's git history on the machine, shared by its copies."""
        return f"{_slug(profile.current().id)}-{_slug(project)}"

    @staticmethod
    def name(node: int | None) -> str:
        found = machines.get(node) if node is not None else None
        return found.name if found else "another machine"

    def up(self, node: int) -> Link:
        if (found := self.hub.connected(node)) is None:
            raise LinkDown(f"{self.name(node)} isn't connected")
        return found

    # ── Where a job runs ─────────────────────────────────────────

    async def place(self, kind: str, job: dict, asked: str = "") -> placement.Place:
        """Where a new job runs (jarvis.node.placement); here when no machine is paired."""
        if not asked and not machines.paired():
            return placement.HERE
        return await placement.choose(self.hub, kind, job, asked, len(self.runner.in_line), self.runner.capacity)

    # ── Copies ───────────────────────────────────────────────────

    def _sent_file(self, key: str) -> Path:
        return STATE / "copies" / f"{key}.json"

    async def copy_to(self, node: int, key: str, folder: Path, mirror: str) -> dict:
        """Bring the machine's copy `key` up to `folder`'s files (and git history). ValueError if it's too big."""
        up = self.up(node)
        cache = STATE / "hashes" / f"{hashlib.sha1(str(folder.resolve()).encode()).hexdigest()}.json"
        sent = await asyncio.to_thread(sync.manifest, folder, cache)
        if (size := sync.total(sent, list(sent))) > NODE_SYNC_MAX_GB * 1e9:
            raise ValueError(f"{folder.name} is {size / 1e9:.1f} GB, more than the {NODE_SYNC_MAX_GB:g} GB copied to a "
                             "machine (JARVIS_NODE_SYNC_MAX_GB)")
        self.copies[key] = Copy(node, key, folder, sent)
        listing = STATE / "spool" / f"{key}-{secrets.token_hex(4)}.manifest"
        listing.parent.mkdir(parents=True, exist_ok=True)
        listing.write_text(json.dumps(sent))
        git = {"head": head} if (folder / ".git").exists() and (head := await bundles.head(folder)) else None
        got = await up.call("copy.sync", {"key": key, "mirror": mirror, "git": git,
                                          "manifest": self.hub.outbox.offer(listing, remove=True)}, timeout=3600)
        self._sent_file(key).parent.mkdir(parents=True, exist_ok=True)
        write_atomic(self._sent_file(key), json.dumps({"folder": str(folder), "sent": sent}))
        return got

    def _copy(self, node: int, key: str) -> Copy:
        if (found := self.copies.get(key)) is None and self._sent_file(key).exists():
            kept = json.loads(self._sent_file(key).read_text())
            found = self.copies[key] = Copy(node, key, Path(kept["folder"]), kept["sent"])
        if found is None or found.node != node:
            raise Refused("that copy isn't this machine's")
        return found

    async def _pack(self, node: int, body: dict) -> dict:
        """A machine asks for files of its copy: only ones in the manifest it was sent."""
        copy = self._copy(node, str(body["key"]))
        files = [r for r in body.get("files") or [] if isinstance(r, str) and r in copy.sent and sync.allowed(r)]
        archive = await asyncio.to_thread(sync.pack, copy.folder, files,
                                          STATE / "spool" / f"{copy.key}-{secrets.token_hex(4)}.tar")
        return self.hub.outbox.offer(archive, remove=True)

    async def _bundle(self, node: int, body: dict) -> dict:
        """A machine's copy lacks history: a bundle of the folder's HEAD, without commits it already has."""
        copy = self._copy(node, str(body["key"]))
        have = [h for h in body.get("have") or [] if isinstance(h, str) and re.fullmatch(r"[0-9a-f]{40}", h)]
        known = [h for h in have if await bundles.has(copy.folder, h)]
        out = STATE / "spool" / f"{copy.key}-{secrets.token_hex(4)}.bundle"
        code, said = await bundles._git(copy.folder, "bundle", "create", str(out), "HEAD", *(f"^{h}" for h in known),
                                        timeout=600)
        if code:
            raise Refused(f"couldn't bundle the history: {said}"[:300])
        return self.hub.outbox.offer(out, remove=True)

    # ── JARVIS's skills ──────────────────────────────────────────
    # Runs on a machine get the skills switched on here: each run (and the scheduled jobs a machine owns) names them
    # by version, and the machine fetches a version it lacks.

    @staticmethod
    def skills_offer() -> list[dict]:
        """The skills a run here would load, by name and version."""
        return [{"name": s.name, "digest": skills.whole_digest(s.folder)} for s in skills.usable()]

    async def _skills_pack(self, node: int, body: dict) -> dict:
        """A machine fetches skills it was offered: only ones switched on here."""
        names = {str(n) for n in body.get("names") or []}
        usable = [s for s in await asyncio.to_thread(skills.usable) if s.name in names]
        rels = [f"{s.name}/{rel}" for s in usable for rel in skills.files(s.folder)]
        archive = await asyncio.to_thread(sync.pack, skills.home(), rels,
                                          STATE / "spool" / f"skills-{node}-{secrets.token_hex(4)}.tar")
        return self.hub.outbox.offer(archive, remove=True)

    async def copy_back(self, node: int, key: str, folder: Path) -> dict:
        """Bring the run's changes into `folder`. Returns {"changed", "deleted", "kept_apart"}: kept apart are files
        that changed here too, whose machine version was saved beside them."""
        up = self.up(node)
        copy = self._copy(node, key)
        got = await up.call("copy.back", {"key": key}, timeout=600)
        where, changed, deleted, apart = self.name(node), [], [], []
        if got.get("archive"):
            staging = STATE / "staging" / f"{key}-{secrets.token_hex(4)}"
            archive = await transfer.fetch(up, got["archive"], staging.with_suffix(".tar"), timeout=BACK_SECONDS)
            try:
                staging.mkdir(parents=True)
                await asyncio.to_thread(sync.unpack, archive, staging)
                for rel in got.get("changed") or []:
                    if not (staging / rel).is_symlink() and not (staging / rel).is_file():
                        continue
                    now = await asyncio.to_thread(sync.entry, folder, rel)
                    if now != copy.sent.get(rel) and now != await asyncio.to_thread(sync.entry, staging, rel):
                        rel_apart = f"{rel}.from-{_slug(where)}"
                        if self._place(staging, rel, folder, rel_apart):
                            apart.append(rel)
                    elif self._place(staging, rel, folder, rel):
                        changed.append(rel)
                        copy.sent[rel] = await asyncio.to_thread(sync.entry, folder, rel)
            finally:
                archive.unlink(missing_ok=True)
                shutil.rmtree(staging, ignore_errors=True)
        for rel in got.get("deleted") or []:
            if not isinstance(rel, str):
                continue
            if await asyncio.to_thread(sync.entry, folder, rel) == copy.sent.get(rel):
                if sync.remove(folder, [rel]):
                    deleted.append(rel)
                    copy.sent.pop(rel, None)
            else:
                apart.append(rel)
        await up.call("copy.backed", {"key": key})
        write_atomic(self._sent_file(key), json.dumps({"folder": str(folder), "sent": copy.sent}))
        return {"changed": changed, "deleted": deleted, "kept_apart": apart}

    @staticmethod
    def _place(staging: Path, rel: str, folder: Path, to: str) -> bool:
        """Move a staged file into `folder` as `to`, never through a symlink or out of the folder."""
        if (target := sync._safe(folder, to)) is None:
            return False
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.is_dir() and not target.is_symlink():
            return False
        if target.is_symlink() or target.exists():
            target.unlink()
        shutil.move(str(staging / rel), str(target))
        return True

    async def forget(self, node: int, key: str) -> None:
        """Remove the machine's copy (best effort: it prunes old copies itself)."""
        self.copies.pop(key, None)
        self._sent_file(key).unlink(missing_ok=True)
        with contextlib.suppress(LinkDown, Refused, TimeoutError):
            await self.up(node).call("copy.forget", {"key": key}, timeout=120)

    # ── Projects that live on a machine ──────────────────────────

    async def register_there(self, name: str, path: str | None, aliases: list[str] | None, about: str,
                             in_place: bool, machine: str) -> dict:
        """Add a project living on `machine`: that machine checks (or makes) its folder first."""
        found = machines.find(machine)
        checked = await self.folder_op(found.id, "check", {"path": path or "", "name": name})
        made = await asyncio.to_thread(projects.register_there, name, found.id, checked["path"], checked["state"],
                                       aliases, about, in_place)
        return made | {"machine": found.name}

    async def folder_op(self, node: int, op: str, body: dict) -> dict:
        """A git or folder step (git_ops.OPS) on the machine a project lives on. ValueError if it's away or says no."""
        try:
            return await self.up(node).call(f"folder.{op}", body, timeout=900)
        except LinkDown:
            raise ValueError(f"{self.name(node)}, where that project lives, isn't online.") from None
        except Refused as e:
            raise ValueError(str(e)) from None

    # ── Scheduled jobs of projects that live on a machine ────────
    # That machine owns them: it keeps their times and starts each when due, so one never runs twice. A command it
    # runs itself and reports. A Claude task it hands to this computer when the link is up (schedule.due), so it runs
    # as any task does; while this computer is away it runs the task on its own and reports it on return ("away"),
    # when it's taken up here as the task it was. Runs reported after being away come together in one summary.

    @staticmethod
    def owner(job: dict) -> int | None:
        """The machine that runs a scheduled job itself: a command job, or a code task, whose project lives there."""
        if job["kind"] not in ("command", "task") or not job.get("project") \
                or (job["kind"] == "task" and job.get("task_kind") not in MACHINE_KINDS):
            return None
        try:
            return projects.find(job["project"]).node
        except ValueError:
            return None

    def _sent_job(self, job: dict) -> dict:
        """What a machine keeps of a job it owns; for a Claude task, enough to run it alone."""
        sent = {k: job[k] for k in SCHEDULE_FIELDS}
        if job["kind"] != "task":
            return sent
        project = projects.find(job["project"])
        return sent | {"kind": "task", "task_kind": job["task_kind"], "project": project.name,
                       "folder": str(project.path), "in_place": project.in_place and job["task_kind"] == "code_change",
                       "prompt": scheduler.task_prompt(job)}

    async def sync_schedules(self) -> None:
        """Send each connected machine its jobs, do-not-disturb and switched-on skills, when they've changed since last
        sent."""
        if not self.hub.conns:
            return
        jobs = [j for j in schedules.items() if self.owner(j)]
        offered = await asyncio.to_thread(self.skills_offer) if any(j["kind"] == "task" for j in jobs) else []
        for node in list(self.hub.conns):
            mine = [self._sent_job(j) for j in jobs if self.owner(j) == node]
            body = {"jobs": mine, "hold": hold.status(), "skills": offered}
            if self.synced.get(node) == (digest := json.dumps(body, sort_keys=True)):
                continue
            with contextlib.suppress(LinkDown, Refused, TimeoutError):
                await self.up(node).call("schedule.sync", body, timeout=300)
                self.synced[node] = digest

    async def _due(self, node: int, body: dict) -> dict:
        """A machine says one of its scheduled Claude tasks is due ({"job", "due", "at": its time now}): start it here,
        as the scheduler would, once per due time (asked again after a dropped reply, it gets the same task)."""
        job = schedules.get(int(body["job"]))
        if not job or self.owner(job) != node or job["kind"] != "task":
            raise Refused("that scheduled task isn't this machine's")
        due, now = str(body["due"]), float(body.get("at") or time.time())
        if (json.loads(job["run"] or "null") or {}).get("due") == due:
            return {"task": job["last_task"], "next_run": job["next_run"]}
        nxt = schedules.following(job, now)
        last = self.runner.store.get(job["last_task"]) if job["last_task"] else None
        if last and last["status"] in ACTIVE:
            schedules.update(job["id"], **scheduler.moved_on(nxt), last_result="Skipped a run: the previous one was "
                                                                               "still going.")
            return {"skipped": True, "next_run": nxt}
        task = self.runner.start(job["task_kind"], job["title"], scheduler.task_prompt(job), job["project"],
                                 job=job["id"])
        schedules.update(job["id"], **scheduler.moved_on(nxt), last_run=now, runs=job["runs"] + 1, fails=0,
                         last_task=task["id"], run=json.dumps({"task": task["id"], "started": now, "due": due}))
        return {"task": task["id"], "next_run": nxt}

    def _adoptions(self) -> dict[str, int]:
        try:
            return json.loads(self.adoptions_file.read_text())
        except (OSError, ValueError):
            return {}

    async def _adopt(self, node: int, run: str, data: dict) -> dict:
        """A machine ran a scheduled Claude task on its own while this computer was away: record it as the task it
        is, and take it up here as after a restart (its run may still be going there, or already have ended)."""
        job = schedules.get(int(data["job"]))
        if not job or self.owner(job) != node or job["kind"] != "task":
            raise Refused("that scheduled task isn't this machine's")
        kept, key = self._adoptions(), f"{node}:{run}"
        if key in kept and self.runner.store.get(kept[key]):  # told again after a dropped reply
            return {"task": kept[key]}
        r, ran = self.runner, dict(data.get("task") or {})
        task = r.store.create(job["task_kind"], job["title"], str(ran.get("instruction") or job["text"]),
                              job["project"], bool(ran.get("in_place")), job["id"])
        task_id, name, started = task["id"], self.name(node), float(data["started"])
        r.store.set(task_id, node=node, status="running", worktree=ran.get("worktree"), branch=ran.get("branch"),
                    base=ran.get("base"), activity=f"running on {name}")
        r.store.log(task_id, "start", str(ran.get("instruction") or job["text"]))
        r.store.log(task_id, "machine", f"{name} started this scheduled run itself, while JARVIS was away")
        if ran.get("saved"):
            r.store.log(task_id, "saved", f"uncommitted changes in {ran.get('worktree')} saved to {ran['saved']} "
                                          "before this run")
        schedules.update(job["id"], **scheduler.moved_on(data.get("next_run")), last_run=started, runs=job["runs"] + 1,
                         fails=0, last_task=task_id,
                         run=json.dumps({"task": task_id, "started": started, "due": data.get("due")}))
        self.adoptions_file.parent.mkdir(parents=True, exist_ok=True)
        write_atomic(self.adoptions_file, json.dumps(kept | {key: task_id}))
        self.adopted[task_id] = node
        r.take_up(task_id)
        return {"task": task_id}

    def while_away(self, task_id: int, status: str, said: str, quiet: bool = False) -> bool:
        """A task's run has ended: True if it ran alone while this computer was away, and its ending goes into the one
        summary of what ran then (one that needs the user is said at once; a quiet one isn't said at all)."""
        node = self.adopted.pop(task_id, None)
        if node is None or quiet or status not in ("done", "failed"):
            return False
        self._away_said(node, said)
        return True

    def _away_said(self, node: int, said: str) -> None:
        self.away.setdefault(node, []).append(said)
        if node not in self.summing:
            self.summing.add(node)
            self.runner._background(self._sum_up(node))

    async def _scheduled(self, node: int, body: dict) -> None:
        """A machine ran one of its scheduled commands: record it as a command task, update the job, and say so (or
        keep it for the one summary of what happened while away)."""
        job = schedules.get(int(body["job"]))
        if not job or self.owner(job) != node:
            raise Refused("that scheduled job isn't this machine's")
        r, ok = self.runner, body.get("code") == 0
        started, ended = float(body["started"]), float(body["ended"])
        how = {"folder": job["folder"], "started": started, "ended": ended, "code": body.get("code"),
               "outcome": command_jobs.DONE if ok else command_jobs.FAILED if body.get("code") is not None
               else command_jobs.LOST, "attempt": 1}
        task = r.store.create(COMMAND, job["title"], job["text"], job["project"], job=job["id"], command=how)
        r.commands.folder.mkdir(parents=True, exist_ok=True)
        r.commands.log_file(task["id"]).write_text(str(body.get("output") or ""))
        took = command_jobs.took(ended - started)
        said = (f"{job['title']} ran on {self.name(node)} and is done, in {took}." if ok else
                f"{job['title']} ran on {self.name(node)} and failed ({command_jobs.ending(how)}), after {took}.")
        schedules.update(job["id"], last_run=started, last_task=task["id"], runs=job["runs"] + 1,
                         last_result=said[:300], run=json.dumps({"task": task["id"], "started": started} |
                                                                {k: how[k] for k in ("ended", "outcome", "code")}),
                         **({"next_run": body["next_run"]} if body.get("next_run") else {}))
        r.store.set(task["id"], node=node)
        if body.get("away"):
            r._finish(task["id"], "done" if ok else "failed", said, quiet=True, seen=1)
            self._away_said(node, said)
        else:
            r._finish(task["id"], "done" if ok else "failed", said, title=job["title"])

    async def _sum_up(self, node: int, wait: float = SUMMARY_WAIT) -> None:
        """One summary of the runs a machine reports from while away, once its reports stop coming and the tasks it
        ran alone have been taken up and ended here."""
        count = -1
        while count != len(self.away.get(node, [])) or node in self.adopted.values():
            count = len(self.away.get(node, []))
            await asyncio.sleep(wait)
        said = self.away.pop(node, [])
        self.summing.discard(node)
        if not said:
            return
        text = (f"While JARVIS was away, {self.name(node)} ran {len(said)} scheduled job{'s' * (len(said) != 1)}: "
                + " ".join(said))
        self.runner.broadcast({"type": "alert", "text": text})
        await self.runner._push(f"{self.name(node)} while JARVIS was away", text[:400], "/", f"away-{node}")

    # ── Commands ─────────────────────────────────────────────────

    async def start_command(self, task: dict, node: int, project: projects.Project, folder: Path, approved: str) -> None:
        """Start the command there, in a copy of the project, or in its own folder when it lives there; then mirror it
        here until it ends."""
        task_id, key = task["id"], self.key(task["id"])
        where = {"task": task_id, "command": task["instruction"], "title": task["title"], "approved": approved}
        if project.node:
            where["path"] = str(folder)
        else:
            self.runner.store.set(task_id, activity=f"copying {project.name} to {self.name(node)}")
            copied = await self.copy_to(node, key, project.path, self.mirror(project.name))
            self.runner.store.log(task_id, "machine", f"copied {project.name} to {self.name(node)}: "
                                                      f"{copied['copied']} new or changed, {copied['removed']} removed")
            rel = str(folder.resolve().relative_to(project.path.resolve()))
            where |= {"key": key, "rel": "" if rel == "." else rel}
        runs = self.runner.commands
        runs.folder.mkdir(parents=True, exist_ok=True)
        got = await self.up(node).call("job.start", where, timeout=60)
        if why := got.get("refused"):
            raise TurnedDown(str(why))
        runs.log_file(task_id).write_bytes(b"")
        write_atomic(runs.folder / f"{task_id}.json", json.dumps({
            "id": task_id, "title": task["title"], "command": task["instruction"], "folder": str(folder), "pid": 0,
            "started": time.time(), "node": node} | ({"lives": True} if project.node else {})))
        self.follow(task_id)

    def follow(self, task_id: int) -> None:
        if task_id not in self.following or self.following[task_id].done():
            self.following[task_id] = asyncio.create_task(self._follow(task_id))

    def resume_following(self) -> None:
        """After a restart: mirror every remote command still running."""
        for task in self.runner.store.active():
            if task["kind"] == COMMAND and task["status"] == "running" and task.get("node"):
                self.follow(task["id"])

    async def _follow(self, task_id: int) -> None:
        """Mirror a remote command's output here until it ends, bring its files back, then write its exit status."""
        runs = self.runner.commands
        record = runs.get(task_id)
        if record is None:
            return
        node, offset, ended = record["node"], runs.log_file(task_id).stat().st_size, None
        while ended is None:
            if runs._stopped(record):
                return
            try:
                got = await self.up(node).call("job.poll", {"task": task_id, "offset": offset}, timeout=60)
            except (LinkDown, Refused, TimeoutError):
                await asyncio.sleep(POLL)
                continue
            if data := base64.b64decode(got.get("data") or ""):
                with open(runs.log_file(task_id), "ab") as f:
                    f.write(data)
                offset += len(data)
            ended = got.get("ended")
            if ended is None:
                await asyncio.sleep(POLL)
        outcome, code = ended
        task = self.runner.store.get(task_id)
        if record.get("lives"):  # it ran in the project's own folder there: nothing to bring back
            pass
        elif not got.get("gone") and task and outcome != command_jobs.STOPPED:
            try:
                back = await self.copy_back(node, self.key(task_id), self._project_folder(task, record))
                self.runner.store.log(task_id, "machine", self.said_back(back, node))
            except Exception as e:
                log.warning("command task %s: its files didn't come back from %s: %s", task_id, self.name(node), e)
                self.runner.store.log(task_id, "machine", f"its files didn't come back from {self.name(node)}: {e}"[:300])
        if not record.get("lives"):
            await self.forget(node, self.key(task_id))
        if code is None:
            write_atomic(runs.folder / f"{task_id}.lost", "")
        else:
            write_atomic(runs.folder / f"{task_id}.exit", str(code))

    @staticmethod
    def _project_folder(task: dict, record: dict) -> Path:
        """The folder a command's copy was made from: its project's."""
        if task.get("project"):
            with contextlib.suppress(ValueError):
                return projects.find(task["project"]).path
        return Path(record["folder"])

    def said_back(self, back: dict, node: int) -> str:
        n = len(back["changed"])
        parts = [f"{n} changed file{'s' * (n != 1)} brought back from {self.name(node)}"]
        if back["deleted"]:
            parts.append(f"{len(back['deleted'])} deleted")
        if back["kept_apart"]:
            parts.append(f"kept beside the originals, as they changed here too: {', '.join(back['kept_apart'][:10])}")
        return "; ".join(parts)

    async def stop_command(self, task: dict) -> None:
        """Stop a remote command: marked stopped here at once, stopped there now or once the machine is back."""
        runs, task_id = self.runner.commands, task["id"]
        if (record := runs.get(task_id)) is None or runs.how(record) is not None:
            raise ValueError(f"Task {task_id}'s command isn't running.")
        write_atomic(runs.folder / f"{task_id}.stopped", json.dumps({"at": time.time()}))
        await self._stop_there("job", record["node"], task_id)

    async def _stop_there(self, what: str, node: int, task_id: int) -> None:
        try:
            await self.up(node).call(f"{what}.stop", {"task": task_id}, timeout=30)
        except (LinkDown, Refused, TimeoutError):
            self._keep_stop(what, node, task_id)

    def _keep_stop(self, what: str, node: int, task_id: int) -> None:
        kept = self._stops()
        kept.append([what, node, task_id])
        self.stops_file.parent.mkdir(parents=True, exist_ok=True)
        write_atomic(self.stops_file, json.dumps(kept))

    def _stops(self) -> list[list]:
        try:
            return json.loads(self.stops_file.read_text())
        except (OSError, ValueError):
            return []

    async def _connected(self, node: int) -> None:
        """A machine is back: send the stops it missed, and its scheduled jobs afresh."""
        self.synced.pop(node, None)
        await self.sync_schedules()
        kept, left = self._stops(), []
        for what, there, task_id in kept:
            if there != node:
                left.append([what, there, task_id])
                continue
            with contextlib.suppress(LinkDown, Refused, TimeoutError):
                await self.up(node).call(f"{what}.stop", {"task": task_id}, timeout=30)
        if left != kept:
            write_atomic(self.stops_file, json.dumps(left))

    # ── Claude tasks ─────────────────────────────────────────────

    async def run_task(self, task: dict, prompt: str, reattach: bool = False) -> Outcome:
        """Run a task on its machine and bring its changes back. With `reattach` (after this computer restarted),
        a run still going there, or one that ended meanwhile, is taken up instead of starting another."""
        task_id, node = task["id"], task["node"]
        up = await self.reachable(task)
        lives = bool(self.runner._lives_on(task))  # in its own folder there, not a copy
        where = {"folder": str(self.folder(task))} if lives else {"key": self.key(task_id)}
        running = (await up.call("task.running", {}, timeout=120))["runs"]
        run = running.get(str(task_id))
        if run and not reattach:  # an older run, from before this computer restarted: it gives way to the new one
            await self._stop_there("task", node, task_id)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._ending(task_id, run), 60)
            run = None
        ended = self.ended.pop(task_id, None) if reattach and not run else None
        if ended:
            outcome = ended[1]
        else:
            if not run and not lives:
                folder = self.folder(task)
                self.runner.store.set(task_id, activity=f"copying {task['project']} to {self.name(node)}")
                await self.copy_to(node, self.key(task_id), folder, self.mirror(task["project"]))
            try:
                if not run:
                    run = secrets.token_hex(6)
                    self._ending(task_id, run)  # waited for before it starts: a quick run may end at once
                    offered = await asyncio.to_thread(self.skills_offer)
                    await up.call("task.run", {"task": {k: task.get(k) for k in TASK_FIELDS}, "prompt": prompt,
                                               "run": run, "skills": offered} | where, timeout=600)
                    self.runner.store.set(task_id, activity=f"running on {self.name(node)}")
                outcome = await self._ending(task_id, run)
            except asyncio.CancelledError:
                if not self.runner.closing:  # stopped by the user: stop it there, keep what it did
                    await self._stop_there("task", node, task_id)
                    if not lives:
                        with contextlib.suppress(Exception):
                            await asyncio.wait_for(self._bring_back(task), 120)
                raise
        if not lives:
            await self._bring_back(task)
        if outcome is None:
            return Outcome("", f"it was cut off on {self.name(node)}, which stopped or restarted; reply to carry on",
                           0, "passing")
        resets = outcome.get("resets")  # a machine on an older JARVIS doesn't send it
        return Outcome(str(outcome.get("result") or ""), str(outcome.get("error") or ""), float(outcome.get("cost") or 0),
                       str(outcome.get("kind") or ""), float(resets) if isinstance(resets, (int, float)) else None)

    @staticmethod
    def folder(task: dict) -> Path:
        return Path(task["worktree"]) if task.get("worktree") else projects.find(task["project"]).path

    async def _bring_back(self, task: dict) -> None:
        """A code change's edits come back into its folder here (a question changed nothing)."""
        if task["kind"] != "code_change":
            return
        back = await self.copy_back(task["node"], self.key(task["id"]), self.folder(task))
        self.runner.store.log(task["id"], "machine", self.said_back(back, task["node"]))

    def _ending(self, task_id: int, run: str) -> asyncio.Future:
        if (waiting := self.waiting.get(task_id)) and waiting[0] == run:
            return waiting[1]
        future = asyncio.get_running_loop().create_future()
        self.waiting[task_id] = (run, future)

        def done(_) -> None:
            if self.waiting.get(task_id, ("", None))[1] is future:
                del self.waiting[task_id]

        future.add_done_callback(done)
        return future

    async def reachable(self, task: dict) -> Link:
        """The task's machine, waiting up to NODE_WAIT_MINUTES for it (its session or project is there). Offline
        after."""
        node = task["node"]
        deadline = time.monotonic() + NODE_WAIT_MINUTES * 60
        said = False
        while (up := self.hub.connected(node)) is None:
            if time.monotonic() >= deadline:
                raise Offline(f"{self.name(node)}, where this task's work is, has been unreachable for "
                              f"{NODE_WAIT_MINUTES:g} minutes. Reply to carry on once it's back.")
            if not said:
                self.runner.store.set(task["id"], activity=f"waiting for {self.name(node)} to come online")
                said = True
            await asyncio.sleep(POLL)
        return up

    def _ended(self, task_id: int, run: str, outcome: dict | None) -> None:
        waiting = self.waiting.get(task_id)
        if waiting and waiting[0] == run and not waiting[1].done():
            waiting[1].set_result(outcome)
        else:
            self.ended[task_id] = (run, outcome)

    # ── What a machine sends ─────────────────────────────────────

    def _own(self, node: int, task_id: object) -> dict:
        task = self.runner.store.get(int(task_id)) if isinstance(task_id, int) or str(task_id).isdigit() else None
        if not task or task.get("node") != node:
            raise Refused("that task isn't running on this machine")
        return task

    def _item(self, node: int, task: dict, item: object) -> dict:
        """An approval request from a machine, described here from the command itself."""
        if not isinstance(item, dict) or item.get("tool") not in HOST_COMMANDS.values():
            raise Refused("that isn't a command request")
        command = str(item.get("command") or "").strip()
        if not command or (why := commands.refused(command, bool(task["in_place"]))):
            raise Refused(f"not asked: {why or 'there is no command'}")
        return {"tool": item["tool"], "command": command, "folder": str(item.get("folder") or ""),
                "why": commands.describe(command) or str(item.get("why") or "needs a go-ahead")[:200],
                "remember": remembered.pieces(command), "machine": self.name(node)}

    async def _report(self, node: int, body: dict) -> dict:
        """One journal event from a machine, applied once, in order."""
        seq = int(body["seq"])
        if seq <= self.seen.get(node, 0):
            return {}
        if body["kind"] == "scheduled":  # a run of one of its own scheduled jobs, not a task's
            await self._scheduled(node, body.get("body") or {})
            self.seen[node] = seq
            return {}
        if body["kind"] == "away":  # a scheduled task it began alone: from here on known by the id it gets now
            got = await self._adopt(node, str(body["run"]), body.get("body") or {})
            self.seen[node] = seq
            return got
        task = self._own(node, body["task"])
        task_id, data, kind = task["id"], body.get("body") or {}, str(body["kind"])
        r = self.runner
        if kind == "session":
            r.session(task_id, str(data["session_id"]))
        elif kind == "activity":
            r.alive(task_id)
            r.activity(task_id, str(data["text"])[:300])
        elif kind in ("progress", "subagent", "ask", "flag"):
            getattr(r, kind)(task_id, str(data.get("text") or data.get("question") or "")[:2000])
        elif kind == "approval":
            r.request_approval(task_id, self._item(node, task, data.get("item")))
        elif kind == "access":
            with contextlib.suppress(ValueError):
                r.request_access(task_id, list(data.get("names") or []), str(data.get("reason") or ""))
        elif kind == "ended":
            self._ended(task_id, str(body["run"]), data.get("outcome"))
        self.seen[node] = seq
        return {}

    async def _alive(self, node: int, body: dict) -> dict:
        task = self._own(node, body.get("task"))
        self.runner.alive(task["id"])
        self.busy[task["id"]] = (str(body.get("busy") or ""), body.get("limited"), time.time())
        return {}

    async def _nudges(self, node: int, body: dict) -> dict:
        return {"messages": self.runner.take_nudges(self._own(node, body["task"])["id"])}

    async def _cleared(self, node: int, body: dict) -> dict:
        task = self._own(node, body["task"])
        return {"cleared": self.runner.cleared(task["id"], self._item(node, task, body.get("item")))}

    async def _commit(self, node: int, body: dict) -> dict:
        task = self._own(node, body["task"])
        # A copy of the project there sends back its files, not commits: only in the project's own folder does one stay
        return {"ordered": self.runner._lives_on(task) == node and self.runner.commit_ordered(task["id"])}

    async def _approval(self, node: int, body: dict) -> dict:
        task = self._own(node, body["task"])
        return {k: v for k, v in self.runner.request_approval(task["id"], self._item(node, task, body.get("item"))).items()
                if k != "nonce_hash"}

    async def _propose(self, node: int, body: dict) -> dict:
        return self.runner.propose(self._own(node, body["task"])["id"], dict(body.get("args") or {}))

    async def _access(self, node: int, body: dict) -> dict:
        task = self._own(node, body["task"])
        return {"asked": self.runner.request_access(task["id"], list(body.get("names") or []), str(body.get("reason") or ""))}

    # ── Watching over it ─────────────────────────────────────────

    def working(self) -> dict[int, list[int]]:
        """Machine -> its active tasks and commands. One waiting here for a usage limit's reset isn't work there."""
        found: dict[int, list[int]] = {}
        for task in self.runner.store.active():
            if task.get("node") and task["status"] in ("queued", "running") and task["id"] not in self.runner.limit_waits:
                found.setdefault(task["node"], []).append(task["id"])
        return found

    def excuse(self, task: dict, now: float) -> str:
        """Why a task running on a machine may be quiet, or "" (for the stall watchdog)."""
        if not task.get("node"):
            return ""
        if self.hub.connected(task["node"]) is None:
            return f"it runs on {self.name(task['node'])}, which can't be reached right now"
        doing, limited, at = self.busy.get(task["id"], ("", None, 0.0))
        if limited and limited > now:
            return f"waiting for {self.name(task['node'])}'s Claude usage limit to reset"
        return doing if doing and now - at < 120 else ""

    def lost(self, now: float | None = None) -> list[dict]:
        """Machines with work, unreachable for NODE_LOST_MINUTES or more (for the notices)."""
        now = now or time.time()
        found = []
        for node, ids in self.working().items():
            if self.hub.connected(node) is not None:
                continue
            seen = machines.get(node)
            gone = self.hub.lost_at.get(node) or (seen.last_seen if seen else 0) or now
            if (minutes := (now - gone) / 60) >= NODE_LOST_MINUTES:
                found.append({"node": node, "name": self.name(node), "minutes": int(minutes), "since": gone,
                              "tasks": ids})
        return found

    def _gone(self, node: int) -> None:
        """A machine's link dropped: if it has work and stays away UNREACHABLE_NOTE seconds, the bell says so."""
        async def note() -> None:
            await asyncio.sleep(UNREACHABLE_NOTE)
            if self.hub.connected(node) is None and (ids := self.working().get(node)):
                bell.add(f"{self.name(node)} can't be reached; its work ({', '.join(map(str, ids))}) carries on there "
                         "and reports back once it's online.", "warn")

        if self.working().get(node):
            self.runner._background(note())
