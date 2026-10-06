"""Scheduled jobs: reminders, repeating background tasks and "watch this for me" loops, kept in the schedules table of
data/tasks.db, so they survive restarts. The voice agent's schedule tools (jarvis.tools.schedules) add, change and
cancel them; the task runner's scheduler (jarvis.tasks.scheduler) runs them when they're due.

  reminder  says `text` when due: a Mac notification, a push to the phone and, in a live session, aloud
  task      starts a background task with `text` as its instruction (`task_kind`, research unless said)
  watch     checks every `every` minutes whether `text` has happened, until it has or `until` passes. The check is a
            background task (probe "task"), or, with no model and next to no memory: a web page answering, and
            showing `expect` if given ("url"); mail matching a Gmail search arriving ("gmail"); or a new Chat
            message from someone ("chat")
  command   runs `text`, a shell command, in `folder` at its times, with no model while it runs: each run is a command
            task (jarvis.tasks.command_jobs), as the voice shell's start_job starts, in the task list like any task.
            `timeout_minutes` stops a run that goes on too long, and a run still going holds back the next. Only its
            end wakes a model: a short call sums up a success (unless `summarize` is off), and one reads a failure
            and, with `auto_fix`, decides whether a code_change task in `project` fixes it at once, waits for the
            user's yes, or there's nothing to fix; a fixed command runs once more. `log_file` is a file the command
            writes its own log to, read beside its output; `run` is how its latest run went, and `last_task` that run

A one-off job runs once, at `next_run`. A repeating one runs at a time of day (`at`) on some days of the week (`days`),
or every `every` minutes. Times are local, in the profile's time zone (jarvis.core.profile).

A task keeps a notepad: what its latest run noted for the next (job memory, JSON {"at", "text"}); with `only_changes`
it stays silent unless something changed since. `retries` and `failing` are the scheduler's: how many times a run that
failed on a passing error has been tried again, and the kind of failure last said.

A command job is made, or its command or folder changed, only through the voice brain's schedule tools, whose calls
meet the shell's gate (jarvis.safety.shell.ShellGate) exactly as a command run there would: a risky one needs the
user's yes, and one touching secrets is refused.
"""

import json
import re
import shlex
import sqlite3
import time
from contextlib import AbstractContextManager
from datetime import datetime, time as clock_time, timedelta
from pathlib import Path

from jarvis.core import database, profile

KINDS = ("reminder", "task", "watch", "command")
PROBES = ("task", "url", "gmail", "chat")
ACTIVE, PAUSED, DONE, CANCELLED, EXPIRED = "active", "paused", "done", "cancelled", "expired"
LIVE = (ACTIVE, PAUSED)
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
REPEATS = {"daily": DAYS, "weekdays": DAYS[:5], "weekends": DAYS[5:]}
# Minutes between a watch's checks: the least, and what it is unless said. A check by a background task starts a
# Claude session, so it's spaced out more than the cheap ones.
MIN_EVERY = {"task": 10, "url": 1, "gmail": 5, "chat": 5}
EVERY = {"task": 15, "url": 5, "gmail": 10, "chat": 10}
MIN_TASK_EVERY = 30  # a background task repeating by the clock, not by the day
MIN_REMINDER_EVERY = 5
MIN_COMMAND_EVERY = 10
WATCH_HOURS, WATCH_MAX_DAYS = 24, 7  # how long a watch lasts unless said, and at most
MAX_LIVE, MAX_TASK_WATCHES = 30, 5  # jobs active or paused at once, and of them watches checked by background tasks
TASK_KINDS = ("research", "code_question", "code_change")
WATCH_TASK_KINDS = ("research", "code_question")  # a check only reads

