"""Heartbeat: every HEARTBEAT_MINUTES, flag what's new without being asked.

Sources live in KINDS (extend with `register`): calendar, email, github, chat, tasks, usage, signins. Which run and
their settings are per user (settings key `heartbeat_sources`), re-read each beat.

New items go out as a Mac notification, a phone push and a spoken update. Lock-screen surfaces get only `text`,
never message content. Seen items live in data/heartbeat.json. Skipped during quiet hours and do-not-disturb.
Read-only: never sends, creates or runs anything. A failing source is logged and skipped.
"""

import asyncio
import hashlib
import json
import logging
import re
import sqlite3
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import datetime, time as clock_time
from email.utils import parseaddr
from functools import partial
from pathlib import Path

from jarvis import prompts
from jarvis.core import profile
from jarvis.core.config import (
    HEARTBEAT_EVENT_MINUTES,
    HEARTBEAT_MINUTES,
    HEARTBEAT_MODEL,
    HEARTBEAT_QUIET_HOURS,
    HEARTBEAT_TRIAGE,
)
from jarvis.core.files import write_atomic
from jarvis.core.proc import run
from jarvis.notify.push import Push, task_target
from jarvis.tasks import hold

log = logging.getLogger("jarvis.tasks.heartbeat")
MAX_ALERTS = 5  # per beat; the rest wait for the next
KEEP_DAYS = 30  # how long flagged keys are remembered
SOURCE_TIMEOUT = 60
MAIL_QUERY = "is:unread is:important newer_than:1d"  # default Gmail search for the email source
GITHUB_REASONS = {
    "mention": "You were mentioned on {repo}: {title}",
    "team_mention": "Your team was mentioned on {repo}: {title}",
    "assign": "You were assigned on {repo}: {title}",
    "security_alert": "Security alert on {repo}: {title}",
}
SNIPPET_WORDS = 12  # Chat message words quoted, in `spoken` only


@dataclass(frozen=True)
class Alert:
    key: str  # stable id for de-duplication
    source: str
    text: str  # one short sentence; shown on the lock screen
    soft: bool = False  # triage may drop it
    task_id: int | None = None
    spoken: str = ""  # voice/triage text; may quote people, so never notified or pushed
    ongoing: bool = False  # a condition: forgotten once it clears, so it can recur
    at: float | None = None  # time it refers to (e.g. meeting start), for voice timing
    urgent: bool = False  # set by triage: may interrupt a conversation

    @property
    def said(self) -> str:
        return self.spoken or self.text


class SourceFailed(RuntimeError):
    """Source failure with a JARVIS-written message, safe to log."""


Source = Callable[[datetime], Awaitable[list[Alert]]]
Notify = Callable[[str, str], Awaitable[object]]


def push_target(a: Alert) -> tuple[str, str]:
    """(tap URL, notification tag); a shared tag lets newer pushes about the same task replace older ones."""
    if a.task_id is not None:
        return task_target(a.task_id)
    return f"/?alert={a.source}", "alert-" + hashlib.sha256(a.key.encode()).hexdigest()[:16]


# ── Quiet hours ─────────────────────────────────────────────────


def parse_quiet_hours(spec: str) -> tuple[clock_time, clock_time] | None:
    """'23:00-08:00' -> (23:00, 08:00); None if empty, invalid, or start == end."""
    if not spec.strip():
        return None
    try:
        start, end = (clock_time.fromisoformat(p.strip().zfill(5)) for p in spec.split("-"))
    except ValueError:
        log.warning("JARVIS_HEARTBEAT_QUIET_HOURS should look like 23:00-08:00; ignoring it")
        return None
    return None if start == end else (start, end)


def in_quiet_hours(now: datetime, quiet: tuple[clock_time, clock_time] | None) -> bool:
    if quiet is None:
        return False
    start, end, t = *quiet, now.time()
    return start <= t < end if start < end else t >= start or t < end


# ── Seen state ──────────────────────────────────────────────────


