"""Tests for alfred.storage.readonly: the lookups read Alfred's database, and nothing they send can change it or reach
its secrets."""

import sqlite3

import pytest

from alfred.storage import database, readonly


@pytest.fixture
def db(own_profile):
    """The profile's database with two tasks, a log line, a vault secret and a voiceprint."""
    with database.table("tasks", "create table tasks (id integer primary key, title text, cost real)") as c:
        c.executemany("insert into tasks values (?, ?, ?)", [(1, "multi-repo folders", 2.5), (2, "the port", 0.25)])
        c.execute("create table log (task integer, at real, kind text, text text)")
        c.execute("insert into log values (1, 1700000000, 'progress', 'Plan settled.')")
        c.execute("create table secrets (name text primary key, blob blob)")
        c.execute("insert into secrets values ('GITHUB_TOKEN', x'00ff')")
        c.execute("create table voiceprints (name text, embedding blob)")
        c.execute("create table nodes (id integer, name text, public_key text)")
        c.execute("insert into nodes values (1, 'studio', 'AAAA')")
        c.execute("create table later (token text)")  # one no one has said may be read
    return own_profile.db


def test_a_select_answers_with_its_columns_and_rows(db):
    found = readonly.query("select id, title, cost from tasks where cost > 1")
    assert found.splitlines() == ["id\ttitle\tcost", "1\tmulti-repo folders\t2.5"]
    assert readonly.query("select sum(cost) as spent, count(*) from tasks") == "spent\tcount(*)\n2.75\t2"
    assert readonly.query("select title from tasks where id = 9") == "title\n(no rows)"
    joined = readonly.query("with t as (select * from tasks) select t.title, l.kind from t join log l on l.task = t.id")
    assert joined.splitlines()[1] == "multi-repo folders\tprogress"


@pytest.mark.parametrize("sql", [
    "update tasks set cost = 0",
    "delete from tasks",
    "insert into tasks values (3, 'x', 1)",
    "drop table tasks",
    "create table more (x)",
    "pragma query_only = 0",
    "attach database 'other.db' as other",
    "select 1; delete from tasks",
    "vacuum",
    "begin immediate",
])
def test_nothing_but_a_read_runs_and_the_database_is_left_as_it_was(db, sql):
    with pytest.raises(readonly.Refused):
        readonly.query(sql)
    with sqlite3.connect(db) as c:
        assert c.execute("select count(*), sum(cost) from tasks").fetchone() == (2, 2.75)
        assert {n for (n,) in c.execute("select name from sqlite_master")} >= {"tasks", "log", "secrets"}


@pytest.mark.parametrize("sql", [
    "select * from secrets",
    "select count(*) from secrets",
    "select name from secrets",
    "select t.title from tasks t join secrets s on 1",
    "select * from voiceprints",
    "select public_key from nodes",
    "select * from nodes",
    "select token from later",
])
def test_secrets_keys_voiceprints_and_unlisted_tables_stay_out_of_reach(db, sql):
    with pytest.raises(readonly.Refused, match="out of reach"):
        readonly.query(sql)
    assert readonly.query("select name from nodes") == "name\nstudio"  # the rest of a table stays readable
    assert readonly.query("select count(*) from json_each('[1, 2]')") == "count(*)\n2"  # functions still work


def test_long_answers_and_slow_queries_are_cut_short(db, monkeypatch):
    monkeypatch.setattr(readonly, "ROWS", 2)
    many = readonly.query("with recursive n(i) as (select 1 union all select i + 1 from n where i < 10) "
                          "select i, printf('%.900c', 'x') from n").splitlines()
    assert len(many) == 4 and "only the first 2 rows" in many[-1] and len(many[1]) < readonly.CELL + 10
    monkeypatch.setattr(readonly, "SECONDS", 0)
    with pytest.raises(readonly.Refused, match="too long"):
        readonly.query("with recursive n(i) as (select 1 union all select i + 1 from n) select count(*) from n")


def test_the_schema_lists_what_can_be_read_and_explains_one_table(db):
    tables = readonly.schema()
    assert "tasks: id, title, cost" in tables and "nodes: id, name\n" in tables + "\n"
    assert not any(hidden in tables for hidden in ("secrets", "voiceprints", "later", "public_key"))
    assert readonly.schema("tasks").startswith("CREATE TABLE tasks (")
    assert "out of reach: public_key" in readonly.schema("nodes")
    with pytest.raises(readonly.Refused, match="no table secrets"):
        readonly.schema("secrets")


def test_with_no_database_yet_it_says_so(own_profile):
    with pytest.raises(readonly.Refused, match="no database yet"):
        readonly.query("select 1")
    assert not own_profile.db.exists()  # reading never makes one
