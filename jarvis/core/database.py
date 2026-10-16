"""Task store and shared SQLite helpers for the profile's database (owner's: data/tasks.db).

Other modules' tables (to-dos, projects, accounts, settings, secrets, MCP servers) live in the same file, opened via
table(), which migrates them first. All connections come from connect(), scoped to the current profile.
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
KINDS = ("code_change", "code_question", "research")  # model-driven kinds
COMMAND = "command"  # shell command, no model (jarvis.tasks.command_jobs)
CONNECTION = "connection"  # backgrounded MCP call (jarvis.tasks.connection_jobs)
ACTIVE = ("queued", "running", "waiting")
# wait_kind values: approval of commands, requested access, an answer, or whether to start a task held because work
# it waited for failed (jarvis.tasks.chains)
WAIT_APPROVAL, WAIT_ACCESS, WAIT_QUESTION, WAIT_AFTER = "approval", "access", "question", "after"
_IN_ACTIVE = f"status in ({', '.join('?' * len(ACTIVE))})"
# Seconds to wait on another process's write before "database is locked" (runner, voice and web share the file)
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
    worktree text,              -- code_change only (as are in_place, branch, base)
    in_place integer not null default 0,  -- 1: edits the project folder directly, uncommitted
    branch text,                -- task branch; in-place: branch checked out at start
    base text,                  -- commit changes are measured against
    activity text,              -- current activity
    question text,              -- set while waiting for the user
    wait_kind text,             -- WAIT_* value
    nudges text,                -- JSON list of undelivered messages
    edits text,                 -- JSON list of suggested data-file edits (jarvis.tasks.data_edits)
    summary text,               -- session's final message
    spoken text,                -- latest spoken update
    changes text,               -- git shortstat vs base
    error text,
    cost real not null default 0,
    seen integer not null default 1, -- 0 until the latest update is heard
    job integer,                -- scheduled job this is a run of
    hidden integer not null default 0, -- 1: silent scheduled run, omitted from lists
    grants text,                -- JSON list of extra access (jarvis.tasks.access)
    wants text,                 -- JSON list of unanswered access requests
    orders text,                -- JSON list of {said, at}: user's standing orders
    command text,               -- command tasks: JSON spec and outcome
    fixes integer,              -- id of the failed command task this fixes
    todo integer,               -- to-do item it serves, settled on finish
    interrupted text            -- JSON {at, on_mac, prompt}: cut off by shutdown, to resume
)"""
# interrupted also holds {prompt, after} while a task waits for a usage limit's reset (Runner._launch)
_TASKS_MIGRATIONS = (
    "alter table tasks add column node integer",  # 2: the machine it runs on (jarvis.node); null = this computer
    # 3, 4: JSON lists of {task, merged} its start, or its merge, waits for (jarvis.tasks.chains)
    "alter table tasks add column waits_on text",
    "alter table tasks add column merge_waits_on text",
)
_LOG = """
create table log (
    task integer not null,
    at real not null,
    kind text not null,
    text text not null
)"""
_LOG_MIGRATIONS = (
    "create index if not exists log_task on log(task)",  # 2: per-task reads of a long log
)
_APPROVALS = """
create table approvals (
    id integer primary key,
    task integer not null,
    created real not null,
    expires real not null,
    commands text not null,     -- JSON list of {tool, command, folder, why}, answered together
    state text not null,        -- pending, approved, denied, expired, cancelled; spent after its run
    announced integer not null default 0,  -- 1 once push and voice were notified
    nonce_hash text,            -- sha256 of the push's one-time answer nonce
    answered_at real,
    via text,                   -- push, web, panel, voice, cli, assistant, rule, order
    used text                   -- JSON list of indexes of commands already run
)"""
PENDING, APPROVED, DENIED, EXPIRED, CANCELLED, SPENT = "pending", "approved", "denied", "expired", "cancelled", "spent"
_SHOWN = "not hidden"

# ── Connections and schema versions ─────────────────────────────

