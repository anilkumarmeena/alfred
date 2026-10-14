"""Scheduled jobs (schedules table in data/tasks.db). Voice tools add/change/cancel; the runner's scheduler runs them.

  reminder  say `text` (desktop notification, phone push, aloud in a live session)
  task      start a background task with `text` as instruction (`task_kind`, default research)
  watch     every `every` minutes until `text` happens or `until`: probe task (model), or cheap url/gmail/chat probes
  command   run shell `text` in `folder` as a model-free command task; a model only summarises or (auto_fix) fixes it.
            With only_changes, a successful run that prints nothing stays silent (how a monitor script reports)
  resume    carry on stopped (or failed) tasks, named ones or all, with `text` as an optional message to each
  todo      start tasks for to-do items, named ones or the next `count` open, each linked to its item; `text` is
            added to every instruction. Both run in jarvis.tasks.unattended; `picks` holds what they take on
Timing: one-off at `next_run`, or `at` on `days`, or every `every` minutes; local profile time zone.
Task jobs keep a `notepad` for the next run. Command jobs are only made or changed through voice tools whose calls
pass the shell gate (risky needs a yes, secrets refused).
"""

import json
import re
import shlex
import sqlite3
import time
from contextlib import AbstractContextManager, closing
from datetime import datetime, time as clock_time, timedelta
from pathlib import Path

from jarvis.core import database, profile

KINDS = ("reminder", "task", "watch", "command", "resume", "todo")
WORK = ("resume", "todo")  # kinds that resume or start the user's own tasks
PROBES = ("task", "url", "gmail", "chat")
ACTIVE, PAUSED, DONE, CANCELLED, EXPIRED = "active", "paused", "done", "cancelled", "expired"
LIVE = (ACTIVE, PAUSED)
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
REPEATS = {"daily": DAYS, "weekdays": DAYS[:5], "weekends": DAYS[5:]}
# Watch check interval in minutes: minimum and default per probe. Task probes start a Claude session, so are sparser.
MIN_EVERY = {"task": 10, "url": 1, "gmail": 5, "chat": 5}
EVERY = {"task": 15, "url": 5, "gmail": 10, "chat": 10}
MIN_TASK_EVERY = 30  # for task, resume and todo jobs repeating by interval
MIN_REMINDER_EVERY = 5
MIN_COMMAND_EVERY = 10
WATCH_HOURS, WATCH_MAX_DAYS = 24, 7  # default and max watch lifetime
MAX_LIVE, MAX_TASK_WATCHES = 30, 5  # live jobs; of those, task-probe watches
TASK_KINDS = ("research", "code_question", "code_change")
WATCH_TASK_KINDS = ("research", "code_question")  # read-only
MAX_PICKS = 10  # tasks one resume or todo run takes on

_SCHEMA = """
create table schedules (
    id integer primary key autoincrement,  -- never reused
    kind text not null,         -- reminder, task, watch, command
    title text not null,
    text text not null,         -- reminder text, task instruction, watch condition or command
    task_kind text,             -- background task kind (task, watch probe task)
    project text,               -- code task kinds and commands
    at text,                    -- HH:MM, with days
    days text,                  -- e.g. mon,tue,wed,thu,fri
    every integer,              -- minutes between runs (all watches)
    until real,                 -- null: never
    probe text,                 -- watch: task, url, gmail or chat
    target text,                -- watch: URL, Gmail search or person
    expect text,                -- watch url: required page text
    status text not null,       -- active, paused, done, cancelled, expired
    next_run real,              -- null: no further runs
    last_run real,
    runs integer not null default 0,
    fails integer not null default 0,   -- consecutive failed checks
    last_task integer,          -- task id of the latest run
    last_result text,           -- one-sentence latest result
    baseline text,              -- watch chat: JSON keys of messages present at first check
    created real not null,
    updated real not null,
    notepad text,               -- task: JSON {at, text} for the next run
    only_changes integer not null default 0,  -- task: 1 = silent unless changed
    retries integer not null default 0,       -- task: retries after transient failure
    failing text,               -- task: last failure kind announced, cleared on success
    folder text,                -- command: working dir
    timeout_minutes integer,    -- command: null: no limit
    summarize integer not null default 1,  -- command: 1 = model summary on success
    auto_fix integer not null default 0,   -- command: 1 = fix failures via a task, rerun once
    log_file text,              -- command: own log path/pattern in folder
    run text                    -- command: JSON of the latest run (see run_of)
)"""
_MIGRATIONS = (
    "alter table schedules add column picks text",  # 2: resume and todo: JSON of what a run takes on (see picks)
)