class Seen:
    """Flagged keys, sources that have answered once, and ongoing alerts per source."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.keys: dict[str, float] = {}
        self.started: set[str] = set()
        self.ongoing: dict[str, list[str]] = {}  # source -> flagged ongoing keys not yet cleared
        try:
            data = json.loads(path.read_text())
            self.keys, self.started = dict(data["seen"]), set(data["started"])
            self.ongoing = {k: list(v) for k, v in data.get("ongoing", {}).items()}
        except FileNotFoundError:
            pass
        except (ValueError, KeyError, TypeError):
            log.warning("heartbeat state in %s is unreadable; starting afresh", path.name)

    def __contains__(self, key: str) -> bool:
        return key in self.keys

    def add(self, alerts: list[Alert], now: float) -> None:
        for a in alerts:
            self.keys.setdefault(a.key, now)
            if a.ongoing and a.key not in self.ongoing.setdefault(a.source, []):
                self.ongoing[a.source].append(a.key)

    def cleared(self, source: str, found: list[Alert]) -> list[str]:
        """Forget ongoing alerts `source` no longer reports, so they can re-flag; returns their keys."""
        still = {a.key for a in found}
        gone = [k for k in self.ongoing.get(source, []) if k not in still]
        for k in gone:
            self.keys.pop(k, None)
        if gone:
            self.ongoing[source] = [k for k in self.ongoing[source] if k in still]
        return gone

    def save(self, now: float) -> None:
        self.keys = {k: t for k, t in self.keys.items() if now - t < KEEP_DAYS * 86400}
        write_atomic(self.path, json.dumps({"started": sorted(self.started), "seen": self.keys,
                                            "ongoing": {k: v for k, v in self.ongoing.items() if v}}))


# ── The beat ────────────────────────────────────────────────────


class Heartbeat:
    def __init__(
        self,
        sources: dict[str, Source] | Callable[[], dict[str, Source]],  # by name; a callable is re-read each beat
        notify: Notify,
        announce: Callable[[dict], None] | None = None,
        state_file: Path | None = None,
        minutes: int = HEARTBEAT_MINUTES,
        quiet_hours: str = HEARTBEAT_QUIET_HOURS,
        triage: Callable[[list[Alert]], Awaitable[set[str] | dict[str, bool]]] | None = None,
        push: Push | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(profile.tz()),
        sleep: Callable[[float], Awaitable[object]] = asyncio.sleep,
    ) -> None:
        self.sources = sources
        self.notify = notify
        self.announce = announce
        self.push = push
        self.seen = Seen(state_file or profile.current().heartbeat_file)
        self.minutes = minutes
        self.quiet = parse_quiet_hours(quiet_hours)
        self.triage = triage
        self.clock = clock
        self.sleep = sleep

    async def run(self) -> None:
        """Beat now, then every `minutes`, forever."""
        while True:
            try:
                await self.beat()
            except Exception:
                log.exception("heartbeat failed")
            await self.sleep(self.minutes * 60)

    async def beat(self) -> list[Alert]:
        """One beat; returns the alerts flagged."""
        now = self.clock()
        if in_quiet_hours(now, self.quiet):
            log.info("heartbeat: quiet hours, nothing checked")
            return []
        if hold.held(now.timestamp()):
            log.info("heartbeat: do not disturb, nothing checked")
            return []

        sources = self.sources() if callable(self.sources) else self.sources
        results = await asyncio.gather(*(self._gather(name, fn, now) for name, fn in sources.items()))
        report = [f"{name} {'failed' if found is None else len(found)}" for name, found in zip(sources, results)]
        stamp = now.timestamp()
        fresh: dict[str, Alert] = {}
        for name, found in zip(sources, results):
            if found is None:
                continue
            if gone := self.seen.cleared(name, found):
                log.info("heartbeat: %s cleared %d ongoing alert%s", name, len(gone), "" if len(gone) == 1 else "s")
            unseen = [a for a in found if a.key not in self.seen]
            if baseline(name) and name not in self.seen.started:  # first answer: existing items aren't news
                self.seen.add(unseen, stamp)
                unseen = []
            self.seen.started.add(name)
            for a in unseen:
                fresh.setdefault(a.key, a)
        new = await self._triage(list(fresh.values()), stamp)
        flagged = new[:MAX_ALERTS]

        for a in flagged:
            try:
                await self.notify(f"JARVIS · {a.source}", a.text)
            except Exception as e:
                log.warning("heartbeat notification failed: %s", type(e).__name__)
        if flagged and self.announce:
            self.announce({"type": "heartbeat", "text": "Heads up. " + " ".join(a.said for a in flagged),
                           "tasks": [a.task_id for a in flagged if a.task_id is not None],
                           "urgency": "urgent" if any(a.urgent for a in flagged) else "soon",
                           "at": min((a.at for a in flagged if a.at is not None), default=None),
                           "pushed": self.push is not None})
        self.seen.add(flagged, stamp)
        self.seen.save(stamp)
        if self.push:  # after announcing, so a slow push service doesn't delay the voice
            await asyncio.gather(*(self._push(a) for a in flagged))
        log.info("heartbeat: %s; %d new, %d flagged", ", ".join(report) or "no sources", len(new), len(flagged))
        return flagged

    async def _push(self, a: Alert) -> None:
        try:
            await self.push(KINDS[a.source].title if a.source in KINDS else a.source, a.text, *push_target(a))
        except Exception as e:
            log.warning("heartbeat push failed: %s", type(e).__name__)

    async def _gather(self, name: str, source: Source, now: datetime) -> list[Alert] | None:
        """A source's alerts, or None if it failed."""
        try:
            return await asyncio.wait_for(source(now), SOURCE_TIMEOUT)
        except SourceFailed as e:
            log.warning("heartbeat source %s failed: %s", name, e)
        except Exception as e:  # log the type only, to keep secrets out
            log.warning("heartbeat source %s failed: %s", name, type(e).__name__)
        return None

    async def _triage(self, alerts: list[Alert], stamp: float) -> list[Alert]:
        """Let the model drop soft alerts and mark urgent ones; dropped ones count as seen. Fails open."""
        soft = [a for a in alerts if a.soft]
        if not (self.triage and soft):
            return alerts
        try:
            keep = await asyncio.wait_for(self.triage(soft), 120)
        except Exception as e:
            log.warning("heartbeat triage failed, keeping everything: %s", type(e).__name__)
            return alerts
        dropped = [a for a in soft if a.key not in keep]
        self.seen.add(dropped, stamp)
        urgent = {k for k, v in keep.items() if v} if isinstance(keep, dict) else set()
        return [replace(a, urgent=True) if a.key in urgent else a for a in alerts if a not in dropped]


