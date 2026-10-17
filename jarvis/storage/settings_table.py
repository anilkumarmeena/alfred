"""The profile database's settings table: one value per key. User settings (jarvis.state.settings) and the vault's
check row (jarvis.auth.vault) live here."""

import sqlite3
from contextlib import AbstractContextManager

from jarvis.storage import database

_SCHEMA = """
create table settings (
    key text primary key,
    value text not null
)"""
_MIGRATIONS = (
    "select 1",  # 2: dropped the retired heartbeat's sources; kept as a no-op so later versions keep their numbers
)


def _db() -> AbstractContextManager[sqlite3.Connection]:
    return database.table("settings", _SCHEMA, migrations=_MIGRATIONS)


def get(key: str, default: str = "") -> str:
    with _db() as db:
        row = db.execute("select value from settings where key = ?", (key,)).fetchone()
    return row[0] if row else default


def put(key: str, value: str) -> None:
    """Set `key`; an empty value deletes it."""
    with _db() as db:
        if value:
            db.execute("insert into settings values (?, ?) on conflict(key) do update set value = excluded.value",
                       (key, value))
        else:
            db.execute("delete from settings where key = ?", (key,))