def _db() -> AbstractContextManager[sqlite3.Connection]:
    return database.table("schedules", _SCHEMA, migrations=_MIGRATIONS)


def _now(now: datetime | None) -> datetime:
    return now or datetime.now(profile.tz())


# ── Times ───────────────────────────────────────────────────────


def parse_time(text: str, now: datetime | None = None) -> datetime:
    """Parse local '17:00' (next occurrence) or '2026-10-06T17:00'."""
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
    """'weekdays' -> 'mon,tue,wed,thu,fri'; also daily, weekends, or 'mon, wed, friday'."""
    repeat = repeat.strip().lower()
    if repeat in REPEATS:
        return ",".join(REPEATS[repeat])
    named = [w[:3] for w in re.split(r"[\s,]+", repeat) if w]
    if not named or any(d not in DAYS for d in named):
        raise ValueError(f"repeat '{repeat}' should be daily, weekdays, weekends, or days like mon,wed,fri.")
    return ",".join(d for d in DAYS if d in named)


def _on_days(at: str, days: str, moment: datetime, step: int) -> datetime:
    """Nearest `at` on one of `days`: strictly after `moment` (step 1) or at/before it (step -1)."""
    t, wanted = clock_time.fromisoformat(at), {DAYS.index(d) for d in days.split(",")}
    moment = moment.astimezone(profile.tz())
    for i in range(8):
        day = moment.date() + timedelta(days=i * step)
        when = datetime.combine(day, t, profile.tz())
        if day.weekday() in wanted and (when > moment if step > 0 else when <= moment):
            return when
    raise ValueError(f"no day in {days}")  # unreachable


def next_on_days(at: str, days: str, after: datetime) -> datetime:
    return _on_days(at, days, after, 1)


def last_on_days(at: str, days: str, before: datetime) -> datetime:
    return _on_days(at, days, before, -1)


def following(job: dict, after: float) -> float | None:
    """Next run time after a run at `after`; None for one-offs or past `until`."""
    if job["every"]:
        at = after + job["every"] * 60
    elif job["days"]:
        at = next_on_days(job["at"], job["days"], datetime.fromtimestamp(after, profile.tz())).timestamp()
    else:
        return None
    return None if job["until"] and at > job["until"] else at


def clock(ts: float, now: datetime | None = None) -> str:
    """'17:00', 'tomorrow 09:00' or 'Mon 12 Oct 09:00', depending on distance."""
    at, today = datetime.fromtimestamp(ts, profile.tz()), _now(now).date()
    if at.date() == today:
        return f"{at:%H:%M}"
    if at.date() == today + timedelta(days=1):
        return f"tomorrow {at:%H:%M}"
    return f"{at:%a} {at.day} {at:%b %H:%M}"


_DAYS_SAID = {",".join(REPEATS["daily"]): "every day", ",".join(REPEATS["weekdays"]): "every weekday",
              ",".join(REPEATS["weekends"]): "at weekends"}


def describe(job: dict, now: datetime | None = None) -> str:
    """Schedule in words, e.g. 'every weekday at 09:00', 'every 15 minutes until 18:00'."""
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
    """How a watch checks, in words; "" for other kinds."""
    if job["kind"] != "watch":
        return ""
    said = CHECKS[job["probe"]] + (f": {job['target']}" if job["target"] else "")
    return said + (f", for \"{job['expect']}\"" if job["expect"] else "")


