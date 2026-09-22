"""The heartbeat: every HEARTBEAT_MINUTES the task runner wakes up and flags what's new, unasked.

Each beat gathers cheap signals with JARVIS's read-only tools:

  calendar  events starting within HEARTBEAT_EVENT_MINUTES
  email     unread mail that Gmail marks important
  github    PRs waiting for the user's review; mentions, assignments and security alerts
  chat      unread Google Chat DMs and @mentions, by who wrote them (only where Chat is set up)
  tasks     background tasks still waiting for an answer, cut off by a restart, or stuck (jarvis.tasks.stall)
  usage     Claude's usage limits getting close or used up, once per limit window (jarvis.brain.usage)
  signins   a Google account, GitHub or GitLab signed out, or its token about to expire (jarvis.integrations.signins)

Those are the built-in kinds of source, in a registry (KINDS) another module adds its own kind to with `register`.
Which sources a beat checks, and their settings (calendar's lead time, the Gmail search), are the user's: kept in the
settings table (jarvis.state.settings, key heartbeat_sources) and changed in the control panel or by voice, read
afresh at every beat. Only what differs from a kind's defaults is kept, so a user who never changed anything gets the
list above. A kind that allows it (email) can be added more than once, each with its own settings and title: another
Gmail search to flag, which the user, a skill or a watch asks for (jarvis.tools.heartbeat).

Anything not flagged before becomes a Mac notification, a push to the phone when push is on
(jarvis.notify.push), and an event on the runner's stream that the voice agent speaks at the next
pause. Notifications and pushes show on the lock screen, so they carry only an alert's `text`:
what someone wrote goes only in its `spoken` words, for the voice and triage. What has been
flagged is kept in data/heartbeat.json, so nothing is flagged twice.
Nothing is checked in quiet hours, or while do not disturb is on (jarvis.tasks.hold); what turns up
meanwhile is flagged at the first beat after. An alert about something ongoing (a sign-in that has
stopped working) is flagged once, and again only if it clears and then comes back.
The first time mail, GitHub and Chat answer, the items already there are only recorded, rather
than flagged as a backlog.

Read-only by design: it calls only read tools, and never sends, creates, starts or runs anything.
A failing source is logged and skipped; the beat goes on without it.
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
MAX_ALERTS = 5  # per beat; the rest wait for the next one
KEEP_DAYS = 30  # how long a flagged item is remembered
SOURCE_TIMEOUT = 60
MAIL_QUERY = "is:unread is:important newer_than:1d"  # the email source's Gmail search, unless the user sets another
GITHUB_REASONS = {
    "mention": "You were mentioned on {repo}: {title}",
    "team_mention": "Your team was mentioned on {repo}: {title}",
    "assign": "You were assigned on {repo}: {title}",
    "security_alert": "Security alert on {repo}: {title}",
}
SNIPPET_WORDS = 12  # of a Chat message, in the spoken alert only


@dataclass(frozen=True)
class Alert:
    key: str  # stable id, so the same thing is never flagged twice
    source: str
    text: str  # one short sentence, fit to be spoken, and shown on the lock screen
    soft: bool = False  # a model may overrule it when triage is on
    task_id: int | None = None
    spoken: str = ""  # said and shown to triage instead of `text`, never notified or pushed: it may quote someone
    ongoing: bool = False  # a condition, not an item: forgotten once its source answers without it, so it can recur
    at: float | None = None  # the time it's about (a meeting's start), so the voice can judge when to mention it
    urgent: bool = False  # triage found it can't wait: it may cut into a conversation (jarvis.voice.floor)

    @property
    def said(self) -> str:
        return self.spoken or self.text


class SourceFailed(RuntimeError):
    """A source failure whose message JARVIS wrote itself, so it's safe to log."""


Source = Callable[[datetime], Awaitable[list[Alert]]]
Notify = Callable[[str, str], Awaitable[object]]


def push_target(a: Alert) -> tuple[str, str]:
    """Where tapping the alert's phone notification opens JARVIS, and the tag that lets a newer
    notification about the same task replace it."""
    if a.task_id is not None:
        return task_target(a.task_id)
    return f"/?alert={a.source}", "alert-" + hashlib.sha256(a.key.encode()).hexdigest()[:16]


# ── Quiet hours ─────────────────────────────────────────────────


def parse_quiet_hours(spec: str) -> tuple[clock_time, clock_time] | None:
    """'23:00-08:00' -> (23:00, 08:00). Empty, or the same start and end, means never quiet."""
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


# ── What has been flagged ───────────────────────────────────────


