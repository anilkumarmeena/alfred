"""The user's to-do list, kept in the todos table of data/tasks.db beside the background tasks.

read() gives the list as a markdown checklist: open items in the order added, then the done ones.

The voice agent (its to-do tools), the runner (applying tasks' suggested edits, and ticking off the item a task was
started for: jarvis.tasks.goal) and the control panel
(jarvis.panel, which works by id: items(), then check(), edit() or delete()) use it, each opening the database
for a moment per call.
"""

import re
import sqlite3
import time
from contextlib import AbstractContextManager
from datetime import datetime

from jarvis.core import database
from jarvis.safety import scan

HEADER = "# To-do"
_LISTED = re.compile(r"^\s*-\s*\[ \]\s*|\s*\(added \d{4}-\d{2}-\d{2}\)\s*$")  # an open item's line in read()

_SCHEMA = """
create table todos (
    id integer primary key,     -- the list's order
    text text not null,
    done integer not null default 0,
    created real not null,
    done_at real                -- when it was ticked off; null while open
)"""


def _db() -> AbstractContextManager[sqlite3.Connection]:
    return database.table("todos", _SCHEMA)


def _day(at: float) -> str:
    return f"{datetime.fromtimestamp(at):%Y-%m-%d}"


def read() -> str:
    """The list as a markdown checklist, "- [ ]" open and "- [x]" done; empty when there's nothing on it."""
    with _db() as db:
        rows = db.execute("select * from todos order by done, id").fetchall()
    if not rows:
        return ""
    lines = [HEADER, ""]
    lines += [f"- [ ] {r['text']} (added {_day(r['created'])})" for r in rows if not r["done"]] or ["Nothing open."]
    if done := [r for r in rows if r["done"]]:
        lines += ["", "## Done"]
        lines += [f"- [x] {r['text']}" + (f" (done {_day(r['done_at'])})" if r["done_at"] else "") for r in done]
    return "\n".join(lines) + "\n"


def add(item: str) -> str:
    item = " ".join(scan.clean(item).split())  # no invisible characters (jarvis.safety.scan)
    if not item:
        raise ValueError("The to-do is empty.")
    with _db() as db:
        db.execute("insert into todos (text, created) values (?, ?)", (item, time.time()))
    return f"Added to the to-do list: {item}"


def find(text: str) -> dict:
    """The one open item containing `text`, or worded exactly as it is; read()'s "- [ ]" and "(added ...)" are let
    through."""
    text = _LISTED.sub("", text).strip()
    with _db() as db:
        rows = db.execute("select * from todos where not done order by id").fetchall()
    wanted = text.lower()
    matches = [r for r in rows if r["text"].lower() == wanted] or [r for r in rows if wanted and wanted in r["text"].lower()]
    if len(matches) != 1:
        found = "No open to-do" if not matches else f"{len(matches)} open to-dos"
        raise ValueError(f"{found} matches '{text}'; say more of it.")
    return dict(matches[0])


def get(item_id: int) -> dict | None:
    with _db() as db:
        row = db.execute("select * from todos where id = ?", (item_id,)).fetchone()
    return dict(row) | {"done": bool(row["done"])} if row else None


def done(text: str) -> str:
    """Tick off the one open item containing `text`."""
    item = find(text)
    check(item["id"])
    return f"Ticked off: {item['text']}"


def reword(text: str, new: str) -> str:
    """Reword the one open item containing `text`."""
    item = find(text)
    edit(item["id"], new)
    return f"Reworded the to-do '{item['text']}' as: {get(item['id'])['text']}"


def items() -> list[dict]:
    """Every item, open ones first in the order added, then the done ones, latest ticked off first."""
    with _db() as db:
        rows = db.execute("select * from todos order by done, case when done then -done_at end, id").fetchall()
    return [dict(r) | {"done": bool(r["done"])} for r in rows]


def _change(item_id: int, sql: str, *params) -> None:
    with _db() as db:
        if db.execute(sql, (*params, item_id)).rowcount != 1:
            raise ValueError(f"There's no to-do {item_id}; it may have been removed.")


def check(item_id: int, done: bool = True) -> None:
    """Tick the item off, or put it back on the list."""
    _change(item_id, "update todos set done = ?, done_at = ? where id = ?", int(done), time.time() if done else None)


def edit(item_id: int, text: str) -> None:
    text = " ".join(scan.clean(text).split())
    if not text:
        raise ValueError("The to-do is empty.")
    _change(item_id, "update todos set text = ? where id = ?", text)


def delete(item_id: int) -> None:
    _change(item_id, "delete from todos where id = ?")