# ── Jobs ────────────────────────────────────────────────────────


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
    """Titles by id, finished jobs included."""
    found = _rows(f"select id, title from schedules where id in ({', '.join('?' * len(ids))})", tuple(ids))
    return {r["id"]: r["title"] for r in found}


def items(finished_days: float = 0) -> list[dict]:
    """Live jobs soonest first, then those finished in the last `finished_days`, latest first."""
    live = _rows(f"select * from schedules where {_IN_LIVE} order by next_run is null, next_run", LIVE)
    if not finished_days:
        return live
    return live + _rows(f"select * from schedules where not {_IN_LIVE} and updated > ? order by updated desc",
                        (*LIVE, time.time() - finished_days * 86400))


def due(now: float) -> list[dict]:
    """Active jobs due by `now`, earliest first."""
    return _rows("select * from schedules where status = ? and next_run <= ? order by next_run", (ACTIVE, now))


def lapsed(now: float) -> list[dict]:
    """Live jobs past their `until`."""
    return _rows(f"select * from schedules where {_IN_LIVE} and until <= ?", (*LIVE, now))


def update(job_id: int, **fields) -> dict:
    """Set fields on a job and return it. Field names are interpolated: callers must never pass user input."""
    columns = ", ".join(f"{k} = ?" for k in fields)
    with _db() as db:
        db.execute(f"update schedules set {columns} where id = ?", (*fields.values(), job_id))
    return find(job_id)


def baseline(job: dict) -> set[str]:
    return set(json.loads(job["baseline"] or "[]"))


def notepad(job: dict) -> dict | None:
    """A task job's notepad {"at", "text"}, or None."""
    try:
        found = json.loads(job.get("notepad") or "null")
    except ValueError:
        return None
    return found if isinstance(found, dict) and found.get("text") else None


def prune(days: float) -> int:
    """Delete jobs finished more than `days` ago; returns the count."""
    with _db() as db:
        return db.execute(f"delete from schedules where not {_IN_LIVE} and updated < ?",
                          (*LIVE, time.time() - days * 86400)).rowcount


def _clean(text: str) -> str:
    return " ".join((text or "").split())


def _timing(job: dict, when: str, in_minutes: int, repeat: str, every_minutes: int, until: str,
            now: datetime) -> dict:
    """Validated at/days/every/until/next_run. A changed job keeps its `until` unless a new one is given."""
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
        # Bare HH:MM counts from now; a full datetime from that moment
        from_ = now if re.fullmatch(r"\s*\d{1,2}:\d{2}\s*", when) else max(now, start - timedelta(seconds=1))
        first = next_on_days(out["at"], out["days"], from_)
    elif every_minutes or kind == "watch":
        every = every_minutes or EVERY[probe]
        least = MIN_TASK_EVERY if kind in ("task", *WORK) else {"watch": MIN_EVERY[probe],
                                                                 "command": MIN_COMMAND_EVERY}.get(kind, MIN_REMINDER_EVERY)
        if every < least:
            raise ValueError(f"Every {least} minutes at most often for this kind of job.")
        out["every"] = every
        # Watches check at once; other repeating jobs first run one interval from now
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
    """End time: `until`, else `kept`, else a watch's default. Must follow the next run; only for repeating jobs."""
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
    """Validated task_kind, project, probe, target and expect."""
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
    """(absolute folder, owning project or None) for a command job.

    `folder` may use ~ or be relative to the named project; defaults to the project's folder. Secret folders refused.
    """
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
    if secret_path(path):
        raise ValueError("That folder holds secrets, which stay out of reach.")
    if there := (named if named and named.node else projects.living_at(path)):
        return str(path), there.name  # its folder is on that machine, which runs the job
    if not path.is_dir():
        raise ValueError(f"{path} isn't a folder.")
    return str(path), project_of(path, named)


