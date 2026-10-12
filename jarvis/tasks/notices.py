"""Notices: JARVIS's news about itself, said unasked, every MINUTES inside the task runner.

  tasks    a question unanswered for MINUTES, a run cut off by a restart, a running task gone quiet (jarvis.tasks.stall)
  usage    Claude's usage limits nearly or all used, once per limit window (jarvis.brain.usage)

It reads only JARVIS's own state, never a service: what to watch is each user's choice, made with scheduled jobs
(jarvis.tasks.schedules). Each notice goes out once as a desktop notification, a phone push and a spoken update;
lock-screen surfaces get only `text`. Keys already given live in data/notices.json, and an ongoing notice (a stall)
is forgotten once it clears, so it can recur. Nothing is checked in quiet hours or do-not-disturb; what still holds
is said after. A failing check is logged and skipped.
"""

import asyncio
import hashlib
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from jarvis.core import profile
from jarvis.core.config import QUIET_HOURS
from jarvis.core.files import write_atomic
from jarvis.notify.push import Push, task_target
from jarvis.tasks import hold
from jarvis.tasks.scheduler import sentence

log = logging.getLogger("jarvis.tasks.notices")
MINUTES = 15
MAX_NOTICES = 5  # per round; the rest wait for the next
KEEP_DAYS = 30  # how long given keys are remembered
CHECK_TIMEOUT = 60
TITLES = {"tasks": "Tasks", "usage": "Claude usage"}  # push titles


@dataclass(frozen=True)
class Notice:
    key: str  # stable id for de-duplication
    source: str
    text: str  # one short sentence; shown on the lock screen
    task_id: int | None = None
    spoken: str = ""  # said instead of `text`
    ongoing: bool = False  # a condition: forgotten once it clears, so it can recur

    @property
    def said(self) -> str:
        return self.spoken or self.text


Check = Callable[[datetime], Awaitable[list[Notice]]]
Notify = Callable[[str, str], Awaitable[object]]


def push_target(n: Notice) -> tuple[str, str]:
    """(tap URL, notification tag); a shared tag lets newer pushes about the same task replace older ones."""
    if n.task_id is not None:
        return task_target(n.task_id)
    return f"/?alert={n.source}", "alert-" + hashlib.sha256(n.key.encode()).hexdigest()[:16]


# ── Given state ─────────────────────────────────────────────────


class Seen:
    """Keys already given, and ongoing ones per check not yet cleared."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.keys: dict[str, float] = {}
        self.ongoing: dict[str, list[str]] = {}
        try:
            data = json.loads(path.read_text())
            self.keys = dict(data["seen"])
            self.ongoing = {k: list(v) for k, v in data.get("ongoing", {}).items()}
        except FileNotFoundError:
            pass
        except (ValueError, KeyError, TypeError):
            log.warning("notice state in %s is unreadable; starting afresh", path.name)

    def __contains__(self, key: str) -> bool:
        return key in self.keys

    def add(self, notices: list[Notice], now: float) -> None:
        for n in notices:
            self.keys.setdefault(n.key, now)
            if n.ongoing and n.key not in self.ongoing.setdefault(n.source, []):
                self.ongoing[n.source].append(n.key)

    def cleared(self, source: str, found: list[Notice]) -> None:
        """Forget ongoing notices `source` no longer reports, so they can be given again."""
        still = {n.key for n in found}
        for k in self.ongoing.get(source, []):
            if k not in still:
                self.keys.pop(k, None)
        self.ongoing[source] = [k for k in self.ongoing.get(source, []) if k in still]

    def save(self, now: float) -> None:
        self.keys = {k: t for k, t in self.keys.items() if now - t < KEEP_DAYS * 86400}
        write_atomic(self.path, json.dumps({"seen": self.keys, "ongoing": {k: v for k, v in self.ongoing.items() if v}}))


# ── The loop ────────────────────────────────────────────────────


class Notices:
    def __init__(
        self,
        checks: dict[str, Check],
        notify: Notify,
        announce: Callable[[dict], None] | None = None,
        state_file: Path | None = None,
        minutes: int = MINUTES,
        quiet_hours: str = QUIET_HOURS,
        push: Push | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(profile.tz()),
        sleep: Callable[[float], Awaitable[object]] = asyncio.sleep,
    ) -> None:
        self.checks = checks
        self.notify = notify
        self.announce = announce
        self.push = push
        self.seen = Seen(state_file or profile.current().notices_file)
        self.minutes = minutes
        self.quiet = hold.parse_quiet_hours(quiet_hours)
        self.clock = clock
        self.sleep = sleep

    async def run(self) -> None:
        """Check now, then every `minutes`, forever."""
        while True:
            try:
                await self.check()
            except Exception:
                log.exception("notices failed")
            await self.sleep(self.minutes * 60)

    async def check(self) -> list[Notice]:
        """One round; returns the notices given."""
        now = self.clock()
        if hold.in_quiet_hours(now, self.quiet) or hold.held(now.timestamp()):
            return []
        results = await asyncio.gather(*(self._gather(name, fn, now) for name, fn in self.checks.items()))
        stamp = now.timestamp()
        fresh: dict[str, Notice] = {}
        for name, found in zip(self.checks, results):
            if found is None:  # a failed check clears nothing
                continue
            self.seen.cleared(name, found)
            for n in found:
                if n.key not in self.seen:
                    fresh.setdefault(n.key, n)
        given = list(fresh.values())[:MAX_NOTICES]

        for n in given:
            try:
                await self.notify(f"JARVIS · {n.source}", n.text)
            except Exception as e:
                log.warning("notice notification failed: %s", type(e).__name__)
        if given and self.announce:
            self.announce({"type": "alert", "text": "Heads up. " + " ".join(n.said for n in given),
                           "tasks": [n.task_id for n in given if n.task_id is not None], "urgency": "soon",
                           "pushed": self.push is not None})
        self.seen.add(given, stamp)
        self.seen.save(stamp)
        if self.push:  # after announcing, so a slow push service doesn't delay the voice
            await asyncio.gather(*(self._push(n) for n in given))
        if given:
            log.info("notices: gave %d", len(given))
        return given

    async def _push(self, n: Notice) -> None:
        try:
            await self.push(TITLES.get(n.source, n.source), n.text, *push_target(n))
        except Exception as e:
            log.warning("notice push failed: %s", type(e).__name__)

    async def _gather(self, name: str, check: Check, now: datetime) -> list[Notice] | None:
        """A check's notices, or None if it failed."""
        try:
            return await asyncio.wait_for(check(now), CHECK_TIMEOUT)
        except Exception as e:  # log the type only, to keep secrets out
            log.warning("notice check %s failed: %s", name, type(e).__name__)
        return None