_SCHEMA = """
create table schedules (
    id integer primary key autoincrement,  -- never reused, so an old number never names a new job
    kind text not null,         -- reminder, task, watch, command
    title text not null,
    text text not null,         -- what a reminder says, a task's instruction, what a watch waits for, or the command
    task_kind text,             -- task, and watch probe task: the background task's kind
    project text,               -- for the code kinds, and the project a command belongs to
    at text,                    -- HH:MM, for a job repeating on days of the week
    days text,                  -- those days, e.g. mon,tue,wed,thu,fri
    every integer,              -- minutes between runs, for a job repeating by the clock (every watch)
    until real,                 -- when it stops; null: never
    probe text,                 -- watch: task, url, gmail or chat
    target text,                -- watch: the URL, the Gmail search, or the person
    expect text,                -- watch url: text the page must show
    status text not null,       -- active, paused, done, cancelled, expired
    next_run real,              -- null once there's no run to come
    last_run real,
    runs integer not null default 0,
    fails integer not null default 0,   -- checks in a row that couldn't be made
    last_task integer,          -- the background task of its latest run
    last_result text,           -- what its latest run found, in a sentence
    baseline text,              -- watch chat: JSON list of the messages already there at its first check
    created real not null,
    updated real not null,
    notepad text,               -- task: what its latest run noted for the next one (job memory)
    only_changes integer not null default 0,  -- task: 1 stays silent unless something changed since the last run
    retries integer not null default 0,       -- task: retries so far of a run that failed on a passing error
    failing text,               -- task: the kind of failure last said (passing, limit, error), till a run succeeds
    folder text,                -- command: the folder it runs in
    timeout_minutes integer,    -- command: a run is stopped after this long; null: never
    summarize integer not null default 1,  -- command: 1 sums up a run that succeeds with a short model call
    auto_fix integer not null default 0,   -- command: 1 lets a task fix a run that fails, then reruns it once
    log_file text,              -- command: the log it writes itself, a path or pattern in its folder
    run text                    -- command: JSON of its latest run: its command task, and once it ends how it went
)"""
# Columns added before versions were kept, for databases made before them (database.migrate)
_ADDED = {"notepad": "text", "only_changes": "integer not null default 0", "retries": "integer not null default 0",
          "failing": "text", "folder": "text", "timeout_minutes": "integer",
          "summarize": "integer not null default 1", "auto_fix": "integer not null default 0", "log_file": "text",
          "run": "text"}


def _db() -> AbstractContextManager[sqlite3.Connection]:
    return database.table("schedules", _SCHEMA, added=_ADDED)


def _now(now: datetime | None) -> datetime:
    return now or datetime.now(profile.tz())


# ── Times ───────────────────────────────────────────────────────


def parse_time(text: str, now: datetime | None = None) -> datetime:
    """A local time: '17:00' (its next occurrence, today or tomorrow) or a date and time, '2026-10-06T17:00'."""
    now, text = _now(now), text.strip()
    if re.fullmatch(r"\d{1,2}:\d{2}", text):
        t = clock_time.fromisoformat(text.zfill(5))
        at = datetime.combine(now.date(), t, profile.tz())
        return at if at > now else datetime.combine(now.date() + timedelta(days=1), t, profile.tz())
    try:
        at = datetime.fromisoformat(text)
    except ValueError:
        raise ValueError(f"'{text}' isn't a time: give HH:MM, or a local date and time like 2026-10-06T17:00.") from None
    return at.replace(tzinfo=profile.tz()) if at.tzinfo is None else at.astimezone(profile.tz())


def parse_days(repeat: str) -> str:
    """'weekdays' -> 'mon,tue,wed,thu,fri'; also daily, weekends, or days named like 'mon, wed, friday'."""
    repeat = repeat.strip().lower()
    if repeat in REPEATS:
        return ",".join(REPEATS[repeat])
    named = [w[:3] for w in re.split(r"[\s,]+", repeat) if w]
    if not named or any(d not in DAYS for d in named):
        raise ValueError(f"repeat '{repeat}' should be daily, weekdays, weekends, or days like mon,wed,fri.")
    return ",".join(d for d in DAYS if d in named)