class Seen:
    """Keys already flagged, and the sources that have answered at least once."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.keys: dict[str, float] = {}
        self.started: set[str] = set()
        self.ongoing: dict[str, list[str]] = {}  # by source: the ongoing alerts flagged and not yet cleared
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
        """Forget the source's ongoing alerts it no longer reports, so they're flagged again if they come back;
        returns their keys."""
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
        sources: dict[str, Source] | Callable[[], dict[str, Source]],  # by name; a function is asked at every beat
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
        """Beat now, then every `minutes`, for as long as the runner lives."""
        while True:
            try:
                await self.beat()
            except Exception:
                log.exception("heartbeat failed")
            await self.sleep(self.minutes * 60)

    async def beat(self) -> list[Alert]:
        """One check. Returns what it flagged."""
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
            if baseline(name) and name not in self.seen.started:  # its first answer: what's there already isn't news
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
        if self.push:  # after the announcement, together: a slow push service shouldn't hold up the voice
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
        except Exception as e:  # never let one source stop the beat; the type alone keeps secrets out of the log
            log.warning("heartbeat source %s failed: %s", name, type(e).__name__)
        return None

    async def _triage(self, alerts: list[Alert], stamp: float) -> list[Alert]:
        """Let the model drop soft alerts not worth an interruption. Dropped ones count as seen."""
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
    """`read(label)` on every connected Google account, each in its own thread, all at once.
    Raises only if all fail."""
    from ai_armory.toolsets.google.common import connected

    from jarvis.tools import google  # sets AI Armory's Google tool sets up with JARVIS's accounts

    google.fresh()  # and with the profile's name and time zone as they are now
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
    """PRs waiting for the user's review and their unread notifications, through the logged-in `gh` CLI."""
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
    """Unread DMs and @mentions. The alert names who wrote and where, never what: the words go only in `spoken`."""
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
    """Tasks the user should hear about that the runner hasn't announced on its own: a question
    unanswered for a whole beat, a run cut off by a restart, or one that seems stuck (`stalls`, the runner's
    jarvis.tasks.stall check): flagged once, and again only if it moves on and then stalls afresh."""

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
            elif t["status"] == "stopped" and t["error"]:  # stopped by the runner, not by the user
                out.append(Alert(key, "tasks", f"{name} stopped. {t['error']}", task_id=t["id"]))
        return out

    return source


async def usage_news(now: datetime) -> list[Alert]:
    """Claude's usage limits getting close or used up, from what the CLI last reported: once per limit window."""
    from jarvis.brain import usage

    return [Alert(key, "usage", text) for key, text in usage.alerts(now.timestamp())]


async def signin_news(now: datetime) -> list[Alert]:
    """Connections signed out, or whose tokens expire soon: each flagged once, and again only if it clears and
    comes back."""
    from jarvis.integrations import signins

    return [Alert(p.key, "signins", p.text, ongoing=True) for p in await signins.check(now)]


# ── The registry, and the sources the user has on ──────────────

SETTINGS_KEY = "heartbeat_sources"  # in the settings table: what the user changed, by source id, as JSON
MAX_ADDED = 10
TITLE_CHARS = 40


@dataclass(frozen=True)
class Context:
    """What a source may need from the task runner: its task store, and its check for stuck tasks (Runner.stalls)."""
    store: object
    stalls: Callable[[float], list[dict]] | None = None


@dataclass(frozen=True)
class Setting:
    key: str
    label: str  # in the control panel
    default: str
    clean: Callable[[str], str]  # the value as kept; raises ValueError, saying what to change, for one it can't take


@dataclass(frozen=True)
class Kind:
    """A kind of source a beat can check. `make` builds the source from the user's settings, each one filled in."""
    name: str
    title: str  # in the control panel, and the phone notification's title
    about: str  # what it flags, in a few words
    make: Callable[[dict[str, str], Context], Source]
    settings: tuple[Setting, ...] = ()
    on: bool = True  # unless the user switches it off
    baseline: bool = False  # its first answer is only recorded, rather than flagged as a backlog
    ready: Callable[[], bool] = lambda: True  # whether it can run here at all (Chat: only where an account has it)
    more: bool = False  # the user may add more of it, each with a title and settings of its own


KINDS: dict[str, Kind] = {}


def register(kind: Kind) -> Kind:
    """Adds a kind of source, or replaces the one of its name. From the next beat it's checked unless the user switches
    it off, and the control panel lists it with its settings."""
    KINDS[kind.name] = kind
    return kind


def baseline(name: str) -> bool:
    """Whether source `name` (one the user added is named kind:slug) only records what's there at its first answer."""
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
    """A source as the user has it: a kind's own, its id the kind's name, or one they added, its id kind:slug."""
    id: str
    kind: Kind
    title: str
    on: bool
    settings: dict[str, str]  # every setting, the kind's default where the user kept none
    added: str = ""  # who added it: "" for a kind's own

    def described(self) -> dict:
        return {"id": self.id, "kind": self.kind.name, "title": self.title, "about": self.kind.about, "on": self.on,
                "ready": self.kind.ready(), "added": self.added, "settings": _described(self.kind, self.settings)}


def _described(kind: Kind, values: dict[str, str] | None = None) -> list[dict]:
    return [{"key": s.key, "label": s.label, "default": s.default, "value": (values or {}).get(s.key, s.default)}
            for s in kind.settings]


def _stored() -> dict[str, dict]:
    """What the user changed, by source id. A settings table that can't be read counts as nothing changed."""
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
    return ":" in id_ or kind.on  # one the user added is on until they switch it off