# ── Sources, on JARVIS's read-only tools ───────────────────────


def _sentence(text: str) -> str:
    return text if text.endswith((".", "!", "?", "…")) else text + "."


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


async def _each_account(read: Callable[[str], list[dict]]) -> list[dict]:
    """Run `read(label)` for every connected Google account concurrently; SourceFailed only if all fail."""
    from ai_armory.toolsets.google.common import connected

    from jarvis.tools import google  # configures AI Armory's Google toolsets with JARVIS's accounts

    google.fresh()  # pick up the current profile name and time zone
    labels = connected()
    results = await asyncio.gather(*(asyncio.to_thread(read, label) for label in labels), return_exceptions=True)
    found, failed = [], []
    for label, result in zip(labels, results):
        if isinstance(result, BaseException):
            failed.append(f"{label} ({type(result).__name__})")
        else:
            found += result
    if failed and not found:
        raise SourceFailed("every account failed: " + ", ".join(failed))
    if failed:
        log.warning("heartbeat: some Google accounts failed: %s", ", ".join(failed))
    return found


async def calendar_soon(now: datetime, lead: int = HEARTBEAT_EVENT_MINUTES) -> list[Alert]:
    from ai_armory.toolsets.google.calendar import calendar_events

    out = []
    for e in await _each_account(lambda label: calendar_events(days=2, account=label)):
        if "T" not in e["start"]:  # all-day
            continue
        start = datetime.fromisoformat(e["start"])
        minutes = ((start if start.tzinfo else start.replace(tzinfo=profile.tz())) - now).total_seconds() / 60
        if 0 <= minutes <= lead:
            when = "now" if minutes < 1 else f"in {_plural(round(minutes), 'minute')}"
            out.append(Alert(f"calendar:{e['account']}:{e['start']}:{e['title']}", "calendar",
                             f"{e['title']} starts {when}.", at=now.timestamp() + minutes * 60))
    return out