def _on_days(at: str, days: str, moment: datetime, step: int) -> datetime:
    """The nearest `at` o'clock on one of `days`: strictly after `moment` going forward (`step` 1), at or before it
    going back (-1)."""
    t, wanted = clock_time.fromisoformat(at), {DAYS.index(d) for d in days.split(",")}
    moment = moment.astimezone(profile.tz())
    for i in range(8):
        day = moment.date() + timedelta(days=i * step)
        when = datetime.combine(day, t, profile.tz())
        if day.weekday() in wanted and (when > moment if step > 0 else when <= moment):
            return when
    raise ValueError(f"no day in {days}")  # unreachable with at least one day


def next_on_days(at: str, days: str, after: datetime) -> datetime:
    return _on_days(at, days, after, 1)


def last_on_days(at: str, days: str, before: datetime) -> datetime:
    return _on_days(at, days, before, -1)


def following(job: dict, after: float) -> float | None:
    """When a job runs next after a run at `after`; None for a one-off job, or once that would be past its end."""
    if job["every"]:
        at = after + job["every"] * 60
    elif job["days"]:
        at = next_on_days(job["at"], job["days"], datetime.fromtimestamp(after, profile.tz())).timestamp()
    else:
        return None
    return None if job["until"] and at > job["until"] else at


def clock(ts: float, now: datetime | None = None) -> str:
    """'17:00', 'tomorrow 09:00' or 'Mon 12 Oct 09:00', as near as it is."""
    at, today = datetime.fromtimestamp(ts, profile.tz()), _now(now).date()
    if at.date() == today:
        return f"{at:%H:%M}"
    if at.date() == today + timedelta(days=1):
        return f"tomorrow {at:%H:%M}"
    return f"{at:%a} {at.day} {at:%b %H:%M}"


_DAYS_SAID = {",".join(REPEATS["daily"]): "every day", ",".join(REPEATS["weekdays"]): "every weekday",
              ",".join(REPEATS["weekends"]): "at weekends"}


def describe(job: dict, now: datetime | None = None) -> str:
    """When the job runs, in words: 'every weekday at 09:00', 'once, at 17:00', 'every 15 minutes until 18:00'."""
    if n := job["every"]:
        said = f"every {n} minutes" if n % 60 else ("every hour" if n == 60 else f"every {n // 60} hours")
    elif job["days"]:
        said = f"{_DAYS_SAID.get(job['days']) or 'on ' + job['days'].replace(',', ', ')} at {job['at']}"
    else:
        said = f"once, at {clock(job['next_run'] or job['last_run'] or job['created'], now)}"
    return said + (f", until {clock(job['until'], now)}" if job["until"] else "")


CHECKS = {"task": "a background task checks", "url": "JARVIS checks the page", "gmail": "JARVIS searches Gmail",
          "chat": "JARVIS looks at Google Chat"}


def check_text(job: dict) -> str:
    """How a watch checks, in words: 'JARVIS checks the page: https://..., for "v2"'; empty for other jobs."""
    if job["kind"] != "watch":
        return ""
    said = CHECKS[job["probe"]] + (f": {job['target']}" if job["target"] else "")
    return said + (f", for \"{job['expect']}\"" if job["expect"] else "")


# ── The jobs ────────────────────────────────────────────────────


_IN_LIVE = f"status in ({', '.join('?' * len(LIVE))})"


def _rows(sql: str, params: tuple = ()) -> list[dict]:
    with _db() as db:
        return [dict(r) for r in db.execute(sql, params)]


def get(job_id: int) -> dict | None:
    return next(iter(_rows("select * from schedules where id = ?", (job_id,))), None)


def find(job_id: int) -> dict:
    if not (job := get(job_id)):
        raise ValueError(f"There's no scheduled job {job_id}.")
    return job


def titles(ids: set[int]) -> dict[int, str]:
    """The titles of these jobs, by id, finished ones included."""
    found = _rows(f"select id, title from schedules where id in ({', '.join('?' * len(ids))})", tuple(ids))
    return {r["id"]: r["title"] for r in found}


def items(finished_days: float = 0) -> list[dict]:
    """Active and paused jobs, soonest first; and those finished in the last `finished_days`, latest first."""
    live = _rows(f"select * from schedules where {_IN_LIVE} order by next_run is null, next_run", LIVE)
    if not finished_days:
        return live
    return live + _rows(f"select * from schedules where not {_IN_LIVE} and updated > ? order by updated desc",
                        (*LIVE, time.time() - finished_days * 86400))