_VERSIONS = """
create table if not exists schema_versions (
    name text primary key,      -- table name
    version integer not null,   -- migrations applied
    at real not null
)"""
Migration = str | Callable[[sqlite3.Connection], object]  # SQL statement or fn(connection)


def connect(at: Path | None = None, **kwargs) -> sqlite3.Connection:
    """Open the current profile's DB (or `at`): autocommit, Row factory, BUSY_SECONDS timeout, WAL, synchronous=NORMAL.

    Transactions must be begun explicitly. `kwargs` pass through to sqlite3.connect."""
    db = sqlite3.connect(at or profile.current().db, timeout=BUSY_SECONDS, isolation_level=None, **kwargs)
    try:
        db.row_factory = sqlite3.Row
        db.execute("pragma journal_mode = wal")  # persistent; no-op after the first
        db.execute("pragma synchronous = normal")
    except sqlite3.Error:
        db.close()
        raise
    return db


def _has(db: sqlite3.Connection, name: str) -> bool:
    return db.execute("select 1 from sqlite_master where name = ?", (name,)).fetchone() is not None


def _version(db: sqlite3.Connection, name: str) -> int:
    """Migrations applied to table `name`; 0 if it doesn't exist."""
    try:
        row = db.execute("select version from schema_versions where name = ? and exists"
                         " (select 1 from sqlite_master where name = ?)", (name, name)).fetchone()
    except sqlite3.OperationalError:  # no schema_versions yet
        return 0
    return row[0] if row else 0


def migrate(db: sqlite3.Connection, name: str, schema: Sequence[str], migrations: Sequence[Migration] = ()) -> bool:
    """Bring table `name` to its latest version in one transaction; returns whether it created the table.

    Version 1 is `schema`; `migrations` are versions 2 on, each run once and counted in schema_versions. Released
    schemas and migrations never change: append a new migration instead. A table a newer JARVIS migrated further is
    left alone."""
    latest, made = 1 + len(migrations), False
    if _version(db, name) >= latest:
        return made
    db.execute("begin immediate")  # serialise concurrent migrators
    try:
        db.execute(_VERSIONS)
        have = _version(db, name)
        if not have:
            if made := not _has(db, name):
                for statement in schema:  # not executescript: it would commit and drop the lock
                    db.execute(statement)
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
def table(name: str, *schema: str, migrations: Sequence[Migration] = (),
          made: Callable[[], object] | None = None, at: Path | None = None) -> Iterator[sqlite3.Connection]:
    """Yield a connection with table `name` migrated; call `made()` if it was just created. Closed on exit."""
    db = connect(at)
    try:
        if migrate(db, name, schema, migrations) and made:
            made()  # after commit, so it may open its own connections
        yield db
    finally:
        if db.in_transaction:
            db.execute("rollback")
        db.close()


def minutes_since_update(task: dict) -> int:
    return round((time.time() - task["updated"]) / 60)


def nudges(task: dict) -> list[str]:
    """Messages waiting to reach the task, oldest first."""
    return json.loads(task.get("nudges") or "[]")


def edits(task: dict) -> list[dict]:
    """Suggested data-file edits, numbered from 1, each with a status."""
    return json.loads(task.get("edits") or "[]")


def same_command(a: dict, b: dict) -> bool:
    """Same tool, command and folder, running the same scripts unchanged (their fingerprints, commands.seen). `why`
    is ignored: it varies with the running processes."""
    return all(a.get(k) == b.get(k) for k in ("tool", "command", "folder")) and (a.get("scripts") or {}) == (
        b.get("scripts") or {})


def _approval(row: sqlite3.Row) -> dict:
    a = dict(row)
    a["commands"], a["used"] = json.loads(a["commands"]), json.loads(a["used"] or "[]")
    return a