async def important_mail(now: datetime, query: str = MAIL_QUERY) -> list[Alert]:
    from ai_armory.toolsets.google.gmail import gmail_search

    out = []
    for m in await _each_account(lambda label: gmail_search(query, account=label, max_results=10)):
        name, address = parseaddr(m["from"])
        subject = " ".join((m["subject"] or "no subject").split())[:100]
        out.append(Alert(f"email:{m['account']}:{m['id']}", "email",
                         _sentence(f"New email from {name or address or 'someone'}: {subject}"), soft=True))
    return out


async def github_overview() -> dict:
    """Review requests and unread notifications via the logged-in `gh` CLI."""
    reviews, notes = await asyncio.gather(
        run("gh", "search", "prs", "--review-requested=@me", "--state=open", "--limit=20", "--json",
            "repository,title,number,updatedAt,url"),
        run("gh", "api", "notifications", "--jq",
            "[.[] | {repo: .repository.full_name, title: .subject.title, type: .subject.type, reason}] | .[:20]"),
    )
    return {"review_requests": json.loads(reviews), "unread_notifications": json.loads(notes)}


async def github_news(now: datetime) -> list[Alert]:
    o = await github_overview()
    out = []
    for pr in o["review_requests"]:
        repo = pr["repository"].get("name") or pr["repository"].get("nameWithOwner", "")
        out.append(Alert(f"github:review:{pr['url']}", "github",
                         _sentence(f"{repo} pull request {pr['number']} is waiting for your review: {pr['title']}")))
    for n in o["unread_notifications"]:
        if template := GITHUB_REASONS.get(n["reason"]):
            repo = n["repo"].split("/")[-1]
            out.append(Alert(f"github:{n['reason']}:{n['repo']}:{n['type']}:{n['title']}", "github",
                             _sentence(template.format(repo=repo, title=n["title"])), soft=True))
    return out


async def chat_news(now: datetime) -> list[Alert]:
    """Unread DMs and @mentions. `text` names who and where only; message words go in `spoken`."""
    from ai_armory.toolsets.google.chat import first_words, news

    out = []
    for n in await _each_account(news):
        if n["kind"] == "dm":
            text = (f"New Chat message from {n['from']}." if n["count"] == 1
                    else f"{n['count']} new Chat messages from {n['from']}.")
        else:
            text = f"{n['from']} mentioned you in {n['name']} on Chat."
        out.append(Alert(f"chat:{n['account']}:{n['latest']}", "chat", text, soft=True,
                         spoken=_sentence(f"{text[:-1]}: {first_words(n['text'], SNIPPET_WORDS)}")))
    return out


def task_changes(store, minutes: int = HEARTBEAT_MINUTES,
                 stalls: Callable[[float], list[dict]] | None = None) -> Source:
    """Source for tasks the runner didn't announce: questions unanswered for a beat, runs cut off by a restart, and
    stalls (`stalls`; ongoing, so re-flagged only after recovering)."""

    async def source(now: datetime) -> list[Alert]:
        out = []
        for t in stalls(now.timestamp()) if stalls else []:
            name, quiet = f"Task {t['id']}, {t['title']},", _plural(t["stalled"]["minutes"], "minute")
            out.append(Alert(f"task:{t['id']}:stalled", "tasks",
                             f"{name} seems stuck: no progress for {quiet}. Nudge it or stop it?",
                             task_id=t["id"], ongoing=True,
                             spoken=f"{name} seems stuck: no sign of progress for {quiet}. Shall I nudge it with a "
                                    "message, or stop it?"))
        for t in store.recent():
            name = f"Task {t['id']}, {t['title']},"
            key = f"task:{t['id']}:{t['status']}:{int(t['updated'])}"
            if t["status"] == "waiting" and not t["seen"] and now.timestamp() - t["updated"] >= minutes * 60:
                out.append(Alert(key, "tasks", _sentence(f"{name} is still waiting for your answer: {t['question']}"),
                                 task_id=t["id"]))
            elif t["status"] == "stopped" and t["error"]:  # stopped by the runner, not the user
                out.append(Alert(key, "tasks", f"{name} stopped. {t['error']}", task_id=t["id"]))
        return out

    return source


