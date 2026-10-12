"""A machine's journal: what its task runs report, kept until the main computer has it.

The link drops whenever either computer sleeps, so every step, progress line, question, approval request and ending
is written here first, numbered, and sent in order; the main computer acknowledges each one. Whatever wasn't
acknowledged goes again on the next connection, so nothing is lost and nothing jumps the queue.
"""

import asyncio
import json
import sqlite3
import time
from pathlib import Path

_SCHEMA = """
create table if not exists events (
    seq integer primary key autoincrement,
    at real not null,
    kind text not null,
    task integer not null,
    run text not null,
    body text not null
)"""


class Journal:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self.db.execute("pragma journal_mode = wal")
        self.db.execute(_SCHEMA)
        self.added = asyncio.Event()  # set on add; the sender clears it once it has caught up

    def add(self, kind: str, task: int, run: str, body: dict) -> int:
        cur = self.db.execute("insert into events (at, kind, task, run, body) values (?, ?, ?, ?, ?)",
                              (time.time(), kind, task, run, json.dumps(body, ensure_ascii=False)))
        self.added.set()
        return cur.lastrowid or 0

    def pending(self, limit: int = 200) -> list[dict]:
        rows = self.db.execute("select seq, at, kind, task, run, body from events order by seq limit ?", (limit,))
        return [{"seq": s, "at": at, "kind": k, "task": t, "run": r, "body": json.loads(b)} for s, at, k, t, r, b in rows]

    def done(self, seq: int) -> None:
        self.db.execute("delete from events where seq = ?", (seq,))