def due(now: float) -> list[dict]:
    """Active jobs whose next run has come, earliest first."""
    return _rows("select * from schedules where status = ? and next_run <= ? order by next_run", (ACTIVE, now))


def lapsed(now: float) -> list[dict]:
    """Active or paused jobs whose end has passed."""
    return _rows(f"select * from schedules where {_IN_LIVE} and until <= ?", (*LIVE, now))


def update(job_id: int, **fields) -> dict:
    """Set fields on a job (their names come from code, never from input); returns it."""
    columns = ", ".join(f"{k} = ?" for k in fields)
    with _db() as db:
        db.execute(f"update schedules set {columns} where id = ?", (*fields.values(), job_id))
    return find(job_id)


def baseline(job: dict) -> set[str]:
    return set(json.loads(job["baseline"] or "[]"))


def notepad(job: dict) -> dict | None:
    """A task job's notepad, {"at", "text"}: what its latest run noted for the next; None if there's none."""
    try:
        found = json.loads(job.get("notepad") or "null")
    except ValueError:
        return None
    return found if isinstance(found, dict) and found.get("text") else None


def prune(days: float) -> int:
    """Forget jobs that finished more than `days` ago; returns how many."""
    with _db() as db:
        return db.execute(f"delete from schedules where not {_IN_LIVE} and updated < ?",
                          (*LIVE, time.time() - days * 86400)).rowcount


def _clean(text: str) -> str:
    return " ".join((text or "").split())


def _timing(job: dict, when: str, in_minutes: int, repeat: str, every_minutes: int, until: str,
            now: datetime) -> dict:
    """The job's at, days, every, until and next_run, checked. A job being changed keeps its end unless given one."""
    kind, probe = job["kind"], job.get("probe") or "task"
    if repeat and every_minutes:
        raise ValueError("Give repeat (days of the week) or every_minutes, not both.")
    if when and in_minutes:
        raise ValueError("Give when or in_minutes, not both.")
    if in_minutes < 0 or every_minutes < 0:
        raise ValueError("Minutes can't be negative.")
    start = parse_time(when, now) if when else now + timedelta(minutes=in_minutes) if in_minutes else None
    out = {"at": None, "days": None, "every": None}
    if kind == "watch" and repeat:
        raise ValueError("A watch checks every so many minutes (every_minutes), not on days of the week.")
    if repeat:
        if not when:
            raise ValueError("A job repeating on days of the week needs when: the time of day, as HH:MM.")
        out["at"], out["days"] = f"{start:%H:%M}", parse_days(repeat)
        # A bare time of day starts from now; a date and time from that moment
        from_ = now if re.fullmatch(r"\s*\d{1,2}:\d{2}\s*", when) else max(now, start - timedelta(seconds=1))
        first = next_on_days(out["at"], out["days"], from_)
    elif every_minutes or kind == "watch":
        every = every_minutes or EVERY[probe]
        least = {"watch": MIN_EVERY[probe], "task": MIN_TASK_EVERY, "command": MIN_COMMAND_EVERY}.get(kind,
                                                                                                  MIN_REMINDER_EVERY)
        if every < least:
            raise ValueError(f"Every {least} minutes at most often for this kind of job.")
        out["every"] = every
        # A watch looks straight away; a repeating reminder or task first runs one interval from now
        first = start or (now if kind == "watch" else now + timedelta(minutes=every))
    else:
        if not start:
            raise ValueError("Say when: when (HH:MM, or a local date and time) or in_minutes; or repeat or every_minutes.")
        first = start
    if first < now - timedelta(minutes=1):
        raise ValueError(f"{first:%Y-%m-%d %H:%M} has already passed.")
    out["next_run"] = max(first, now).timestamp()
    out["until"] = _end(job | out, until, now, kept=job.get("until"))
    return out


