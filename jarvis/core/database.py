"""Background tasks, kept in the profile's database (jarvis.core.profile: the owner's is data/tasks.db) so they
survive restarts of the runner.

The same database holds other modules' tables (the to-do list, the project list, Google accounts, settings, secrets,
MCP servers), each opened through table(), which first brings it to its latest version (migrate()). Every connection
comes from connect(), to the database of the profile the code runs as.
"""

import json
import logging
import sqlite3
import threading
import time
from collections import deque
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path

from jarvis.core import profile

log = logging.getLogger("jarvis.core.database")
KINDS = ("code_change", "code_question", "research")  # what a model works on
COMMAND = "command"  # a shell command run with no model while it works (jarvis.tasks.command_jobs)
CONNECTION = "connection"  # a call on a connection carried on in the background (jarvis.tasks.connection_jobs)
ACTIVE = ("queued", "running", "waiting")
# What a waiting task waits for (its wait_kind): the user's yes to its requests, access it asked for, or an answer
WAIT_APPROVAL, WAIT_ACCESS, WAIT_QUESTION = "approval", "access", "question"
_IN_ACTIVE = f"status in ({', '.join('?' * len(ACTIVE))})"
# How long a statement waits for another connection's write to end (the runner, voice agent and web server share the
# file) before it fails with "database is locked"
BUSY_SECONDS = 10

_TASKS = """
create table tasks (
    id integer primary key,
    kind text not null,
    title text not null,
    instruction text not null,
    project text,
    status text not null,       -- queued, running, waiting, done, failed, stopped, discarded
    created real not null,
    updated real not null,
    session_id text,            -- Claude session, resumed for answers and follow-ups
    worktree text,              -- code_change only, as are in_place, branch and base
    in_place integer not null default 0,  -- 1: works uncommitted in the project's own folder, kept in worktree
    branch text,                -- the task's branch; for in-place tasks, the branch checked out when it began
    base text,                  -- commit the changes are measured against
    activity text,              -- what it is doing right now
    question text,              -- set while it waits for the user
    wait_kind text,             -- what that question waits for: approval, access or question (WAIT_*)
    nudges text,                -- JSON list of messages sent while queued or running, not yet delivered
    edits text,                 -- JSON list of edits to JARVIS's data files the task suggested (jarvis.tasks.data_edits)
    summary text,               -- the session's final message
    spoken text,                -- the latest update, as JARVIS says it
    changes text,               -- git shortstat of the branch against base
    error text,
    cost real not null default 0,
    seen integer not null default 1, -- 0 until the user has heard the latest update
    job integer,                -- the scheduled job (jarvis.tasks.schedules) it's a run of
    hidden integer not null default 0, -- 1: a scheduled run with nothing to say, left out of the lists
    grants text,                -- JSON list of access granted beyond its kind's (jarvis.tasks.access)
    wants text,                 -- JSON list of access it asked for with request_access, not yet answered
    orders text,                -- JSON list of {said, at}: the user's own words ordering what it may do unasked
    command text,               -- command tasks: JSON of how it runs and how it went (jarvis.tasks.command_jobs)
    fixes integer,              -- a task fixing a command task that failed: that task's id
    todo integer,               -- the to-do item (jarvis.state.todo) it was started for, settled as it ends (jarvis.tasks.goal)
    interrupted text            -- JSON {at, on_mac, prompt}: cut off by a shutdown while queued or running, to resume
)"""
# Columns added to tasks before versions were kept, for databases made before them
_ADDED = {"job": "integer", "hidden": "integer not null default 0", "grants": "text", "wants": "text", "orders": "text",
          "command": "text", "fixes": "integer", "todo": "integer", "wait_kind": "text", "interrupted": "text"}
