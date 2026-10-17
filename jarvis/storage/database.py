"""The profile's SQLite database (owner's: data/tasks.db): connections and table migrations.

Each part keeps its own tables in the same file (tasks: jarvis.tasks.store; to-dos, projects, accounts, settings,
secrets, MCP servers...), opened via table(), which migrates them first. All connections come from connect(), scoped
to the current profile.
"""

import logging
import sqlite3
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path

from jarvis.storage import profile

log = logging.getLogger("jarvis.storage.database")
BUSY_SECONDS = 10

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
