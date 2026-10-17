"""Tests for jarvis.storage.database: connections, ordered migrations, batched task-step writes."""

import asyncio
import sqlite3
import threading
import time

import pytest

from jarvis.storage import database
from jarvis.tasks import runner
from jarvis.tasks.store import Store

NOTES = "create table notes (id integer primary key, text text not null)"


@pytest.fixture
def db(own_profile):
    return own_profile.db


def raw(path) -> sqlite3.Connection:
    """A separate connection, as another process would have."""
    return sqlite3.connect(path, isolation_level=None)


def versions(path) -> dict[str, int]:
    return dict(raw(path).execute("select name, version from schema_versions"))


# ── Connections ─────────────────────────────────────────────────


def test_every_connection_waits_for_a_writer_and_uses_wal(db):
    conn = database.connect()
    assert conn.execute("pragma busy_timeout").fetchone()[0] == database.BUSY_SECONDS * 1000
    assert conn.execute("pragma journal_mode").fetchone()[0] == "wal"
    assert conn.execute("pragma synchronous").fetchone()[0] == 1  # normal
    assert conn.isolation_level is None and conn.row_factory is sqlite3.Row


def test_the_runner_and_tables_share_that_connection(db, monkeypatch):
    made = []
    real = database.connect
    monkeypatch.setattr(database, "connect", lambda *a, **kw: made.append(a) or real(*a, **kw))
    Store()
    with database.table("notes", NOTES):
        pass
    assert len(made) == 2


# ── Migrations ──────────────────────────────────────────────────


def test_a_new_table_is_made_then_migrated_in_order_once(db):
    ran = []
    migrations = ("alter table notes add column tag text", lambda c: ran.append(2), lambda c: ran.append(3))
    with database.table("notes", NOTES, migrations=migrations) as conn:
        conn.execute("insert into notes (text, tag) values ('a', 'x')")
    with database.table("notes", NOTES, migrations=migrations):
        pass
    assert ran == [2, 3] and versions(db) == {"notes": 4}


def test_made_runs_once_only_when_the_table_is_made(db, tmp_path):
    seeded = []
    for _ in range(2):
        with database.table("notes", NOTES, made=lambda: seeded.append(1)):
            pass
    with database.table("notes", NOTES, at=tmp_path / "other.db", made=lambda: seeded.append(2)):
        pass
    assert seeded == [1, 2]


def test_a_migration_added_later_runs_on_a_table_already_versioned(db):
    with database.table("notes", NOTES) as conn:
        conn.execute("insert into notes (text) values ('kept')")
    assert versions(db) == {"notes": 1}

    def backfill(conn):
        conn.execute("update notes set tag = upper(text)")

    with database.table("notes", NOTES, migrations=["alter table notes add column tag text", backfill]) as conn:
        assert [tuple(r) for r in conn.execute("select text, tag from notes")] == [("kept", "KEPT")]
    assert versions(db) == {"notes": 3}


def test_a_migration_that_fails_changes_nothing(db):
    with database.table("notes", NOTES) as conn:
        conn.execute("insert into notes (text) values ('kept')")

    def broken(conn):
        conn.execute("delete from notes")
        raise sqlite3.OperationalError("half done")

    with pytest.raises(sqlite3.OperationalError), database.table("notes", NOTES, migrations=[broken]):
        pass
    assert raw(db).execute("select text from notes").fetchall() == [("kept",)] and versions(db) == {"notes": 1}


def test_a_table_a_newer_jarvis_migrated_further_is_left_alone(db):
    with database.table("notes", NOTES, migrations=["alter table notes add column tag text"]):
        pass
    with database.table("notes", NOTES) as conn:  # older code, which knows only version 1
        conn.execute("insert into notes (text, tag) values ('a', 'b')")
    assert versions(db) == {"notes": 2}


def test_a_dropped_table_is_made_afresh(db):
    with database.table("notes", NOTES, migrations=["alter table notes add column tag text"]) as conn:
        conn.execute("drop table notes")
    with database.table("notes", NOTES, migrations=["alter table notes add column tag text"]) as conn:
        conn.execute("insert into notes (text, tag) values ('a', 'b')")


def test_processes_opening_it_at_once_migrate_it_once(db):
    ran, start = [], threading.Barrier(4)

    def slow(conn):
        ran.append(1)
        time.sleep(0.05)

    def open_it():
        start.wait()
        with database.table("notes", NOTES, migrations=[slow]):
            pass

    threads = [threading.Thread(target=open_it) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert ran == [1] and versions(db) == {"notes": 2}


# ── Steps, written in batches ───────────────────────────────────


def logged(path, task_id: int) -> list[tuple[str, str]]:
    return raw(path).execute("select kind, text from log where task = ? order by rowid", (task_id,)).fetchall()


def test_steps_wait_in_memory_then_go_in_one_write(db):
    store = Store()
    task = store.create("research", "prices", "x", None)
    for text in ("searching the web", "searching the web", "reading a page"):
        store.step(task["id"], text)
    assert logged(db, task["id"]) == []  # nothing written yet
    store.write_steps()
    assert logged(db, task["id"]) == [("step", "searching the web"), ("step", "reading a page")]
    assert raw(db).execute("select activity from tasks").fetchone()[0] == "reading a page"


def test_anything_else_the_store_does_comes_after_the_steps_before_it(db):
    store = Store()
    task = store.create("research", "prices", "x", None)
    store.step(task["id"], "reading a page")
    store.log(task["id"], "progress", "found it")
    assert store.get(task["id"])["activity"] == "reading a page"
    assert logged(db, task["id"]) == [("step", "reading a page"), ("progress", "found it")]
    store.set(task["id"], activity="found it")
    store.step(task["id"], "reading a page")  # differs from the activity set since: logged again
    store.write_steps()
    assert logged(db, task["id"])[-1] == ("step", "reading a page")


def test_steps_that_cant_be_written_are_dropped_and_the_store_carries_on(db, monkeypatch, caplog):
    monkeypatch.setattr(database, "BUSY_SECONDS", 0.05)
    store = Store()
    task = store.create("research", "prices", "x", None)
    store.step(task["id"], "reading a page")
    other = raw(db)
    other.execute("begin immediate")  # another process holding the write lock
    store.write_steps()
    other.execute("rollback")
    assert "couldn't write 1 steps" in caplog.text
    store.step(task["id"], "reading another")
    assert store.history(task["id"], steps=True)[-1]["text"] == "reading another"


def test_the_runner_writes_steps_on_a_thread_every_so_often(db):
    async def main():
        r = runner.Runner()
        task = r.store.create("research", "prices", "x", None)
        writer = asyncio.create_task(r._write_steps(every=0.01))
        threads = []
        real = r.store.write_steps
        r.store.write_steps = lambda: threads.append(threading.current_thread()) or real()
        r.activity(task["id"], "searching the web")
        for _ in range(100):
            await asyncio.sleep(0.01)
            if logged(db, task["id"]):
                break
        writer.cancel()
        return task, threads

    task, threads = asyncio.run(main())
    assert logged(db, task["id"]) == [("step", "searching the web")]
    assert threads and threading.main_thread() not in threads