async def usage_news(now: datetime) -> list[Alert]:
    """Claude usage limits nearly or fully used, once per limit window."""
    from jarvis.brain import usage

    return [Alert(key, "usage", text) for key, text in usage.alerts(now.timestamp())]


async def signin_news(now: datetime) -> list[Alert]:
    """Connections signed out or with tokens expiring soon (ongoing alerts)."""
    from jarvis.integrations import signins

    return [Alert(p.key, "signins", p.text, ongoing=True) for p in await signins.check(now)]


# ── Registry and user configuration ────────────────────────────

SETTINGS_KEY = "heartbeat_sources"  # settings row: JSON of user overrides by source id
MAX_ADDED = 10
TITLE_CHARS = 40


@dataclass(frozen=True)
class Context:
    """Runner state a source may need: the task store and Runner.stalls."""
    store: object
    stalls: Callable[[float], list[dict]] | None = None


@dataclass(frozen=True)
class Setting:
    key: str
    label: str  # shown in the control panel
    default: str
    clean: Callable[[str], str]  # normalise; ValueError explains a bad value


@dataclass(frozen=True)
class Kind:
    """A source type. `make` builds the source from fully filled-in settings."""
    name: str
    title: str  # panel label and push title
    about: str  # what it flags, briefly
    make: Callable[[dict[str, str], Context], Source]
    settings: tuple[Setting, ...] = ()
    on: bool = True  # default state
    baseline: bool = False  # first answer is recorded, not flagged as backlog
    ready: Callable[[], bool] = lambda: True  # can it run here at all (Chat needs an account with it)
    more: bool = False  # user may add extra instances with own title and settings


KINDS: dict[str, Kind] = {}


def register(kind: Kind) -> Kind:
    """Add or replace a source kind; it runs from the next beat unless switched off."""
    KINDS[kind.name] = kind
    return kind


def baseline(name: str) -> bool:
    """Whether source `name` (`kind` or `kind:slug`) baselines its first answer."""
    kind = KINDS.get(name.partition(":")[0])
    return bool(kind and kind.baseline)


def _minutes(value: str) -> str:
    try:
        n = int(value)
    except ValueError:
        raise ValueError(f"{value} isn't a whole number of minutes.") from None
    if not 1 <= n <= 240:
        raise ValueError("Pick from 1 to 240 minutes.")
    return str(n)


def _gmail_search(value: str) -> str:
    value = " ".join(value.split())
    if len(value) > 300:
        raise ValueError("Keep the Gmail search to 300 characters.")
    return value


def _chat_ready() -> bool:
    from jarvis.integrations import accounts

    return bool(accounts.load().chat_labels)


for _kind in (
    Kind("calendar", "Calendar", "Meetings about to start",
         lambda s, _: partial(calendar_soon, lead=int(s["minutes"])),
         (Setting("minutes", "Minutes ahead", str(HEARTBEAT_EVENT_MINUTES), _minutes),)),
    Kind("email", "Mail", "New mail matching a Gmail search", lambda s, _: partial(important_mail, query=s["query"]),
         (Setting("query", "Gmail search", MAIL_QUERY, _gmail_search),), baseline=True, more=True),
    Kind("github", "GitHub", "Reviews asked of you; mentions, assignments and security alerts",
         lambda s, _: github_news, baseline=True),
    Kind("chat", "Chat", "Unread Google Chat messages and @mentions", lambda s, _: chat_news, baseline=True,
         ready=_chat_ready),
    Kind("tasks", "Tasks", "Background tasks waiting for you, cut off or stuck",
         lambda s, c: task_changes(c.store, stalls=c.stalls)),
    Kind("usage", "Claude usage", "Claude's usage limits nearly or all used", lambda s, _: usage_news),
    Kind("signins", "Sign-in", "Accounts signed out, or about to be", lambda s, _: signin_news),
):
    register(_kind)