def _end(job: dict, until: str, now: datetime, kept: float | None = None) -> float | None:
    """When a job stops: `until`, else the end it had (`kept`), else a watch's default; checked against its timing
    and its next run."""
    end = parse_time(until, now) if until else datetime.fromtimestamp(kept, profile.tz()) if kept else None
    if job["kind"] == "watch":
        end = end or now + timedelta(hours=WATCH_HOURS)
        if end > now + timedelta(days=WATCH_MAX_DAYS):
            raise ValueError(f"A watch lasts at most {WATCH_MAX_DAYS} days.")
    if not end:
        return None
    if end.timestamp() <= max(job["next_run"] or 0, now.timestamp()):
        raise ValueError(f"until ({end:%Y-%m-%d %H:%M}) must be after the next run.")
    if not (job["days"] or job["every"]):
        raise ValueError("until is only for repeating jobs and watches.")
    return end.timestamp()


def _what(kind: str, task_kind: str, project: str, probe: str, target: str, expect: str) -> dict:
    """The job's task_kind, project, probe, target and expect, checked."""
    out = {"task_kind": None, "project": None, "probe": None, "target": None, "expect": None}
    if kind == "watch":
        probe = probe or "task"
        if probe not in PROBES:
            raise ValueError(f"probe must be one of {', '.join(PROBES)}.")
        out["probe"] = probe
        if probe == "url":
            if not re.match(r"https?://\S+$", target.strip()):
                raise ValueError("A url watch needs target: the page's full http(s) address.")
            out["target"], out["expect"] = target.strip(), _clean(expect) or None
        elif probe in ("gmail", "chat"):
            if not _clean(target):
                raise ValueError("A gmail watch needs target: a Gmail search, e.g. from:sam@acme.com."
                                 if probe == "gmail" else "A chat watch needs target: the person's name.")
            out["target"] = _clean(target)
    if kind == "task" or out["probe"] == "task":
        allowed = TASK_KINDS if kind == "task" else WATCH_TASK_KINDS
        task_kind = task_kind or "research"
        if task_kind not in allowed:
            raise ValueError(f"task_kind must be one of {', '.join(allowed)}.")
        out["task_kind"] = task_kind
        if task_kind != "research":
            if not project:
                raise ValueError(f"A {task_kind} job needs a project.")
            from jarvis.tasks import projects

            out["project"] = projects.find(project).name
    return out


def place(folder: str, project: str = "") -> tuple[str, str | None]:
    """Where a command runs, as a full path, and the project it belongs to (None if none): `folder` (~ allowed;
    relative to the project's folder if a project is named), else the project's own folder. Its project is the one
    whose folder holds it most closely, the one named first."""
    from jarvis.safety.commands import secret_path
    from jarvis.tasks import projects
    from jarvis.tasks.command_jobs import project_of

    named = projects.find(project) if project.strip() else None
    if folder.strip():
        path = Path(folder.strip()).expanduser()
        if not path.is_absolute():
            if not named:
                raise ValueError("Give folder as a full path (~ allowed), or name the project it's relative to.")
            path = named.path / path
    elif named:
        path = named.path
    else:
        raise ValueError("A command job needs folder, where it runs, or its project.")
    path = path.resolve()
    if not path.is_dir():
        raise ValueError(f"{path} isn't a folder.")
    if secret_path(path):
        raise ValueError("That folder holds secrets, which stay out of reach.")
    return str(path), project_of(path, named)


def command_line(command: str, folder: str) -> str:
    """A command job's command as the shell's gate weighs it: run in its folder."""
    return f"cd {shlex.quote(folder)} && {command.strip()}"


COMMAND_TOOLS = ("schedule_add", "schedule_change")  # the voice tools that can keep a command (jarvis.tools.schedules)


