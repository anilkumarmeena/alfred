"""The user's to-do list (todos table in the profile database).

Text API (voice tools, task runner) matches items by wording: find(), done(), reword(). The control panel works by
id: items(), check(), edit(), delete(), restore(). read() renders a Markdown checklist, open items first.

Repeats: add_new() (voice tool, suggested edits) won't add an item the list already has. repeat_of() finds it: worded
the same (ignoring case and punctuation), else judged by the todo_match role among the few items sharing most of its words.
The panel's add() takes what the user typed as it is.
"""

import asyncio
import json
import logging
import re
import sqlite3
import time
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime

from jarvis import prompts
from jarvis.config import TODO_MATCH_ENABLED, TODO_MATCH_SECONDS
from jarvis.llm.roles import Role
from jarvis.safety import scan
from jarvis.storage import database, profile

log = logging.getLogger("jarvis.state.todo")

HEADER = "# To-do"
_LISTED = re.compile(r"^\s*-\s*\[ \]\s*|\s*\(added \d{4}-\d{2}-\d{2}\)\s*$")  # strips read()'s decoration from a quoted line
# A lookalike shares at least LOOKALIKE of the shorter item's words (common short words aside); at most LOOKALIKES of
# them, likeliest first, go to the model. Real repeats share over half; unrelated items rarely a third.
LOOKALIKE = 0.45
LOOKALIKES = 5
_COMMON = frozenset("a an and as at be by for from in into is it its of on or so than that the then this to with"
                    .split())

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


def _tidy(text: str) -> str:
    """`text` without invisible characters or extra spacing; ValueError if nothing's left."""
    if not (text := " ".join(scan.clean(text).split())):
        raise ValueError("The to-do is empty.")
    return text


def add(item: str) -> str:
    item = _tidy(item)
    with _db() as db:
        db.execute("insert into todos (text, created) values (?, ?)", (item, time.time()))
    return f"Added to the to-do list: {item}"


@dataclass
class Repeat:
    """The item on the list a new to-do repeats, and both as one sentence when the new one adds detail."""
    item: dict
    merged: str = ""


def _words(text: str) -> list[str]:
    return re.findall(r"[^\W_]+", text.casefold())


def _likeness(a: set[str], b: set[str]) -> float:
    """Share of the shorter set's words in the other, so a brief item still matches a fuller one."""
    return len(a & b) / min(len(a), len(b)) if a and b else 0.0


def _twin(item: str, listed: list[dict]) -> dict | None:
    """The item worded like `item`, ignoring case, spacing and punctuation: an open one before a done one."""
    words = _words(item)
    return next((i for i in listed if _words(i["text"]) == words), None)


def _lookalikes(item: str, listed: list[dict]) -> list[dict]:
    """Up to LOOKALIKES items sharing most of `item`'s words, likeliest first."""
    words = {w for w in _words(item) if w not in _COMMON}
    scored = [(_likeness(words, {w for w in _words(i["text"]) if w not in _COMMON}), i) for i in listed]
    return [i for score, i in sorted(scored, key=lambda s: -s[0]) if score >= LOOKALIKE][:LOOKALIKES]


_MATCH = {"type": "object", "properties": {"same": {"type": ["integer", "null"]}, "merged": {"type": "string"}},
          "required": ["same"]}


async def repeat_of(item: str) -> Repeat | None:
    """The item on the list that `item` repeats, if any. A model call that fails or times out finds none."""
    listed = await asyncio.to_thread(items)
    if same := _twin(item, listed):
        return Repeat(same)
    if not TODO_MATCH_ENABLED or not (near := _lookalikes(item, listed)):
        return None
    from jarvis.llm import claude

    shown = [{"n": n, "text": i["text"], "state": "done" if i["done"] else "open"} for n, i in enumerate(near, 1)]
    try:
        found, _ = await claude.ask_json(json.dumps({"new": item, "listed": shown}, ensure_ascii=False), _MATCH,
                                      prompts.load("todo_match", user_name=profile.name()), Role.TODO_MATCH,
                                      timeout=TODO_MATCH_SECONDS, effort="low")
    except Exception as e:
        log.warning("checking a new to-do for repeats failed (%s); it's added", type(e).__name__)
        return None
    if not found or not 1 <= (n := found["same"] or 0) <= len(near):
        return None
    old = near[n - 1]
    merged = " ".join(scan.clean(found.get("merged") or "").split())
    words = _words(merged)
    return Repeat(old, merged if words and words != _words(old["text"]) else "")  # empty or the same words: nothing new


def repeated(found: Repeat) -> str:
    """Where the repeated item stands, quoting it."""
    old = found.item
    if not old["done"]:
        return f"it's already on the to-do list as '{old['text']}'"
    when = f" {_day(old['done_at'])}" if old["done_at"] else ""
    return f"it's already done (ticked off{when}): '{old['text']}'"


async def add_new(item: str, reopen: bool = False) -> str:
    """Add `item` unless it repeats one on the list. An open repeat stays, reworded as the merged sentence when `item`
    adds detail; a done one is reopened (and reworded the same way) if `reopen`, else left. The reply names it."""
    item = _tidy(item)
    if not (found := await repeat_of(item)):
        return await asyncio.to_thread(add, item)
    return await asyncio.to_thread(_settle_repeat, found, reopen)


def _settle_repeat(found: Repeat, reopen: bool) -> str:
    old, merged = found.item, found.merged
    if old["done"] and not reopen:
        return f"Not added: {repeated(found)}."
    if merged:
        edit(old["id"], merged)
    if old["done"]:
        check(old["id"], False)
        now = f", now reading: {merged}" if merged else "."
        return f"Not added twice: {repeated(found)}, so it's back on the list{now}"
    if merged:
        return f"Merged into the to-do already on the list, '{old['text']}', which now reads: {merged}"
    return f"Not added: {repeated(found)}."


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
    _change(item_id, "update todos set text = ? where id = ?", _tidy(text))


def delete(item_id: int) -> None:
    _change(item_id, "delete from todos where id = ?")


def _when(value) -> float | None:
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) and value > 0 else None


def restore(item: dict) -> None:
    """Put back an item just deleted (the panel's Undo): in its old place, or at the end if a new one took its id."""
    text = _tidy(str(item.get("text", "")))
    done = bool(item.get("done"))
    row = (text, int(done), _when(item.get("created")) or time.time(), (_when(item.get("done_at")) or time.time()) if done else None)
    with _db() as db:
        try:
            db.execute("insert into todos (id, text, done, created, done_at) values (?, ?, ?, ?, ?)", (int(item["id"]), *row))
        except sqlite3.IntegrityError:
            db.execute("insert into todos (text, done, created, done_at) values (?, ?, ?, ?)", row)