@dataclass(frozen=True)
class Configured:
    """A source as configured: built-in (id = kind name) or user-added (id = kind:slug)."""
    id: str
    kind: Kind
    title: str
    on: bool
    settings: dict[str, str]  # all settings, defaults filled in
    added: str = ""  # who added it; "" for built-in

    def described(self) -> dict:
        return {"id": self.id, "kind": self.kind.name, "title": self.title, "about": self.kind.about, "on": self.on,
                "ready": self.kind.ready(), "added": self.added, "settings": _described(self.kind, self.settings)}


def _described(kind: Kind, values: dict[str, str] | None = None) -> list[dict]:
    return [{"key": s.key, "label": s.label, "default": s.default, "value": (values or {}).get(s.key, s.default)}
            for s in kind.settings]


def _stored() -> dict[str, dict]:
    """User overrides by source id; {} if the settings row is unreadable."""
    from jarvis.state import settings

    try:
        found = json.loads(settings.get(SETTINGS_KEY) or "{}")
    except (sqlite3.Error, ValueError) as e:
        log.warning("heartbeat sources unreadable, so the defaults apply: %s", type(e).__name__)
        return {}
    return {k: v for k, v in found.items() if isinstance(v, dict)} if isinstance(found, dict) else {}


def _keep(kept: dict[str, dict]) -> None:
    from jarvis.state import settings

    kept = {k: v for k, v in kept.items() if v}
    settings.put(SETTINGS_KEY, json.dumps(kept, sort_keys=True) if kept else "")


def _on_unless_said(id_: str, kind: Kind) -> bool:
    return ":" in id_ or kind.on  # user-added sources default to on


def _configured(id_: str, kind: Kind, entry: dict) -> Configured:
    values = {s.key: str(entry.get(s.key) or s.default) for s in kind.settings}
    return Configured(id_, kind, str(entry.get("title") or kind.title),
                      bool(entry.get("on", _on_unless_said(id_, kind))), values, str(entry.get("added", "")))


def lineup() -> list[Configured]:
    """All sources, on or off: built-ins, then user-added. Added ones of an unregistered kind are hidden but kept."""
    kept = _stored()
    return ([_configured(name, kind, kept.get(name, {})) for name, kind in KINDS.items()]
            + [_configured(id_, KINDS[e["kind"]], e) for id_, e in kept.items() if ":" in id_ and e.get("kind") in KINDS])


def described() -> dict:
    """Sources and addable kinds, as shown by the panel and voice tools."""
    return {"sources": [c.described() for c in lineup()],
            "kinds": [{"name": k.name, "title": k.title, "about": k.about, "settings": _described(k)}
                      for k in KINDS.values() if k.more]}


def _find(id_: str) -> Configured:
    if found := next((c for c in lineup() if c.id == id_), None):
        return found
    raise ValueError(f"There's no heartbeat source called {id_}.")


def _cleaned(kind: Kind, values: dict) -> dict[str, str | None]:
    """Cleaned values by key; None where the value reverts to the default."""
    known = {s.key: s for s in kind.settings}
    out: dict[str, str | None] = {}
    for key, value in values.items():
        if key not in known:
            raise ValueError(f"{kind.title} has no setting called {key}.")
        value = known[key].clean(v) if (v := str(value).strip()) else ""
        out[key] = value if value and value != known[key].default else None
    return out


def change_source(id_: str, on: bool | None = None, values: dict | None = None) -> Configured:
    """Toggle source `id_` and/or change settings (empty value resets). ValueError for unknown ids, keys or values."""
    c, kept = _find(id_), _stored()
    entry = kept.setdefault(id_, {})
    for key, value in _cleaned(c.kind, values or {}).items():
        if value is None:
            entry.pop(key, None)
        else:
            entry[key] = value
    if on is not None:
        entry["on"] = on
        if on == _on_unless_said(id_, c.kind):
            del entry["on"]
    _keep(kept)
    return _find(id_)


