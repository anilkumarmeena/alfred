"""Recent-activity feed for the control panel's bell (activity table).

Writers: the voice brain (successful state-changing tools and MCP writes) and the task runner (task lifecycle,
approvals, alerts). Reads/searches are not recorded. Entries older than KEEP_DAYS are pruned.
add() never raises: failures are logged and the caller carries on.
"""

import logging
import re
import sqlite3
import time
from contextlib import AbstractContextManager

from jarvis.storage import database

log = logging.getLogger("jarvis.state.activity")

KEEP_DAYS = 7
PRUNE_EVERY = 50  # prune every N inserts
MOST = 200  # max entries returned
MAX_TEXT = 240
KINDS = ("ok", "bad", "warn", "run")

_SCHEMA = """
create table activity (
    id integer primary key,
    at real not null,
    kind text not null,         -- ok, bad, warn, run (indicator colour)
    text text not null,
    task integer                -- related task, if any
)"""

# Tool results meaning no change happened
_UNCHANGED = re.compile(r"nothing (changed|deleted)", re.IGNORECASE)
_URL = re.compile(r":?\s*https?://\S+")


def _db() -> AbstractContextManager[sqlite3.Connection]:
    return database.table("activity", _SCHEMA)


def _tidy(text: str) -> str:
    text = " ".join(_URL.sub("", text).split())
    return text if len(text) <= MAX_TEXT else text[:MAX_TEXT - 1].rstrip() + "…"


def add(text: str, kind: str = "ok", task: int | None = None) -> dict | None:
    """Insert an entry (pruning occasionally); return it, or None if empty or the write failed."""
    if not (text := _tidy(text)):
        return None
    if kind not in KINDS:
        kind = "ok"
    now = time.time()
    try:
        with _db() as db:
            row = db.execute("insert into activity (at, kind, text, task) values (?, ?, ?, ?)",
                             (now, kind, text, task)).lastrowid
            if row % PRUNE_EVERY == 0:
                db.execute("delete from activity where at < ?", (now - KEEP_DAYS * 86400,))
    except (sqlite3.Error, OSError):
        log.exception("couldn't add to the activity list")
        return None
    return {"id": row, "at": now, "kind": kind, "text": text, "task": task}


def tool_ran(name: str, args: dict, result: str, said: str = "") -> None:
    """Record a successful state-changing tool call: `said` formatted with `args`, else the result's text.

    Skipped when the result reports no change."""
    if _UNCHANGED.search(result):
        return
    if said:
        try:
            add(said.format(**args))
            return
        except (KeyError, IndexError, ValueError):
            pass
    first = result.strip()
    if first.startswith(("{", "[")) or not first:  # structured result: fall back to the tool name
        first = name.replace("_", " ").capitalize()
    add(first)


def recent(after: int = 0) -> list[dict]:
    """Entries from the last KEEP_DAYS with id > `after`, newest first."""
    with _db() as db:
        rows = db.execute("select * from activity where id > ? and at > ? order by id desc limit ?",
                          (after, time.time() - KEEP_DAYS * 86400, MOST)).fetchall()
    return [dict(r) for r in rows]


def latest_id() -> int:
    with _db() as db:
        return db.execute("select coalesce(max(id), 0) from activity").fetchone()[0]