# ── Checks ──────────────────────────────────────────────────────


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def task_notices(store, stalls: Callable[[float], list[dict]] | None = None, minutes: int = MINUTES,
                 lost: Callable[[float], list[dict]] | None = None) -> Check:
    """Tasks the runner didn't announce: questions unanswered for `minutes`, runs cut off by a restart, stalls
    (`stalls`, Runner.stalls) and machines unreachable with work on them (`lost`, Offload.lost); both ongoing, so
    given again only after recovering."""

    async def check(now: datetime) -> list[Notice]:
        out = []
        for m in lost(now.timestamp()) if lost else []:
            out.append(Notice(f"machine:{m['node']}:lost:{int(m['since'])}", "tasks",
                              f"{m['name']} has been unreachable for {_plural(m['minutes'], 'minute')}, with task "
                              f"{', '.join(map(str, m['tasks']))} on it. It carries on there and reports back once "
                              "it's online.", task_id=m["tasks"][0], ongoing=True))
        for t in stalls(now.timestamp()) if stalls else []:
            name, quiet = f"Task {t['id']}, {t['title']},", _plural(t["stalled"]["minutes"], "minute")
            out.append(Notice(f"task:{t['id']}:stalled", "tasks",
                              f"{name} seems stuck: no progress for {quiet}. Nudge it or stop it?",
                              task_id=t["id"], ongoing=True,
                              spoken=f"{name} seems stuck: no sign of progress for {quiet}. Shall I nudge it with a "
                                     "message, or stop it?"))
        for t in store.recent():
            name = f"Task {t['id']}, {t['title']},"
            key = f"task:{t['id']}:{t['status']}:{int(t['updated'])}"
            if t["status"] == "waiting" and not t["seen"] and now.timestamp() - t["updated"] >= minutes * 60:
                out.append(Notice(key, "tasks", sentence(f"{name} is still waiting for your answer: {t['question']}"),
                                  task_id=t["id"]))
            elif t["status"] == "stopped" and t["error"]:  # stopped by the runner, not the user
                out.append(Notice(key, "tasks", f"{name} stopped. {t['error']}", task_id=t["id"]))
        return out

    return check


async def usage_notices(now: datetime) -> list[Notice]:
    from jarvis.brain import usage

    return [Notice(key, "usage", text) for key, text in usage.alerts(now.timestamp())]


def _carry_over(path: Path) -> None:
    """Migration: keep the keys data/heartbeat.json recorded, so nothing it already said is said again."""
    if not path.exists() and (old := path.with_name("heartbeat.json")).exists():
        old.replace(path)


def make(store, notify: Notify, announce: Callable[[dict], None] | None = None, push: Push | None = None,
         stalls: Callable[[float], list[dict]] | None = None, lost: Callable[[float], list[dict]] | None = None,
         **settings) -> Notices:
    """The runner's Notices. `stalls` is Runner.stalls, `lost` Offload.lost; `settings` override Notices kwargs."""
    if "state_file" not in settings:
        _carry_over(profile.current().notices_file)
    checks = {"tasks": task_notices(store, stalls, lost=lost), "usage": usage_notices}
    return Notices(checks, notify, announce, push=push, **settings)