class Store:
    def __init__(self) -> None:
        self._file = profile.current().db  # pinned so write_steps on another thread uses it
        self._db = connect(self._file)
        migrate(self._db, "tasks", [_TASKS], migrations=_TASKS_MIGRATIONS)
        migrate(self._db, "log", [_LOG], migrations=_LOG_MIGRATIONS)
        migrate(self._db, "approvals", [_APPROVALS])
        # Buffered tool calls (task, at, activity, logged) for write_steps(). Deque: filled on the event loop, drained
        # on another thread.
        self._steps: deque[tuple[int, float, str, bool]] = deque()
        self._shown: dict[int, str] = {}  # last activity per task, to skip repeats
        self._writing = threading.Lock()
        self._writer: sqlite3.Connection | None = None  # write_steps may run on any thread

    @property
    def db(self) -> sqlite3.Connection:
        """The connection, after flushing buffered steps so later reads/writes see them."""
        self.write_steps()
        return self._db

    def step(self, task_id: int, text: str) -> None:
        """Buffer a tool call as the task's activity (logged unless a repeat). No DB call; flushed by write_steps()."""
        self._steps.append((task_id, time.time(), text, self._shown.get(task_id) != text))
        self._shown[task_id] = text

    def write_steps(self) -> None:
        """Flush buffered steps in one transaction: log rows plus each task's latest activity.

        On failure (e.g. locked DB) the steps are dropped with a warning; they are informational only."""
        with self._writing:
            steps = [self._steps.popleft() for _ in range(len(self._steps))]  # later appends wait for the next flush
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
        columns = ", ".join(f"{k} = ?" for k in fields)  # keys come from code, never input
        self.db.execute(f"update tasks set {columns} where id = ?", (*fields.values(), task_id))

    def update(self, task_id: int, **fields) -> dict:
        """set(), returning the updated task."""
        self.set(task_id, **fields)
        return self.get(task_id)

    def add_nudge(self, task_id: int, text: str) -> dict:
        """Queue a message for the task, after any already waiting; returns the task."""
        return self.update(task_id, nudges=json.dumps([*nudges(self.get(task_id)), text]))

    def take_nudges(self, task_id: int) -> list[str]:
        """Pop the task's pending messages, oldest first."""
        if waiting := nudges(self.get(task_id)):
            self.set(task_id, nudges=None)
        return waiting

    def add_edit(self, task_id: int, edit: dict) -> dict:
        """Append a pending suggested edit with the next number; return it."""
        kept = edits(self.get(task_id))
        kept.append(edit := {"n": len(kept) + 1, **edit, "status": "pending"})
        self.set(task_id, edits=json.dumps(kept, ensure_ascii=False))
        return edit

    def set_edits(self, task_id: int, changed: list[dict]) -> dict:
        """Replace the task's full edit list; return the task."""
        return self.update(task_id, edits=json.dumps(changed, ensure_ascii=False))

    def _rows(self, sql: str, params: tuple = ()) -> list[dict]:
        return [dict(r) for r in self.db.execute(sql, params)]

    def active(self) -> list[dict]:
        return self._rows(f"select * from tasks where {_IN_ACTIVE} order by id", ACTIVE)

    def recent(self, hours: float = 24, limit: int = 20) -> list[dict]:
        """Active tasks plus those updated in the last `hours`, excluding hidden runs."""
        return self._rows(
            f"select * from tasks where ({_IN_ACTIVE} or updated > ?) and {_SHOWN} order by id desc limit ?",
            (*ACTIVE, time.time() - hours * 3600, limit),
        )

    def unseen(self) -> list[dict]:
        return self._rows(f"select * from tasks where seen = 0 and {_SHOWN} order by id")

    def ended_as(self, statuses: tuple[str, ...], hours: float) -> list[dict]:
        """Shown tasks in one of `statuses` (e.g. stopped, failed), updated in the last `hours`, oldest first."""
        return self._rows(f"select * from tasks where status in ({', '.join('?' * len(statuses))}) and updated > ?"
                          f" and {_SHOWN} order by id", (*statuses, time.time() - hours * 3600))

    def todos_taken(self) -> frozenset[int]:
        """To-do items a task works on, or worked on until it stopped or failed: all but finished or discarded ones."""
        return frozenset(r["todo"] for r in self.db.execute(
            "select distinct todo from tasks where todo is not null and status not in ('done', 'discarded')"))

    def runs(self, job: int, limit: int = 10) -> list[dict]:
        """A scheduled job's latest runs, newest first, hidden ones included."""
        return self._rows("select * from tasks where job = ? order by id desc limit ?", (job, limit))

    def prune_hidden(self, days: float) -> list[int]:
        """Delete hidden finished runs older than `days`, with their logs; return their ids.

        The newest task is always kept so its id is never reused."""
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

    def waiting(self) -> list[dict]:
        """Tasks whose start waits for other work (still waiting, or held), or that have a merge waiting; oldest
        first."""
        return self._rows("select * from tasks where (waits_on is not null and status in ('queued', 'waiting'))"
                          " or merge_waits_on is not null order by id")

    def merged(self, task_id: int) -> bool:
        """Whether the task's branch has been merged (GitOps.merge logs it)."""
        return self.db.execute("select 1 from log where task = ? and kind = 'merged' limit 1",
                               (task_id,)).fetchone() is not None

    def mark_seen(self, ids: list[int]) -> None:
        self.db.executemany("update tasks set seen = 1 where id = ?", [(i,) for i in ids])

    def log(self, task_id: int, kind: str, text: str) -> None:
        self.db.execute("insert into log values (?, ?, ?, ?)", (task_id, time.time(), kind, text))

    def last_logged(self, task_id: int) -> float:
        """Time of the task's latest log line; 0 if none."""
        return self.db.execute("select max(at) from log where task = ?", (task_id,)).fetchone()[0] or 0.0

    # ── Approvals: risky host commands awaiting a yes ──

    def approval(self, approval_id: int) -> dict | None:
        row = self.db.execute("select * from approvals where id = ?", (approval_id,)).fetchone()
        return _approval(row) if row else None

    def approvals(self, task_id: int | None = None, states: tuple[str, ...] = (PENDING,)) -> list[dict]:
        """Approvals in `states` for one task (or all), oldest first."""
        marks = ", ".join("?" * len(states))
        sql = f"select * from approvals where state in ({marks})" + (" and task = ?" if task_id is not None else "")
        rows = self.db.execute(sql + " order by id", (*states, *(() if task_id is None else (task_id,))))
        return [_approval(r) for r in rows]

    def ask_approval(self, task_id: int, item: dict, expires: float, batch_seconds: float) -> tuple[dict, bool]:
        """Attach a risky command to a pending request (an identical one, or an unannounced one opened within
        `batch_seconds`), else open a new one. Returns (request, whether the command was added)."""
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
        columns = ", ".join(f"{k} = ?" for k in fields)  # keys come from code, never input
        self.db.execute(f"update approvals set {columns} where id = ?", (*fields.values(), approval_id))
        return self.approval(approval_id)

    def answer_approval(self, approval_id: int, state: str, via: str) -> bool:
        """Record the first answer to a pending request; False if already answered or lapsed."""
        cur = self.db.execute("update approvals set state = ?, via = ?, answered_at = ? where id = ? and state = ?",
                              (state, via, time.time(), approval_id, PENDING))
        return cur.rowcount == 1

    def use_approved(self, task_id: int, item: dict) -> dict | None:
        """If this exact command was approved, mark it used and return its request, else None.

        Approval lasts for the resumed run, so a retry within it isn't asked again."""
        for a in self.approvals(task_id, (APPROVED,)):
            same = [i for i, c in enumerate(a["commands"]) if same_command(c, item)]
            if fresh := [i for i in same if i not in a["used"]]:
                return self.set_approval(a["id"], used=json.dumps([*a["used"], fresh[0]]))
            if same:
                return a
        return None

    def history(self, task_id: int, limit: int = 30, steps: bool = False) -> list[dict]:
        """Latest `limit` log entries, oldest first; tool-call steps only if `steps`."""
        return self._rows(
            "select at, kind, text from (select rowid, * from log where task = ? and (? or kind != 'step')"
            " order by rowid desc limit ?) order by rowid",
            (task_id, steps, limit),
        )