def _configured(id_: str, kind: Kind, entry: dict) -> Configured:
    values = {s.key: str(entry.get(s.key) or s.default) for s in kind.settings}
    return Configured(id_, kind, str(entry.get("title") or kind.title),
                      bool(entry.get("on", _on_unless_said(id_, kind))), values, str(entry.get("added", "")))


def lineup() -> list[Configured]:
    """Every source the user has, on or off: each kind's own, then those they added. One added of a kind no longer
    registered is left out, but kept, for when it's back."""
    kept = _stored()
    return ([_configured(name, kind, kept.get(name, {})) for name, kind in KINDS.items()]
            + [_configured(id_, KINDS[e["kind"]], e) for id_, e in kept.items() if ":" in id_ and e.get("kind") in KINDS])


def described() -> dict:
    """The user's sources, as the control panel and the voice tools show them, and the kinds more can be added of."""
    return {"sources": [c.described() for c in lineup()],
            "kinds": [{"name": k.name, "title": k.title, "about": k.about, "settings": _described(k)}
                      for k in KINDS.values() if k.more]}


def _find(id_: str) -> Configured:
    if found := next((c for c in lineup() if c.id == id_), None):
        return found
    raise ValueError(f"There's no heartbeat source called {id_}.")


def _cleaned(kind: Kind, values: dict) -> dict[str, str | None]:
    """Each setting given, as kept: None where it goes back to the default."""
    known = {s.key: s for s in kind.settings}
    out: dict[str, str | None] = {}
    for key, value in values.items():
        if key not in known:
            raise ValueError(f"{kind.title} has no setting called {key}.")
        value = known[key].clean(v) if (v := str(value).strip()) else ""
        out[key] = value if value and value != known[key].default else None
    return out


def change_source(id_: str, on: bool | None = None, values: dict | None = None) -> Configured:
    """Switches source `id_` on or off, and changes its settings: an empty value goes back to the default. Raises
    ValueError, saying what's wrong, for an unknown source or setting, or a value its kind can't take."""
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
    """Another source of a kind that allows more (another Gmail search), on, with a title and every setting given;
    `added` says who asked (the user in the panel, by voice, a skill or a watch). Raises ValueError as change_source."""
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
    """Takes out a source the user added; a kind's own can only be switched off."""
    kept = _stored()
    if ":" not in id_ or id_ not in kept:
        raise ValueError(f"{id_} is built in: switch it off instead." if id_ in KINDS
                         else f"There's no heartbeat source called {id_}.")
    del kept[id_]
    _keep(kept)


def sources_on(store, stalls: Callable[[float], list[dict]] | None = None) -> dict[str, Source]:
    """The sources the user has on, by id, each set up as they set it: what one beat checks."""
    context, out = Context(store, stalls), {}
    for c in lineup():
        if not c.on:
            continue
        try:
            if c.kind.ready():
                out[c.id] = c.kind.make(c.settings, context)
        except Exception as e:  # a kind registered elsewhere mustn't stop the beat
            log.warning("heartbeat source %s couldn't be set up: %s", c.id, type(e).__name__)
    return out


# ── Optional triage by a short model call ──────────────────────

def triage_system() -> str:
    return prompts.load("heartbeat_triage", user_name=profile.name())


# The triage's reply: the items kept, by number, each with whether it can't wait
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
    """The alerts worth an interruption, by key, each with whether it can't wait, by one short call to HEARTBEAT_MODEL
    with no tools. ValueError for a reply that isn't JSON matching TRIAGE_SCHEMA, so everything is kept."""
    from jarvis.brain.sdk import ask_json

    prompt = "\n".join(f"{i}. [{a.source}] {a.said}" for i, a in enumerate(alerts, 1))
    found, _ = await ask_json(prompt, TRIAGE_SCHEMA, triage_system(), HEARTBEAT_MODEL, effort="low")
    if found is None:
        raise ValueError("the triage's reply wasn't usable")
    picked = {k["n"]: k["urgent"] for k in found["keep"]}
    return {a.key: picked[i] for i, a in enumerate(alerts, 1) if i in picked}


def make(store, notify: Notify, announce: Callable[[dict], None] | None = None, push: Push | None = None,
         stalls: Callable[[float], list[dict]] | None = None, **settings) -> Heartbeat:
    """The heartbeat the task runner runs, set up from config, checking the sources the user has on at each beat;
    `stalls` is the runner's (Runner.stalls), and `settings` override Heartbeat's own."""
    return Heartbeat(lambda: sources_on(store, stalls), notify, announce, triage=model_triage if HEARTBEAT_TRIAGE else None,
                     push=push, **settings)


if __name__ == "__main__":
    # A dry run: one beat, printed rather than notified, as if nothing had been flagged before.
    # It keeps its state in a throwaway file, so the real heartbeat still flags everything.
    import tempfile

    from jarvis.core.database import Store

    async def _show(title: str, text: str) -> None:
        print(f"{title}: {text}")

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    with tempfile.TemporaryDirectory() as scratch:
        hb = make(Store(), _show, state_file=Path(scratch) / "heartbeat.json", quiet_hours="")
        hb.seen.started = set(sources_on(None))  # not a first beat, so existing mail and GitHub items show too
        asyncio.run(hb.beat())