def add_source(kind: str, title: str, values: dict, added: str) -> Configured:
    """Add another instance of a `more` kind, with a title and all settings; `added` records who asked.

    Raises ValueError like change_source."""
    k = KINDS.get(kind)
    if not (k and k.more):
        raise ValueError(f"Only these can be added more than once: {', '.join(n for n, x in KINDS.items() if x.more)}.")
    if not (title := " ".join(title.split())) or len(title) > TITLE_CHARS:
        raise ValueError(f"Give it a title of up to {TITLE_CHARS} characters.")
    if missing := [s.label for s in k.settings if not str(values.get(s.key, "")).strip()]:
        raise ValueError(f"Say its {', '.join(missing)}.")
    cleaned, kept = _cleaned(k, values), _stored()
    if sum(":" in i for i in kept) >= MAX_ADDED:
        raise ValueError(f"There are {MAX_ADDED} added sources already: remove one first.")
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-") or "source"
    id_, n = f"{kind}:{slug}", 2
    while id_ in kept:
        id_, n = f"{kind}:{slug}-{n}", n + 1
    kept[id_] = {"kind": kind, "title": title, "added": added} | {key: v for key, v in cleaned.items() if v}
    _keep(kept)
    return _find(id_)


def remove_source(id_: str) -> None:
    """Remove a user-added source; built-ins can only be switched off."""
    kept = _stored()
    if ":" not in id_ or id_ not in kept:
        raise ValueError(f"{id_} is built in: switch it off instead." if id_ in KINDS
                         else f"There's no heartbeat source called {id_}.")
    del kept[id_]
    _keep(kept)


def sources_on(store, stalls: Callable[[float], list[dict]] | None = None) -> dict[str, Source]:
    """Enabled, ready sources by id, built from the user's settings."""
    context, out = Context(store, stalls), {}
    for c in lineup():
        if not c.on:
            continue
        try:
            if c.kind.ready():
                out[c.id] = c.kind.make(c.settings, context)
        except Exception as e:  # an external kind mustn't break the beat
            log.warning("heartbeat source %s couldn't be set up: %s", c.id, type(e).__name__)
    return out


# ── Optional triage by a short model call ──────────────────────

def triage_system() -> str:
    return prompts.load("heartbeat_triage", user_name=profile.name())


# Reply: kept items by number, each with an urgent flag
TRIAGE_SCHEMA = {
    "type": "object",
    "properties": {"keep": {"type": "array", "items": {
        "type": "object",
        "properties": {"n": {"type": "integer"}, "urgent": {"type": "boolean"}},
        "required": ["n", "urgent"],
    }}},
    "required": ["keep"],
}


async def model_triage(alerts: list[Alert]) -> dict[str, bool]:
    """key -> urgent for alerts worth interrupting (one tool-less HEARTBEAT_MODEL call).

    ValueError on an unusable reply, so the caller keeps everything."""
    from jarvis.brain.sdk import ask_json

    prompt = "\n".join(f"{i}. [{a.source}] {a.said}" for i, a in enumerate(alerts, 1))
    found, _ = await ask_json(prompt, TRIAGE_SCHEMA, triage_system(), HEARTBEAT_MODEL, effort="low")
    if found is None:
        raise ValueError("the triage's reply wasn't usable")
    picked = {k["n"]: k["urgent"] for k in found["keep"]}
    return {a.key: picked[i] for i, a in enumerate(alerts, 1) if i in picked}


def make(store, notify: Notify, announce: Callable[[dict], None] | None = None, push: Push | None = None,
         stalls: Callable[[float], list[dict]] | None = None, **settings) -> Heartbeat:
    """The runner's Heartbeat from config. `stalls` is Runner.stalls; `settings` override Heartbeat kwargs."""
    return Heartbeat(lambda: sources_on(store, stalls), notify, announce, triage=model_triage if HEARTBEAT_TRIAGE else None,
                     push=push, **settings)


if __name__ == "__main__":
    # Dry run: one beat printed instead of notified, with throwaway state so the real heartbeat is unaffected.
    import tempfile

    from jarvis.core.database import Store

    async def _show(title: str, text: str) -> None:
        print(f"{title}: {text}")

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    with tempfile.TemporaryDirectory() as scratch:
        hb = make(Store(), _show, state_file=Path(scratch) / "heartbeat.json", quiet_hours="")
        hb.seen.started = set(sources_on(None))  # skip baselining so existing items show
        asyncio.run(hb.beat())