_LOG = """
create table log (
    task integer not null,
    at real not null,
    kind text not null,
    text text not null
)"""
_LOG_MIGRATIONS = (
    "create index if not exists log_task on log(task)",  # 2: a task's log is read on its own, and steps make it long
)
_APPROVALS = """
create table approvals (
    id integer primary key,
    task integer not null,
    created real not null,
    expires real not null,
    commands text not null,     -- JSON list of {tool, command, folder, why}: risky host commands, all answered together
    state text not null,        -- pending, approved, denied, expired, cancelled; spent once the run it was for ends
    announced integer not null default 0,  -- 1 once the push and the voice agent have been told
    nonce_hash text,            -- sha256 of the one-time nonce in its push, which answers it from the phone
    answered_at real,
    via text,                   -- how it was answered: push, web, panel, voice, cli, assistant, rule, order
    used text                   -- JSON list of the indexes of its commands that have run
)"""
PENDING, APPROVED, DENIED, EXPIRED, CANCELLED, SPENT = "pending", "approved", "denied", "expired", "cancelled", "spent"
_SHOWN = "not hidden"

# ── Connections and schema versions ─────────────────────────────

_VERSIONS = """
create table if not exists schema_versions (
    name text primary key,      -- a table, whose migrations are its own
    version integer not null,   -- how many of them it has had
    at real not null
)"""
Migration = str | Callable[[sqlite3.Connection], object]  # a statement, or a function of the connection


def connect(at: Path | None = None, **kwargs) -> sqlite3.Connection:
    """A connection to the database (the current profile's, or the one `at`), as every part of JARVIS opens one: in
    autocommit mode (transactions begun explicitly), rows by column name, waiting up to BUSY_SECONDS for another
    connection's write (sqlite's busy_timeout), and in WAL mode, where reading and writing don't wait for each other,
    with NORMAL sync, which skips an fsync per commit. `kwargs` go on to sqlite3.connect."""
    db = sqlite3.connect(at or profile.current().db, timeout=BUSY_SECONDS, isolation_level=None, **kwargs)
    try:
        db.row_factory = sqlite3.Row
        db.execute("pragma journal_mode = wal")  # kept in the file, so a no-op once any connection has set it
        db.execute("pragma synchronous = normal")
    except sqlite3.Error:
        db.close()
        raise
    return db


def _has(db: sqlite3.Connection, name: str) -> bool:
    return db.execute("select 1 from sqlite_master where name = ?", (name,)).fetchone() is not None


def _version(db: sqlite3.Connection, name: str) -> int:
    """How many migrations the table `name` has had: 0 if it was made before versions were kept, or isn't there (made
    afresh, then, should it have been dropped)."""
    try:
        row = db.execute("select version from schema_versions where name = ? and exists"
                         " (select 1 from sqlite_master where name = ?)", (name, name)).fetchone()
    except sqlite3.OperationalError:  # no table of versions yet
        return 0
    return row[0] if row else 0


def migrate(db: sqlite3.Connection, name: str, schema: Sequence[str], added: dict[str, str] | None = None,
            migrations: Sequence[Migration] = ()) -> bool:
    """Bring the table `name` to its latest version, in one transaction. Version 1 makes it with `schema`'s statements
    and gives it the columns in `added` (those added before versions were kept), so a table made before then, rows and
    all, starts at version 1 too. `migrations` are versions 2 on, in order; each runs once, counted in
    schema_versions. Neither schema, added nor a released migration ever changes: a change goes on the end of
    `migrations`. A table a newer JARVIS has taken further is left as it is. Returns whether it made the table."""
    latest, made = 1 + len(migrations), False
    if _version(db, name) >= latest:
        return made
    db.execute("begin immediate")  # so of two processes opening it at once, one migrates it, once
    try:
        db.execute(_VERSIONS)
        have = _version(db, name)
        if not have:
            if made := not _has(db, name):
                for statement in schema:  # not executescript, which would commit the lock away
                    db.execute(statement)
            add_columns(db, name, added or {})
            have = 1
        for step in migrations[have - 1:]:
            step(db) if callable(step) else db.execute(step)
        db.execute("insert into schema_versions values (?, ?, ?) on conflict (name) do update set"
                   " version = excluded.version, at = excluded.at", (name, max(have, latest), time.time()))
        db.execute("commit")
        return made
    finally:
        if db.in_transaction:
            db.execute("rollback")


