"""Tests for jarvis.tasks.tool_watch: which tools a watch may call, calling them, reading their results into items,
and old Gmail and Chat watches becoming tool watches."""

import asyncio
import sys
import textwrap
from contextlib import closing
from datetime import datetime, timedelta

import pytest
from test_mcp_servers import fresh  # noqa: F401  (fixture)

from conftest import use_profile
from jarvis.integrations import mcp_servers
from jarvis.integrations.mcp_servers import Server, Tool
from jarvis.storage import database, profile
from jarvis.tasks import schedules, tool_watch
from jarvis.tasks.scheduler import ProbeFailed


def test_a_result_is_read_as_the_list_in_it_or_its_lines():
    issues = [{"number": 1}, {"number": 2}]
    assert tool_watch.items(issues) == issues
    assert tool_watch.items({"messages": issues, "notes": ["one account failed"]}) == issues
    assert tool_watch.items({"unread": [], "notes": ["Chat is blocked"]}) == []  # nothing, not the notes
    assert tool_watch.items({"data": {"repository": {"issues": issues}}}) == issues
    assert tool_watch.items({"state": "open", "title": "Fix it"}) == [{"state": "open", "title": "Fix it"}]
    assert tool_watch.items("#1 Fix it\n\n  #2 Ship it  \n") == ["#1 Fix it", "#2 Ship it"]
    assert len(tool_watch.items(list(range(500)))) == tool_watch.MAX_ITEMS


def test_an_item_is_known_by_its_content_whatever_the_order_of_its_fields():
    assert tool_watch.key({"a": 1, "b": "x"}) == tool_watch.key({"b": "x", "a": 1})
    assert tool_watch.key({"a": 1, "b": "x"}) != tool_watch.key({"a": 1, "b": "y"})  # changed: new
    assert tool_watch.key("#1 Fix it") != tool_watch.key("#1 Fixed it")


def test_an_item_is_dated_by_when_it_was_written():
    sent = datetime(2026, 10, 6, 9, 15, tzinfo=profile.tz())
    assert tool_watch.dated({"date": "Tue, 06 Oct 2026 03:45:00 +0000"}) == datetime.fromisoformat(
        "2026-10-06T03:45:00+00:00").timestamp()
    assert tool_watch.dated({"created_at": "2026-10-06T03:45:00Z"}) == datetime.fromisoformat(
        "2026-10-06T03:45:00+00:00").timestamp()
    assert tool_watch.dated({"createTime": "2026-10-06T09:15:00"}) == sent.timestamp()  # local, without a zone
    assert tool_watch.dated({"date": "soon", "created": "2026-10-06T09:15:00"}) == sent.timestamp()
    assert tool_watch.dated({"updated_at": "2026-10-06T09:15:00"}) is None  # when it changed, not when it was made
    lately = datetime.now(profile.tz()).replace(second=0, microsecond=0) - timedelta(days=3)
    assert tool_watch.dated({"last_active": f"{lately:%a %d %b %H:%M}"}) == lately.timestamp()  # as Chat gives it
    assert tool_watch.dated("#1 Fix it") is None


def test_only_reading_tools_of_accounts_and_switched_on_connections_can_be_watched(fresh):  # noqa: F811
    tools = {"list_issues": Tool("read"), "mark_read": Tool("own"), "create_issue": Tool("write", "create an issue")}
    mcp_servers.save(Server("acme", "Acme", command="acme-mcp", tools=tools), new=True)
    mcp_servers.save(Server("off", "Off", command="off-mcp", enabled=False, tools=tools), new=True)
    assert tool_watch.problem("gmail_search") == "" and tool_watch.problem("chat_unread") == ""
    assert tool_watch.problem("mcp__acme__list_issues") == ""
    assert "isn't one of Acme's reading tools" in tool_watch.problem("mcp__acme__mark_read")  # changes their inbox
    assert "isn't one of Acme's reading tools" in tool_watch.problem("mcp__acme__create_issue")
    assert "isn't one of Acme's reading tools" in tool_watch.problem("mcp__acme__unlisted")
    assert "switched on" in tool_watch.problem("mcp__off__list_issues")
    assert "switched on" in tool_watch.problem("mcp__gone__list_issues")
    for name in ("gmail_modify", "gmail_draft_send", "chat_draft", "calendar_draft_invite", "task_list", "nothing"):
        assert "isn't a reading tool a watch can call" in tool_watch.problem(name), name


ISSUES_SERVER = textwrap.dedent("""
    import json, sys
    for line in sys.stdin:
        m = json.loads(line)
        if "id" not in m:
            continue
        if m["method"] == "initialize":
            result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}, "serverInfo": {"name": "fake"}}
        elif m["params"]["name"] == "list_issues":
            issues = [{"number": 7, "title": "Crash on start", "repo": m["params"]["arguments"]["repo"]}]
            result = {"content": [{"type": "text", "text": json.dumps(issues)}]}
        elif m["params"]["name"] == "search":
            result = {"content": [{"type": "text", "text": "found"}], "structuredContent": {"hits": [{"id": 1}]}}
        else:
            result = {"isError": True, "content": [{"type": "text", "text": "bad credentials"}]}
        print(json.dumps({"jsonrpc": "2.0", "id": m["id"], "result": result}), flush=True)
""")


