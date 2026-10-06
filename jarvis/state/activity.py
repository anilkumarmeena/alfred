"""What JARVIS did lately, for the bell in the web page's control panel: the activity table of data/tasks.db.

Two processes add to it. The voice brain (jarvis.brain.cli) adds each of its tools that changed something (those
registered with `changes`, jarvis.tools) and each MCP server write, once it has run without an error; the task runner
adds tasks starting, finishing, failing, asking, being merged or discarded, requests for a yes and their answers, and
heartbeat alerts. Reads and searches are left out. jarvis.server sends new rows to open pages as they come; entries
older than KEEP_DAYS go.
Adding never raises: the bell is a convenience, so a failure there is logged and the caller carries on.
"""

import logging
import re
import sqlite3
import time
from contextlib import AbstractContextManager

from jarvis.core import database

log = logging.getLogger("jarvis.state.activity")

KEEP_DAYS = 7
PRUNE_EVERY = 50  # entries added between clear-outs of those older than KEEP_DAYS
MOST = 200  # entries listed at once
MAX_TEXT = 240
KINDS = ("ok", "bad", "warn", "run")

_SCHEMA = """
create table activity (
    id integer primary key,
    at real not null,
    kind text not null,         -- ok, bad, warn or run: the colour of its light
    text text not null,
    task integer                -- the task it was about, if any
)"""

# A result that says nothing changed after all
_UNCHANGED = re.compile(r"nothing (changed|deleted)", re.IGNORECASE)
_URL = re.compile(r":?\s*https?://\S+")


def _db() -> AbstractContextManager[sqlite3.Connection]:
    return database.table("activity", _SCHEMA)


def _tidy(text: str) -> str:
    text = " ".join(_URL.sub("", text).split())
    return text if len(text) <= MAX_TEXT else text[:MAX_TEXT - 1].rstrip() + "…"


def add(text: str, kind: str = "ok", task: int | None = None) -> dict | None:
    """Keep an entry, now and then letting go of those older than KEEP_DAYS. None if there's nothing to say, or it
    couldn't be kept."""
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
    """Note a tool that changed something and has run without an error: as `said`, filled in from its arguments, or
    else as its result says it. Nothing if the result says nothing changed after all."""
    if _UNCHANGED.search(result):
        return
    if said:
        try:
            add(said.format(**args))
            return
        except (KeyError, IndexError, ValueError):
            pass
    first = result.strip()
    if first.startswith(("{", "[")) or not first:  # a structured result: say which tool, at least
        first = name.replace("_", " ").capitalize()
    add(first)


def recent(after: int = 0) -> list[dict]:
    """The latest entries in the last KEEP_DAYS, newest first; only those after the id `after`, if given."""
    with _db() as db:
        rows = db.execute("select * from activity where id > ? and at > ? order by id desc limit ?",
                          (after, time.time() - KEEP_DAYS * 86400, MOST)).fetchall()
    return [dict(r) for r in rows]


def latest_id() -> int:
    with _db() as db:
        return db.execute("select coalesce(max(id), 0) from activity").fetchone()[0]