@contextmanager
def table(name: str, *schema: str, added: dict[str, str] | None = None, migrations: Sequence[Migration] = (),
          made: Callable[[], object] | None = None, at: Path | None = None) -> Iterator[sqlite3.Connection]:
    """The database (the current profile's, or the one `at`), with the table `name` at its latest version (migrate()),
    and `made()` run if that made it; closed after."""
    db = connect(at)
    try:
        if migrate(db, name, schema, added, migrations) and made:
            made()  # after the commit, so it can use connections of its own
        yield db
    finally:
        if db.in_transaction:
            db.execute("rollback")
        db.close()


def add_columns(db: sqlite3.Connection, name: str, columns: dict[str, str]) -> None:
    """Add the columns (name: type) that the table `name`, made before them, lacks."""
    have = {r[1] for r in db.execute(f"pragma table_info({name})")}
    for column, kind in columns.items():
        if column not in have:
            db.execute(f"alter table {name} add column {column} {kind}")


def minutes_since_update(task: dict) -> int:
    return round((time.time() - task["updated"]) / 60)


def nudges(task: dict) -> list[str]:
    """Messages waiting to reach the task, oldest first."""
    return json.loads(task.get("nudges") or "[]")


def edits(task: dict) -> list[dict]:
    """The data-file edits the task suggested, numbered from 1, each with its status."""
    return json.loads(task.get("edits") or "[]")


def same_command(a: dict, b: dict) -> bool:
    """Whether two host commands are the same request: tool, text and folder. Why it's risky can change as the
    processes it names come and go, so it doesn't count."""
    return all(a.get(k) == b.get(k) for k in ("tool", "command", "folder"))


def _approval(row: sqlite3.Row) -> dict:
    a = dict(row)
    a["commands"], a["used"] = json.loads(a["commands"]), json.loads(a["used"] or "[]")
    return a