def test_a_connections_reading_tool_is_called_with_its_arguments(fresh):  # noqa: F811
    script = fresh / "issues.py"
    script.write_text(ISSUES_SERVER)
    tools = {"list_issues": Tool("read"), "search": Tool("read"), "whoami": Tool("read")}
    mcp_servers.save(Server("acme", "Acme", command=sys.executable, args=[str(script)], tools=tools), new=True)
    found = asyncio.run(tool_watch.call("mcp__acme__list_issues", {"repo": "acme/app"}))
    assert found == [{"number": 7, "title": "Crash on start", "repo": "acme/app"}]
    assert asyncio.run(tool_watch.call("mcp__acme__search", {})) == {"hits": [{"id": 1}]}  # its structured content
    with pytest.raises(ProbeFailed, match="^Acme refused the call$"):  # its words go only to the log
        asyncio.run(tool_watch.call("mcp__acme__whoami", {}))


def test_a_watched_tool_that_can_no_longer_be_called_fails_its_check(fresh):  # noqa: F811
    mcp_servers.save(Server("acme", "Acme", command="acme-mcp", enabled=False,
                            tools={"list_issues": Tool("read")}), new=True)
    with pytest.raises(ProbeFailed, match="isn't a tool of a connection that's switched on"):
        asyncio.run(tool_watch.call("mcp__acme__list_issues", {}))


def schedules_version() -> int:
    with closing(database.connect()) as db:
        return db.execute("select version from schema_versions where name = 'schedules'").fetchone()[0]


def test_old_gmail_and_chat_watches_become_tool_watches_after_the_resume_and_todo_migration(monkeypatch, tmp_path):
    use_profile(monkeypatch, tmp_path)
    # As a live database has it: migrated to 2 (resume and todo picks), with watches, and a resume job, of before
    with database.table("schedules", schedules._SCHEMA, migrations=schedules._MIGRATIONS[:1]) as db:
        for probe, target, baseline, status in (("gmail", "from:priya", None, "active"),
                                                ("chat", "Priya", '["work:m1"]', "paused"),
                                                ("url", "https://app.example", None, "active"),
                                                ("gmail", "from:sam", None, "done")):
            db.execute("insert into schedules (kind, title, text, every, probe, target, baseline, status, next_run, "
                       "created, updated) values ('watch', ?, 'x', 10, ?, ?, ?, ?, 1, 1, 1)",
                       (probe, probe, target, baseline, status))
        db.execute("insert into schedules (kind, title, text, at, days, status, next_run, created, updated, picks) "
                   "values ('resume', 'Overnight', '', '03:00', 'mon', 'active', 2, 1, 1, '{\"tasks\": [12, 14]}')")
    assert schedules_version() == 2
    gmail, chat, url, resume, done = schedules.items(finished_days=36500)
    assert schedules_version() == 1 + len(schedules._MIGRATIONS) == 4
    assert (gmail["probe"], gmail["target"], gmail["expect"]) == ("tool", "gmail_search", None)
    assert schedules.arguments(gmail) == {"query": "from:priya", "max_results": 10}
    assert (chat["probe"], chat["target"], chat["expect"], chat["baseline"]) == ("tool", "chat_unread", "Priya", None)
    assert schedules.arguments(chat) == {"mentions_only": True, "days": 1} and chat["status"] == "paused"
    assert (url["probe"], url["target"], url["arguments"]) == ("url", "https://app.example", None)
    assert schedules.check_text(gmail) == ('JARVIS calls the tool: gmail_search with {"query": "from:priya", '
                                           '"max_results": 10}')
    assert schedules.check_text(done) == ('JARVIS calls the tool: gmail_search with {"query": "from:sam", '
                                          '"max_results": 10}')  # finished ones too, as the app lists them
    assert schedules.picks(resume) == {"tasks": [12, 14]} and schedules.work_text(resume) == "resumes tasks 12, 14"


def test_a_fresh_database_gets_every_schedules_migration(monkeypatch, tmp_path):
    use_profile(monkeypatch, tmp_path)
    assert schedules.items() == []
    with closing(database.connect()) as db:
        columns = {r["name"] for r in db.execute("pragma table_info(schedules)")}
    assert {"picks", "arguments"} <= columns and schedules_version() == 4
    job = schedules.add("resume", "Overnight", "", when="03:00", repeat="daily", tasks=[], failed=True)
    assert schedules.picks(job) == {"failed": True} and job["arguments"] is None
