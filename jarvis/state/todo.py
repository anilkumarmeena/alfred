"""The user's to-do list (todos table in the profile database).

Text API (voice tools, task runner) matches items by wording: find(), done(), reword(). The control panel works by
id: items(), check(), edit(), delete(), restore(). read() renders a Markdown checklist, open items first.
"""

import re
import sqlite3
import time
from contextlib import AbstractContextManager
from datetime import datetime

from jarvis.core import database
from jarvis.safety import scan

HEADER = "# To-do"
_LISTED = re.compile(r"^\s*-\s*\[ \]\s*|\s*\(added \d{4}-\d{2}-\d{2}\)\s*$")  # strips read()'s decoration from a quoted line

_SCHEMA = """
create table todos (
    id integer primary key,     -- list order
    text text not null,
    done integer not null default 0,
    created real not null,
    done_at real                -- null while open
)"""


def _db() -> AbstractContextManager[sqlite3.Connection]:
    return database.table("todos", _SCHEMA)


def _day(at: float) -> str:
    return f"{datetime.fromtimestamp(at):%Y-%m-%d}"


def read() -> str:
    """Markdown checklist ("- [ ]" open, "- [x]" done); "" if the list is empty."""
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
    item = " ".join(scan.clean(item).split())  # strip invisible characters
    if not item:
        raise ValueError("The to-do is empty.")
    with _db() as db:
        db.execute("insert into todos (text, created) values (?, ?)", (item, time.time()))
    return f"Added to the to-do list: {item}"


def find(text: str) -> dict:
    """The single open item matching `text` exactly, else by substring. Accepts lines quoted from read().

    ValueError if none or several match."""
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
    """Tick off the open item matching `text`."""
    item = find(text)
    check(item["id"])
    return f"Ticked off: {item['text']}"


def reword(text: str, new: str) -> str:
    """Reword the open item matching `text`."""
    item = find(text)
    edit(item["id"], new)
    return f"Reworded the to-do '{item['text']}' as: {get(item['id'])['text']}"


def items() -> list[dict]:
    """All items: open in added order, then done, most recently ticked first."""
    with _db() as db:
        rows = db.execute("select * from todos order by done, case when done then -done_at end, id").fetchall()
    return [dict(r) | {"done": bool(r["done"])} for r in rows]


def _change(item_id: int, sql: str, *params) -> None:
    with _db() as db:
        if db.execute(sql, (*params, item_id)).rowcount != 1:
            raise ValueError(f"There's no to-do {item_id}; it may have been removed.")


def check(item_id: int, done: bool = True) -> None:
    """Mark done, or reopen with `done=False`."""
    _change(item_id, "update todos set done = ?, done_at = ? where id = ?", int(done), time.time() if done else None)


def edit(item_id: int, text: str) -> None:
    text = " ".join(scan.clean(text).split())
    if not text:
        raise ValueError("The to-do is empty.")
    _change(item_id, "update todos set text = ? where id = ?", text)


def delete(item_id: int) -> None:
    _change(item_id, "delete from todos where id = ?")


def _when(value) -> float | None:
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) and value > 0 else None


def restore(item: dict) -> None:
    """Put back an item just deleted (the panel's Undo): in its old place, or at the end if a new one took its id."""
    text = " ".join(scan.clean(str(item.get("text", ""))).split())
    if not text:
        raise ValueError("The to-do is empty.")
    done = bool(item.get("done"))
    row = (text, int(done), _when(item.get("created")) or time.time(), (_when(item.get("done_at")) or time.time()) if done else None)
    with _db() as db:
        try:
            db.execute("insert into todos (id, text, done, created, done_at) values (?, ?, ?, ?, ?)", (int(item["id"]), *row))
        except sqlite3.IntegrityError:
            db.execute("insert into todos (text, done, created, done_at) values (?, ?, ?, ?)", row)