class Store:
    def __init__(self) -> None:
        self._file = profile.current().db  # the profile's it was made for, also when write_steps runs on a thread
        self._db = connect(self._file)
        migrate(self._db, "tasks", [_TASKS], _ADDED)
        migrate(self._db, "log", [_LOG], migrations=_LOG_MIGRATIONS)
        migrate(self._db, "approvals", [_APPROVALS])
        # Tool calls, too many to write one by one on the runner's event loop: (task, at, activity, logged), kept
        # here till write_steps(). A deque, as it's filled on the loop and emptied on a thread of write_steps' own.
        self._steps: deque[tuple[int, float, str, bool]] = deque()
        self._shown: dict[int, str] = {}  # each task's activity as last set here, so a step repeated isn't logged
        self._writing = threading.Lock()
        self._writer: sqlite3.Connection | None = None  # write_steps' own, as it may run on any thread

    @property
    def db(self) -> sqlite3.Connection:
        """The connection, once the steps kept in memory are written, so what's read or written next comes after
        them."""
        self.write_steps()
        return self._db

    def step(self, task_id: int, text: str) -> None:
        """A tool call the task made, its activity now: logged as a step unless it repeats the one before. Kept in
        memory, with no database call, till write_steps() (which the runner calls every so often, off its event loop)
        or the store's next use."""
        self._steps.append((task_id, time.time(), text, self._shown.get(task_id) != text))
        self._shown[task_id] = text

    def write_steps(self) -> None:
        """Write the steps kept in memory in one transaction: each logged one in the log, and each task's latest as its
        activity. Steps that can't be written (the database stayed locked, say) are logged here and dropped: they
        only say what a task was doing."""
        with self._writing:
            steps = [self._steps.popleft() for _ in range(len(self._steps))]  # any added meanwhile wait for the next
            if not steps:
                return
            latest = {task: (text, at) for task, at, text, _ in steps}
            try:
                if self._writer is None:
                    self._writer = connect(self._file, check_same_thread=False)
                self._writer.execute("begin immediate")
                self._writer.executemany("insert into log values (?, ?, 'step', ?)",
                                         [(task, at, text) for task, at, text, logged in steps if logged])
                self._writer.executemany("update tasks set activity = ?, updated = ? where id = ?",
                                         [(text, at, task) for task, (text, at) in latest.items()])
                self._writer.execute("commit")
            except sqlite3.Error as e:
                log.warning("couldn't write %d steps of tasks %s: %s", len(steps), sorted(latest), e)
                if self._writer is not None and self._writer.in_transaction:
                    self._writer.execute("rollback")

    def create(self, kind: str, title: str, instruction: str, project: str | None, in_place: bool = False,
               job: int | None = None, hidden: bool = False, grants: list[str] = (), command: dict | None = None,
               fixes: int | None = None, todo: int | None = None) -> dict:
        now = time.time()
        cur = self.db.execute(
            "insert into tasks (kind, title, instruction, project, in_place, status, created, updated, job, hidden, grants,"
            " command, fixes, todo) values (?, ?, ?, ?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?, ?)",
            (kind, title, instruction, project, int(in_place), now, now, job, int(hidden),
             json.dumps(grants) if grants else None, json.dumps(command) if command is not None else None, fixes, todo),
        )
        return self.get(cur.lastrowid)

    def get(self, task_id: int) -> dict | None:
        row = self.db.execute("select * from tasks where id = ?", (task_id,)).fetchone()
        return dict(row) if row else None

    def set(self, task_id: int, **fields) -> None:
        if "activity" in fields:
            self._shown[task_id] = fields["activity"]
        fields["updated"] = time.time()
        columns = ", ".join(f"{k} = ?" for k in fields)  # keys come from code, never from input
        self.db.execute(f"update tasks set {columns} where id = ?", (*fields.values(), task_id))

    def update(self, task_id: int, **fields) -> dict:
        """set(), returning the updated task."""
        self.set(task_id, **fields)
        return self.get(task_id)

    def add_nudge(self, task_id: int, text: str) -> dict:
        """Queue a message for the task, after any already waiting; returns the task."""
        return self.update(task_id, nudges=json.dumps([*nudges(self.get(task_id)), text]))

    def take_nudges(self, task_id: int) -> list[str]:
        """The messages waiting for the task, oldest first, which then stop waiting."""
        if waiting := nudges(self.get(task_id)):
            self.set(task_id, nudges=None)
        return waiting

    def add_edit(self, task_id: int, edit: dict) -> dict:
        """Keep an edit the task suggested, pending, under the next number; returns it as kept."""
        kept = edits(self.get(task_id))
        kept.append(edit := {"n": len(kept) + 1, **edit, "status": "pending"})
        self.set(task_id, edits=json.dumps(kept, ensure_ascii=False))
        return edit

    def set_edits(self, task_id: int, changed: list[dict]) -> dict:
        """Replace the task's edits with `changed` (all of them, updated); returns the task."""
        return self.update(task_id, edits=json.dumps(changed, ensure_ascii=False))

    def _rows(self, sql: str, params: tuple = ()) -> list[dict]:
        return [dict(r) for r in self.db.execute(sql, params)]

    def active(self) -> list[dict]:
        return self._rows(f"select * from tasks where {_IN_ACTIVE} order by id", ACTIVE)

    def recent(self, hours: float = 24, limit: int = 20) -> list[dict]:
        """Active tasks, plus any that changed in the last `hours`; scheduled runs with nothing to say left out."""
        return self._rows(
            f"select * from tasks where ({_IN_ACTIVE} or updated > ?) and {_SHOWN} order by id desc limit ?",
            (*ACTIVE, time.time() - hours * 3600, limit),
        )

    def unseen(self) -> list[dict]:
        return self._rows(f"select * from tasks where seen = 0 and {_SHOWN} order by id")

    def runs(self, job: int, limit: int = 10) -> list[dict]:
        """A scheduled job's latest runs, newest first, the hidden ones (silent checks) included."""
        return self._rows("select * from tasks where job = ? order by id desc limit ?", (job, limit))

    def prune_hidden(self, days: float) -> list[int]:
        """Forget hidden scheduled runs (watch checks that found nothing) that ended more than `days` ago, with their
        logs; returns their ids. The newest task is always kept, so no id is ever handed out twice."""
        ids = [r["id"] for r in self.db.execute(
            f"select id from tasks where hidden and not {_IN_ACTIVE} and updated < ? and id < (select max(id) from tasks)",
            (*ACTIVE, time.time() - days * 86400))]
        if ids:
            marks = ", ".join("?" * len(ids))
            self.db.execute("begin")
            self.db.execute(f"delete from log where task in ({marks})", ids)
            self.db.execute(f"delete from tasks where id in ({marks})", ids)
            self.db.execute("commit")
        return ids

    def mark_seen(self, ids: list[int]) -> None:
        self.db.executemany("update tasks set seen = 1 where id = ?", [(i,) for i in ids])

    def log(self, task_id: int, kind: str, text: str) -> None:
        self.db.execute("insert into log values (?, ?, ?, ?)", (task_id, time.time(), kind, text))

    def last_logged(self, task_id: int) -> float:
        """When the task's latest log line was written; 0 if it has none."""
        return self.db.execute("select max(at) from log where task = ?", (task_id,)).fetchone()[0] or 0.0

    # ── Approvals: risky host commands waiting for the user's yes ──

    def approval(self, approval_id: int) -> dict | None:
        row = self.db.execute("select * from approvals where id = ?", (approval_id,)).fetchone()
        return _approval(row) if row else None

    def approvals(self, task_id: int | None = None, states: tuple[str, ...] = (PENDING,)) -> list[dict]:
        """A task's approvals (or every task's) in these states, oldest first."""
        marks = ", ".join("?" * len(states))
        sql = f"select * from approvals where state in ({marks})" + (" and task = ?" if task_id is not None else "")
        rows = self.db.execute(sql + " order by id", (*states, *(() if task_id is None else (task_id,))))
        return [_approval(r) for r in rows]

    def ask_approval(self, task_id: int, item: dict, expires: float, batch_seconds: float) -> tuple[dict, bool]:
        """Add a risky command to the task's requests: to one already pending for the identical command, or to
        the one it opened in the last `batch_seconds` and hasn't announced yet, or else a new one. Returns the
        request, and whether the command is new to it."""
        now = time.time()
        pending = self.approvals(task_id)
        if same := next((a for a in pending if any(same_command(c, item) for c in a["commands"])), None):
            return same, False
        if batch := next((a for a in pending if not a["announced"] and now - a["created"] < batch_seconds), None):
            self.set_approval(batch["id"], commands=json.dumps([*batch["commands"], item], ensure_ascii=False))
            return self.approval(batch["id"]), True
        cur = self.db.execute("insert into approvals (task, created, expires, commands, state) values (?, ?, ?, ?, ?)",
                              (task_id, now, expires, json.dumps([item], ensure_ascii=False), PENDING))
        return self.approval(cur.lastrowid), True

    def set_approval(self, approval_id: int, **fields) -> dict:
        columns = ", ".join(f"{k} = ?" for k in fields)  # keys come from code, never from input
        self.db.execute(f"update approvals set {columns} where id = ?", (*fields.values(), approval_id))
        return self.approval(approval_id)

    def answer_approval(self, approval_id: int, state: str, via: str) -> bool:
        """Record the first answer to a pending request; False if it had already been answered (or lapsed)."""
        cur = self.db.execute("update approvals set state = ?, via = ?, answered_at = ? where id = ? and state = ?",
                              (state, via, time.time(), approval_id, PENDING))
        return cur.rowcount == 1

    def use_approved(self, task_id: int, item: dict) -> dict | None:
        """If the user said yes to exactly this command for the task, mark it run and return the request it was in.
        The yes lasts the run it resumed (then it's spent): run again in it, say after a failure, it isn't asked about
        again."""
        for a in self.approvals(task_id, (APPROVED,)):
            same = [i for i, c in enumerate(a["commands"]) if same_command(c, item)]
            if fresh := [i for i in same if i not in a["used"]]:
                return self.set_approval(a["id"], used=json.dumps([*a["used"], fresh[0]]))
            if same:
                return a
        return None

    def history(self, task_id: int, limit: int = 30, steps: bool = False) -> list[dict]:
        """The task's latest `limit` log entries, oldest first; its steps (each tool call) only if asked."""
        return self._rows(
            "select at, kind, text from (select rowid, * from log where task = ? and (? or kind != 'step')"
            " order by rowid desc limit ?) order by rowid",
            (task_id, steps, limit),
        )
