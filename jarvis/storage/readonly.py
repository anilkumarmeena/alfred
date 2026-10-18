"""Read-only SQL on the profile's database, for the lookup helpers (jarvis.brain.side) to answer questions about
JARVIS's own records: a task's cost, its steps and progress, the projects, the schedules.

Held to reading in code three times over: the file is opened read-only (mode=ro), the connection is query_only, and
an authorizer lets a statement do nothing but select, so a write, ATTACH or PRAGMA is refused before it runs. The
authorizer also keeps to the tables listed in READABLE, less the columns in HIDDEN_COLUMNS: the vault's secrets,
keys and voiceprints share the file, and a table added later stays out of reach until it's listed. Answers are cut
to ROWS rows of CELL characters a value, and a query is stopped after SECONDS.
"""

import sqlite3
import time
from pathlib import Path

from jarvis.storage import profile

ROWS = 50
CELL = 300
SECONDS = 5
# Never secrets (the vault's), settings (its check value), voiceprints or node_pairings (pairing codes' hashes)
READABLE = {"accounts", "activity", "approval_rules", "approvals", "injection_checks", "log", "mcp_servers",
            "mcp_tools", "nodes", "profiles", "projects", "schedules", "schema_versions", "skills", "tasks", "todos"}
HIDDEN_COLUMNS = {("approvals", "nonce_hash"), ("nodes", "public_key"), ("mcp_servers", "headers"),
                  ("mcp_servers", "secret_env")}
_ALLOWED = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_RECURSIVE}


class Refused(Exception):
    """Why a query didn't run, said to the helper."""


def _open(at: Path) -> tuple[sqlite3.Connection, set[str]]:
    """A read-only connection to `at`, and its tables out of reach."""
    if not at.is_file():
        raise Refused("there's no database yet.")
    db = sqlite3.connect(f"{at.as_uri()}?mode=ro", uri=True, timeout=SECONDS)
    db.execute("pragma query_only = 1")  # before the authorizer, which refuses every pragma
    tables = {n for (n,) in db.execute("select name from sqlite_master where type in ('table', 'view')")}
    return db, tables - READABLE


def _authorizer(hidden: set[str]):
    # Only real tables are checked: eponymous ones like json_each and pragma_table_info only compute
    def authorize(action: int, table: str | None, column: str | None, _db, _trigger) -> int:
        if action not in _ALLOWED or action == sqlite3.SQLITE_READ and (
                table in hidden or (table, column) in HIDDEN_COLUMNS):
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK
    return authorize


def query(sql: str, at: Path | None = None) -> str:
    """One SELECT's result as tab-separated lines under its column names, at most ROWS of them. Refused for anything
    but a single read, or one reaching a hidden table or column, or taking over SECONDS."""
    db, hidden = _open(at or profile.current().db)
    deadline = time.monotonic() + SECONDS
    db.set_authorizer(_authorizer(hidden))
    db.set_progress_handler(lambda: time.monotonic() > deadline, 10_000)  # nonzero interrupts it
    try:
        cursor = db.execute(sql)
        rows = cursor.fetchmany(ROWS + 1)
    except sqlite3.Error as e:
        said = "it took too long" if "interrupt" in str(e) else str(e)
        if "not authorized" in said or "prohibited" in said:  # the authorizer's refusals
            said += ": only a SELECT runs here, and secrets, keys and voiceprints stay out of reach"
        raise Refused(said) from None
    finally:
        db.close()
    if cursor.description is None:
        raise Refused("only a SELECT returns anything here.")
    lines = ["\t".join(c[0] for c in cursor.description)]
    lines += ["\t".join(_cell(v) for v in row) for row in rows[:ROWS]]
    if len(rows) > ROWS:
        lines.append(f"(only the first {ROWS} rows: narrow it with WHERE, or count with GROUP BY)")
    if not rows:
        lines.append("(no rows)")
    return "\n".join(lines)


def _cell(value: object) -> str:
    if isinstance(value, bytes):
        return f"<{len(value)} bytes>"
    text = " ".join(str(value).split()) if value is not None else "NULL"
    return text if len(text) <= CELL else text[:CELL - 1] + "…"


def schema(table: str = "", at: Path | None = None) -> str:
    """With no `table`, every table the helpers may read with its columns, one a line; with one, how it was created,
    with its columns' comments. Hidden tables are left out, and hidden columns said to be out of reach."""
    db, _ = _open(at or profile.current().db)
    try:
        names = [n for (n,) in db.execute("select name from sqlite_master where type = 'table' order by name")
                 if n in READABLE]
        if not table:
            return "\n".join(f"{n}: " + ", ".join(c for _, c, *_ in db.execute("select * from pragma_table_info(?)",
                                                                             (n,)) if (n, c) not in HIDDEN_COLUMNS)
                             for n in names)
        if table not in names:
            raise Refused(f"there's no table {table} to read; the tables are {', '.join(names)}.")
        (sql,) = db.execute("select sql from sqlite_master where name = ?", (table,)).fetchone()
        hidden = sorted(c for t, c in HIDDEN_COLUMNS if t == table)
        return sql + (f"\n(out of reach: {', '.join(hidden)})" if hidden else "")
    finally:
        db.close()