def command_line(command: str, folder: str) -> str:
    """Command as the shell gate evaluates it: prefixed with a cd into its folder."""
    return f"cd {shlex.quote(folder)} && {command.strip()}"


COMMAND_TOOLS = ("schedule_add", "schedule_change")  # voice tools that can store a command


def command_called(tool: str, args: dict) -> str | None:
    """Command line a schedule_add/schedule_change call would store, for the shell gate to check first; else None."""
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
    except ValueError:  # the call will fail anyway; gate the raw folder
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
    """Validated command fields given, layered over `job` ({} for new). auto_fix defaults on when there's a project."""
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
    if auto_fix is None and not job:  # new job
        auto_fix = bool(project)
    if auto_fix is not None:
        if auto_fix and not project:
            raise ValueError("auto_fix needs the project the command belongs to (projects_list): a fix is a "
                             "code_change task there.")
        out["auto_fix"] = int(auto_fix)
    elif not project and job.get("auto_fix"):
        out["auto_fix"] = 0  # no project left to fix in
    return out


def run_of(job: dict) -> dict | None:
    """Latest run of a command job, or None.

    {"task", "started", "attempt"}, plus "ended", "outcome", "code", "summary" and optional "fix_task" once done.
    """
    try:
        found = json.loads(job.get("run") or "null")
    except ValueError:
        return None
    return found if isinstance(found, dict) else None


def picks(job: dict) -> dict:
    """What a resume or todo job takes on each run: {"tasks": [ids]} or {"failed": bool} (every stopped task, failed
    ones too if true); {"todos": [item ids]} or {"count": n} (the next n open items). {} for other kinds."""
    try:
        found = json.loads(job.get("picks") or "null")
    except ValueError:
        return {}
    return found if isinstance(found, dict) else {}


def _tasks_named(ids: list[int]) -> dict[int, dict]:
    """Tasks by id, from the task store; {} before any task exists."""
    with closing(database.connect()) as db:
        try:
            rows = db.execute(f"select id, kind, title from tasks where id in ({', '.join('?' * len(ids))})", ids)
            return {r["id"]: dict(r) for r in rows}
        except sqlite3.OperationalError:  # no tasks table yet
            return {}


def _picks(kind: str, tasks: list[int] | None, failed: bool, todos: list[str] | None, count: int) -> str | None:
    """Validated picks (JSON) for a resume or todo job; None for other kinds, which mustn't be given any."""
    if kind not in WORK:
        if tasks or failed or todos or count:
            raise ValueError("tasks and failed are for resume jobs, todos and count for todo jobs.")
        return None
    if kind == "resume":
        if todos or count:
            raise ValueError("todos and count are for todo jobs; a resume job takes tasks, or none for every stopped "
                             "task.")
        if not tasks:
            return json.dumps({"failed": bool(failed)})
        ids = list(dict.fromkeys(tasks))
        if len(ids) > MAX_PICKS:
            raise ValueError(f"A resume job takes at most {MAX_PICKS} tasks.")
        found = _tasks_named(ids)
        if missing := [i for i in ids if i not in found]:
            raise ValueError(f"There's no task {', '.join(map(str, missing))}.")
        if odd := [found[i] for i in ids if found[i]["kind"] not in database.KINDS]:
            raise ValueError(f"Task {odd[0]['id']}, {odd[0]['title']}, is a {odd[0]['kind']} task, which can't be "
                             "resumed: only code and research tasks carry on.")
        return json.dumps({"tasks": ids})
    if tasks or failed:
        raise ValueError("tasks and failed are for resume jobs; a todo job takes todos or count.")
    if todos and count:
        raise ValueError("Give todos (the items) or count (the next so many open items), not both.")
    if todos:
        from jarvis.state import todo

        ids = list(dict.fromkeys(todo.find(t)["id"] for t in todos))
        if len(ids) > MAX_PICKS:
            raise ValueError(f"A todo job starts at most {MAX_PICKS} tasks.")
        return json.dumps({"todos": ids})
    if not 0 <= count <= MAX_PICKS:
        raise ValueError(f"count is how many open items to start tasks for, 1 to {MAX_PICKS}.")
    return json.dumps({"count": count or 1})