def command_called(tool: str, args: dict) -> str | None:
    """The command a schedule_add or schedule_change call would keep for JARVIS to run, put as command_line() puts it,
    for the voice shell's gate to weigh before the call goes ahead; None for a call that keeps none."""
    command, folder, project = (str(args.get(k) or "").strip() for k in ("text", "folder", "project"))
    if tool == "schedule_change":
        job = get(args["id"]) if (command or folder) and isinstance(args.get("id"), int) else None
        if not job or job["kind"] != "command":
            return None
        command, folder, project = command or job["text"], folder or job["folder"], job["project"] or ""
    elif tool != "schedule_add" or args.get("kind") != "command":
        return None
    try:
        folder = place(folder, project)[0]
    except ValueError:  # the call fails on it anyway: weighed as given
        pass
    return command_line(command, folder)


def _log_file(log_file: str) -> str | None:
    from jarvis.safety.commands import names_secret

    log_file = (log_file or "").strip()
    if not log_file:
        return None
    if log_file.startswith("~") or Path(log_file).is_absolute() or ".." in Path(log_file).parts:
        raise ValueError("log_file is a path in the command's folder, like logs/*-produce.log.")
    if names_secret(log_file):
        raise ValueError("log_file names a secret, which stays out of reach.")
    return log_file


def _command(job: dict, folder: str | None, project: str, timeout_minutes: int | None, summarize: bool | None,
             auto_fix: bool | None, log_file: str | None) -> dict:
    """A command job's folder, project, timeout_minutes, summarize, auto_fix and log_file, checked: those given, over
    what `job` has (nothing, for a new one). Auto-fix is on unless said, once there's a project to fix it in."""
    out: dict = {}
    if folder is not None:
        out["folder"], out["project"] = place(folder, project)
    if timeout_minutes is not None:
        if timeout_minutes < 0:
            raise ValueError("timeout_minutes can't be negative.")
        out["timeout_minutes"] = timeout_minutes or None
    if summarize is not None:
        out["summarize"] = int(summarize)
    if log_file is not None:
        out["log_file"] = _log_file(log_file)
    project = out["project"] if "project" in out else job.get("project")
    if auto_fix is None and not job:  # a new job
        auto_fix = bool(project)
    if auto_fix is not None:
        if auto_fix and not project:
            raise ValueError("auto_fix needs the project the command belongs to (projects_list): a fix is a "
                             "code_change task there.")
        out["auto_fix"] = int(auto_fix)
    elif not project and job.get("auto_fix"):
        out["auto_fix"] = 0  # moved out of its project: nothing to fix in
    return out


def run_of(job: dict) -> dict | None:
    """A command job's latest run, or None before its first: {"task", "started", "attempt"}, its command task's id
    (jarvis.tasks.command_jobs), and once it has ended "ended", "outcome", "code", "summary" and any "fix_task"."""
    try:
        found = json.loads(job.get("run") or "null")
    except ValueError:
        return None
    return found if isinstance(found, dict) else None


def _only_changes(kind: str, only_changes: bool | None) -> None:
    if only_changes and kind != "task":
        raise ValueError("only_changes is for scheduled tasks: a watch speaks only once what it waits for happens, "
                         "and a reminder always speaks.")


def _command_only(kind: str, *given) -> None:
    if kind != "command" and any(v not in (None, "", 0) for v in given):
        raise ValueError("folder, timeout_minutes, summarize, auto_fix and log_file are for command jobs.")