def work_text(job: dict) -> str:
    """What a resume or todo job does, in words; "" for other kinds."""
    found = picks(job)
    if job["kind"] == "resume":
        if ids := found.get("tasks"):
            return f"resumes task{'s' if len(ids) > 1 else ''} {', '.join(map(str, ids))}"
        return "resumes every stopped " + ("or failed task" if found.get("failed") else "task")
    if job["kind"] != "todo":
        return ""
    if ids := found.get("todos"):
        from jarvis.state import todo

        items = [item["text"] for i in ids if (item := todo.get(i))]
        return f"starts a task for each of these to-do items: {'; '.join(items) or '(all removed)'}"
    n = found.get("count") or 1
    return f"starts tasks for the next {n} open to-do items" if n > 1 else "starts a task for the next open to-do item"


def _only_changes(kind: str, only_changes: bool | None) -> None:
    if only_changes and kind not in ("task", "command"):
        raise ValueError("only_changes is for scheduled tasks and commands: a watch speaks only once what it waits for "
                         "happens, and a reminder always speaks.")


def _command_only(kind: str, *given) -> None:
    if kind != "command" and any(v not in (None, "", 0) for v in given):
        raise ValueError("folder, timeout_minutes, summarize, auto_fix and log_file are for command jobs.")


def add(kind: str, title: str, text: str, when: str = "", in_minutes: int = 0, repeat: str = "",
        every_minutes: int = 0, until: str = "", task_kind: str = "", project: str = "", probe: str = "",
        target: str = "", expect: str = "", only_changes: bool = False, folder: str = "", timeout_minutes: int = 0,
        summarize: bool | None = None, auto_fix: bool | None = None, log_file: str = "",
        tasks: list[int] | None = None, failed: bool = False, todos: list[str] | None = None, count: int = 0,
        now: datetime | None = None) -> dict:
    """Validate and save a new job; returns it. `tasks` and `failed` are for resume jobs, `todos` and `count` for
    todo jobs (see picks)."""
    now = _now(now)
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {', '.join(KINDS)}.")
    _only_changes(kind, only_changes)
    _command_only(kind, folder, timeout_minutes, summarize, auto_fix, log_file)
    text = (text or "").strip()
    if not text and kind not in WORK:
        raise ValueError("text is empty: say what to remind, the task's instruction, what to watch for, or the command.")
    if kind in WORK and (task_kind or project):
        raise ValueError(f"A {kind} job takes no task_kind or project: "
                         + ("each task carries on as it was." if kind == "resume" else "each item's task gets its own."))
    job = {"kind": kind, "picks": _picks(kind, tasks, failed, todos, count)}
    job |= _what(kind, task_kind, project, probe, target, expect)
    if kind == "command":
        job |= _command({}, folder, project, timeout_minutes, summarize, auto_fix, log_file)
    job |= _timing(job, when, in_minutes, repeat, every_minutes, until, now)
    live = items()
    if len(live) >= MAX_LIVE:
        raise ValueError(f"There are already {len(live)} scheduled jobs; cancel some first.")
    if job["probe"] == "task" and sum(1 for j in live if j["probe"] == "task") >= MAX_TASK_WATCHES:
        raise ValueError(f"At most {MAX_TASK_WATCHES} watches can be checked by background tasks at once; cancel one, "
                         "or use a url, gmail or chat probe.")
    said = text or work_text(job)
    job |= {"title": _clean(title)[:80] or _clean(said)[:60], "text": text, "status": ACTIVE,
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
    """Change a live job; given fields replace existing ones.

    Any new time (when, in_minutes, repeat, every_minutes) replaces the whole timing. `paused` pauses or resumes.
    For commands: timeout_minutes 0 and log_file "" mean none.
    """
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
        if not paused and "next_run" not in fields:  # resumed: runs missed while paused run at most once
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