def add(kind: str, title: str, text: str, when: str = "", in_minutes: int = 0, repeat: str = "",
        every_minutes: int = 0, until: str = "", task_kind: str = "", project: str = "", probe: str = "",
        target: str = "", expect: str = "", only_changes: bool = False, folder: str = "", timeout_minutes: int = 0,
        summarize: bool | None = None, auto_fix: bool | None = None, log_file: str = "",
        now: datetime | None = None) -> dict:
    """A new job, checked and saved; returns it."""
    now = _now(now)
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {', '.join(KINDS)}.")
    _only_changes(kind, only_changes)
    _command_only(kind, folder, timeout_minutes, summarize, auto_fix, log_file)
    text = (text or "").strip()
    if not text:
        raise ValueError("text is empty: say what to remind, the task's instruction, what to watch for, or the command.")
    job = {"kind": kind} | _what(kind, task_kind, project, probe, target, expect)
    if kind == "command":
        job |= _command({}, folder, project, timeout_minutes, summarize, auto_fix, log_file)
    job |= _timing(job, when, in_minutes, repeat, every_minutes, until, now)
    live = items()
    if len(live) >= MAX_LIVE:
        raise ValueError(f"There are already {len(live)} scheduled jobs; cancel some first.")
    if job["probe"] == "task" and sum(1 for j in live if j["probe"] == "task") >= MAX_TASK_WATCHES:
        raise ValueError(f"At most {MAX_TASK_WATCHES} watches can be checked by background tasks at once; cancel one, "
                         "or use a url, gmail or chat probe.")
    job |= {"title": _clean(title)[:80] or _clean(text)[:60], "text": text, "status": ACTIVE,
            "only_changes": int(bool(only_changes)), "created": now.timestamp(), "updated": now.timestamp()}
    with _db() as db:
        cur = db.execute(f"insert into schedules ({', '.join(job)}) values ({', '.join('?' * len(job))})",
                         tuple(job.values()))
    return find(cur.lastrowid)


def change(job_id: int, title: str = "", text: str = "", when: str = "", in_minutes: int = 0, repeat: str = "",
           every_minutes: int = 0, until: str = "", target: str = "", expect: str = "", paused: bool | None = None,
           only_changes: bool | None = None, folder: str = "", timeout_minutes: int | None = None,
           summarize: bool | None = None, auto_fix: bool | None = None, log_file: str | None = None,
           now: datetime | None = None) -> dict:
    """Change an active or paused job: what's given replaces what it had. A new time (when, in_minutes, repeat or
    every_minutes) replaces its whole timing; paused pauses or resumes it; only_changes turns staying silent when
    nothing has changed on or off. A command job's folder (a relative one is in its project's), timeout_minutes (0:
    none), summarize, auto_fix and log_file ("": none) are changed the same way."""
    now, job = _now(now), find(job_id)
    if job["status"] not in LIVE:
        raise ValueError(f"Job {job_id} has {job['status'] if job['status'] != DONE else 'finished'}; schedule a new one.")
    _command_only(job["kind"], folder, timeout_minutes, summarize, auto_fix, log_file)
    fields: dict = {}
    if job["kind"] == "command":
        fields |= _command(job, folder or None, job["project"] or "", timeout_minutes, summarize, auto_fix, log_file)
    if only_changes is not None:
        _only_changes(job["kind"], only_changes)
        fields["only_changes"] = int(only_changes)
    if _clean(title):
        fields["title"] = _clean(title)[:80]
    if (text or "").strip():
        fields["text"] = text.strip()
    if target or expect:
        if job["kind"] != "watch" or job["probe"] == "task":
            raise ValueError("target and expect are only for url, gmail and chat watches.")
        fields |= {k: v for k, v in _what("watch", "", "", job["probe"], target or job["target"],
                                           expect or job["expect"] or "").items() if k in ("target", "expect")}
        fields["baseline"] = None
    if when or in_minutes or repeat or every_minutes:
        fields |= _timing(job, when, in_minutes, repeat, every_minutes, until, now)
    elif until:
        fields["until"] = _end(job, until, now)
    if paused is not None:
        fields["status"] = PAUSED if paused else ACTIVE
        if not paused and "next_run" not in fields:  # resumed: what came due while paused runs no more than once
            nxt = job["next_run"] or now.timestamp()
            if job["days"] and nxt < now.timestamp():
                nxt = next_on_days(job["at"], job["days"], now).timestamp()
            elif job["every"]:
                nxt = max(nxt, now.timestamp())
            fields["next_run"] = nxt
    if not fields:
        raise ValueError("Nothing to change: give what's new.")
    return update(job_id, **fields, updated=now.timestamp())


def cancel(job_id: int) -> dict:
    job = find(job_id)
    if job["status"] not in LIVE:
        raise ValueError(f"Job {job_id} has already {'finished' if job['status'] == DONE else job['status']}.")
    return update(job_id, status=CANCELLED, next_run=None, updated=time.time())
