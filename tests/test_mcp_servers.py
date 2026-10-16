"""Tests for integrations.mcp_servers and mcp_launch: server list, secret-holding launcher, tool sorting, sessions."""

import json
import os
import re
import subprocess
import sys
import textwrap
from contextlib import closing
from dataclasses import replace

import pytest
from conftest import use_profile
from test_in_place import NullReporter, worker_task

from jarvis import tools
from jarvis.brain import cli, sdk
from jarvis.core import config, database, vault
from jarvis.integrations import mcp_launch, mcp_relay, mcp_servers
from jarvis.integrations.mcp_servers import Server, Tool
from jarvis.safety import shell
from jarvis.state import settings
from jarvis.tasks import worker

GH_TOKEN, GL_TOKEN = "ghp_not_a_real_token", "glpat-not-a-real-token"
GH_VAR, GL_VAR = "GITHUB_PERSONAL_ACCESS_TOKEN", "GITLAB_PERSONAL_ACCESS_TOKEN"


def _emptied() -> None:
    for name in vault.names():
        vault.remove(name)
    with mcp_servers._db() as db:
        db.execute("delete from mcp_servers")
        db.execute("delete from mcp_tools")


@pytest.fixture
def fresh(monkeypatch, tmp_path):
    """An empty list and vault, a master key, a scratch .env, and every program installed; emptied again after, so
    the connections a test saved reach no later test's tasks."""
    monkeypatch.setattr(config, "MASTER_KEY", vault.new_key())
    monkeypatch.setattr(vault, "ENV_FILE", tmp_path / "dotenv")  # not named .env: sandboxes hide those
    settings.put(vault.CHECK, "")
    _emptied()
    monkeypatch.setattr(mcp_servers, "which", lambda name: f"/opt/bin/{name}")
    monkeypatch.setattr(mcp_launch, "which", lambda name: f"/opt/bin/{name}")
    yield tmp_path
    _emptied()


def github() -> Server:
    """A hand-sorted GitHub entry, stored as an ordinary row."""
    return Server("github", "GitHub", command="github-mcp-server", args=["stdio", "--toolsets=repos,pull_requests"],
                  read_only_args=["--read-only"], secret_env={GH_VAR: "github.token"}, for_research=True,
                  prompt="pull requests, issues, notifications.",
                  tools={"list_notifications": Tool("read"), "pull_request_read": Tool("read"),
                         "dismiss_notification": Tool("own"),
                         "create_pull_request": Tool("write", "open a pull request in {owner}/{repo}"),
                         "delete_repository": Tool("blocked")})


def gitlab() -> Server:
    return Server("gitlab", "GitLab", command="npx", args=["-y", "@zereight/mcp-gitlab@2.1.69"],
                  env={"GITLAB_API_URL": "https://git.example.com/api/v4", "GITLAB_PERMISSION_MODE": "modify"},
                  read_only_env={"GITLAB_PERMISSION_MODE": "readonly"}, secret_env={GL_VAR: "gitlab.token"},
                  for_research=True, prompt="merge requests and to-dos, not the user's own to-do list.",
                  tools={"get_merge_request": Tool("read"), "list_todos": Tool("read"),
                         "create_merge_request_note": Tool("write", "comment on {project_id}")})


def seeded(github_on=True, gitlab_on=True):
    """Save GitHub and GitLab, each with its token, enabled when asked."""
    for server, token, wanted in ((github(), GH_TOKEN, github_on), (gitlab(), GL_TOKEN, gitlab_on)):
        server.enabled = wanted
        mcp_servers.save(server, new=True)
        if wanted:
            vault.ensure()
            vault.put(server.secret_env[next(iter(server.secret_env))], token)


# ── Stored rows ─────────────────────────────────────────────────

def test_tokens_are_kept_only_in_the_vault(fresh):
    seeded()
    servers = {s.name: s for s in mcp_servers.load()}
    assert set(servers) == {"github", "gitlab"} and all(s.enabled and s.reviewed for s in servers.values())
    assert vault.get("github.token") == GH_TOKEN and vault.get("gitlab.token") == GL_TOKEN
    with mcp_servers._db() as db:
        stored = json.dumps([list(r) for r in db.execute("select * from mcp_servers")])
    assert GH_TOKEN not in stored and GL_TOKEN not in stored


def test_nothing_is_put_in_on_a_first_start(fresh):
    with mcp_servers._db() as db:
        db.execute("drop table mcp_servers")
        db.execute("drop table mcp_tools")
    assert mcp_servers.load() == []  # no built-in servers are seeded


def test_gitlab_connections_saved_before_inline_comments_get_them_as_a_changing_tool(monkeypatch, tmp_path):
    use_profile(monkeypatch, tmp_path)
    listed = replace(gitlab(), env=gitlab().env | {"GITLAB_TOOLS": "get_merge_request,create_merge_request_note"})
    blocked = replace(gitlab(), name="lab2", tools={"create_merge_request_thread": Tool("blocked")})
    for server in (listed, blocked, github()):
        mcp_servers.save(server, new=True)
    with closing(database.connect()) as db:  # as a live database has them: migrated to 2
        db.execute("update schema_versions set version = 2 where name = 'mcp_servers'")
    servers = {s.name: s for s in mcp_servers.load()}
    assert servers["gitlab"].env["GITLAB_TOOLS"] == "get_merge_request,create_merge_request_note,create_merge_request_thread"
    assert servers["gitlab"].tools["create_merge_request_thread"] == Tool("write", mcp_servers.GITLAB_THREAD_SAYS)
    assert servers["lab2"].env == gitlab().env  # no allow-list: GitLab's own defaults already offer it
    assert servers["lab2"].tools["create_merge_request_thread"] == Tool("blocked")  # as the user sorted it
    assert (servers["github"].env, servers["github"].tools) == (github().env, github().tools)


def test_setting_a_secret_makes_the_master_key_when_theres_none(fresh, monkeypatch):
    monkeypatch.setattr(config, "MASTER_KEY", "")
    seeded(gitlab_on=False)
    assert vault.VAR in vault.ENV_FILE.read_text() and vault.get("github.token") == GH_TOKEN


def python(code: str, **env: str) -> subprocess.CompletedProcess:
    """Run `code` in a fresh interpreter with the launcher's env plus `env`."""
    return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60, cwd=config.ROOT,
                          env=os.environ | mcp_servers.launcher("x")["env"] | env)


# ── Sorting a server's tools ────────────────────────────────────

LISTED = [
    {"name": "search", "title": "Search pages", "annotations": {"readOnlyHint": True}},
    {"name": "create_page", "title": "Create a page", "annotations": {"readOnlyHint": False, "destructiveHint": False}},
    {"name": "delete_page", "title": "Delete {page}", "annotations": {"destructiveHint": True}},
    {"name": "mystery", "title": "", "annotations": {}},
]


def test_tools_are_sorted_by_their_own_markings_and_unmarked_ones_count_as_changing():
    sorted_ = mcp_servers.sort(LISTED)
    assert sorted_ == {"search": Tool("read"), "create_page": Tool("write", "create a page"),
                       "delete_page": Tool("write", "delete page"), "mystery": Tool("write")}
    assert [t["name"] for t in LISTED if mcp_servers.destructive(t)] == ["delete_page", "mystery"]


def test_the_users_choices_win_over_the_markings():
    sorted_ = mcp_servers.sort(LISTED, reading=["mystery"], changing=["search"], off=["delete_page"])
    assert sorted_["mystery"] == Tool("read") and sorted_["search"].access == "write"
    assert sorted_["delete_page"] == Tool("blocked") and mcp_servers.problems(Server("a", "A", command="x", tools=sorted_)) == []


FAKE_SERVER = textwrap.dedent("""
    import json, sys
    pages = {None: ([{"name": "look", "annotations": {"readOnlyHint": True}}], "p2"),
             "p2": ([{"name": "post", "title": "Post a message"}], None)}
    for line in sys.stdin:
        m = json.loads(line)
        if "id" not in m:
            continue
        if m["method"] == "initialize":
            print(json.dumps({"jsonrpc": "2.0", "method": "notifications/message", "params": {}}), flush=True)
            result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}, "serverInfo": {"name": "fake"}}
        else:
            tools, cursor = pages[(m.get("params") or {}).get("cursor")]
            result = {"tools": tools, **({"nextCursor": cursor} if cursor else {})}
        print(json.dumps({"jsonrpc": "2.0", "id": m["id"], "result": result}), flush=True)
""")


def test_a_server_thats_off_and_unsorted_is_started_to_list_every_page_of_its_tools(fresh, tmp_path):
    script = tmp_path / "fake_mcp.py"
    script.write_text(FAKE_SERVER)
    mcp_servers.save(Server("fake", "Fake", enabled=False, reviewed=False, command=sys.executable, args=[str(script)]),
                     new=True)
    found = mcp_servers.offered(mcp_servers.get("fake"), timeout=30)
    assert [t["name"] for t in found] == ["look", "post"] and found[1]["title"] == "Post a message"
    assert mcp_servers.sort(found) == {"look": Tool("read"), "post": Tool("write", "post a message")}


def test_a_server_that_doesnt_list_its_tools_says_what_it_said(fresh, tmp_path):
    script = tmp_path / "broken.py"
    script.write_text("import sys; print('no token, giving up', file=sys.stderr)")
    mcp_servers.save(Server("broken", "Broken", command=sys.executable, args=[str(script)]), new=True)
    with pytest.raises(RuntimeError, match="no token, giving up"):
        mcp_servers.offered(mcp_servers.get("broken"), timeout=30)


CHECKED_SERVER = textwrap.dedent("""
    import json, sys
    for line in sys.stdin:
        m = json.loads(line)
        if "id" not in m:
            continue
        if m["method"] == "initialize":
            result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}, "serverInfo": {"name": "fake"}}
        elif m["params"] == {"name": "whoami", "arguments": {}}:
            result = {"content": [{"type": "text", "text": "you are sam"}]}
        else:
            result = {"isError": True, "content": [{"type": "text", "text": "bad credentials"}]}
        print(json.dumps({"jsonrpc": "2.0", "id": m["id"], "result": result}), flush=True)
""")


def test_a_sign_in_check_says_whether_its_tool_answered_refused_or_nothing_came_back(fresh, tmp_path):
    script, silent = tmp_path / "checked.py", tmp_path / "silent.py"
    script.write_text(CHECKED_SERVER)
    silent.write_text("")
    tools = {"whoami": Tool("read"), "expired": Tool("read")}
    mcp_servers.save(Server("fake", "Fake", command=sys.executable, args=[str(script)], tools=tools,
                            check_tool="whoami"), new=True)
    mcp_servers.save(Server("quiet", "Quiet", command=sys.executable, args=[str(silent)], tools=tools,
                            check_tool="whoami"), new=True)
    fake = mcp_servers.get("fake")
    assert fake.check_tool == "whoami" and mcp_servers.check(fake, timeout=30) == ""
    assert mcp_servers.check(replace(fake, check_tool="expired"), timeout=30) == "bad credentials"
    assert mcp_servers.check(mcp_servers.get("quiet"), timeout=30) is None


def test_checking_from_the_command_line_says_only_whether_it_passed(fresh, tmp_path, capsys):
    script = tmp_path / "checked.py"
    script.write_text(CHECKED_SERVER)
    tools = {"whoami": Tool("read"), "expired": Tool("read"), "post": Tool("write")}
    mcp_servers.save(Server("fake", "Fake", command=sys.executable, args=[str(script)], tools=tools), new=True)
    assert mcp_servers.main(["check", "fake", "--tool", "whoami"]) == 0
    out = capsys.readouterr().out
    assert out == "Fake (whoami): signed in.\n" and "sam" not in out
    assert mcp_servers.main(["check", "fake", "--tool", "expired"]) == 1
    assert capsys.readouterr().out == "Fake (expired): refused: bad credentials\n"
    for argv in (["check", "fake"], ["check", "fake", "--tool", "post"]):
        with pytest.raises(SystemExit, match="reading tools"):
            mcp_servers.main(argv)


def test_a_sign_in_check_has_to_be_a_reading_tool(fresh, capsys):
    tools = {"whoami": Tool("read"), "post": Tool("write")}
    for check in ("post", "nothing"):
        with pytest.raises(ValueError, match="has to be one of its reading tools"):
            mcp_servers.save(Server("acme", "Acme", command="acme-mcp", tools=tools, check_tool=check), new=True)
    mcp_servers.save(Server("acme", "Acme", command="acme-mcp", tools=tools), new=True)
    assert mcp_servers.main(["tools", "acme", "--check", "whoami"]) == 0
    assert mcp_servers.get("acme").check_tool == "whoami" and "(its sign-in check)" in capsys.readouterr().out
    assert mcp_servers.main(["tools", "acme", "--forget", "whoami"]) == 0
    assert mcp_servers.get("acme").check_tool == ""  # gone with its tool


def test_an_unsorted_server_is_offered_to_no_session_even_when_on(fresh):
    mcp_servers.save(Server("acme", "Acme", command="acme-mcp", reviewed=False, tools={"look": Tool("read")}), new=True)
    assert mcp_servers.for_voice() == [] and mcp_servers.for_research() == []
    assert "sorted" in mcp_servers.missing(mcp_servers.get("acme"))
    assert mcp_servers.missing(mcp_servers.get("acme"), reviewed=False) == ""
    with pytest.raises(SystemExit, match="no enabled MCP server acme"):
        mcp_launch.main(["acme"])


def test_tools_sort_from_the_command_line_marks_it_sorted(fresh, monkeypatch, capsys):
    mcp_servers.save(Server("acme", "Acme", command="acme-mcp", reviewed=False), new=True)
    monkeypatch.setattr(mcp_servers, "offered", lambda server: LISTED)
    assert mcp_servers.main(["tools", "acme", "--sort", "--block", "delete_page"]) == 0
    acme = mcp_servers.get("acme")
    assert acme.reviewed and acme.tools["search"] == Tool("read") and acme.tools["delete_page"] == Tool("blocked")


# ── HTTP servers ────────────────────────────────────────────────

def test_an_http_server_with_a_key_or_a_sign_in_is_reached_through_the_launcher(fresh):
    mcp_servers.save(Server("notes", "Notes", transport="http", url="https://mcp.notes.example/mcp",
                            headers={"Authorization": "Bearer {NOTES_KEY}"}, secret_env={"NOTES_KEY": "notes.notes_key"},
                            tools={"find": Tool("read")}), new=True)
    mcp_servers.save(Server("wiki", "Wiki", transport="http", url="https://mcp.wiki.example/mcp", sign_in=True,
                            tools={"find": Tool("read")}), new=True)
    got = mcp_servers.session(mcp_servers.for_voice())
    assert got.configs == {}  # neither key set nor signed in
    assert "control panel" in mcp_servers.missing(mcp_servers.get("notes"))
    assert "connection_sign_in" in mcp_servers.missing(mcp_servers.get("wiki"))
    vault.ensure()
    vault.put("notes.notes_key", "k-123")
    vault.put("wiki.oauth", json.dumps({"access_token": "a"}))
    got = mcp_servers.session(mcp_servers.for_voice())
    for name in ("notes", "wiki"):
        assert got.configs[name]["args"] == ["-m", "jarvis.integrations.mcp_launch", name]
    assert "k-123" not in json.dumps(got.configs) and mcp_servers.session_env()["NOTES_KEY"] == ""
    assert mcp_servers.remove("wiki") == ["wiki.oauth"] and not vault.has("wiki.oauth")


@pytest.mark.parametrize("change, why", [
    ({"headers": {"Authorization": "Bearer {OTHER}"}}, "isn't one of its secrets"),
    ({"headers": {"Authorization": "Bearer abc123"}}, "looks like it holds a key"),
    ({"headers": {}}, "go in its headers"),
    ({"headers": {"Bad Header": "{K}"}}, "isn't a header name"),
])
def test_an_http_servers_headers_keep_their_keys_in_the_vault(change, why):
    server = Server("notes", "Notes", transport="http", url="https://n.example/mcp",
                    headers={"Authorization": "Bearer {K}"}, secret_env={"K": "notes.k"})
    assert mcp_servers.problems(server) == []
    assert any(why in p for p in mcp_servers.problems(replace(server, **change))), mcp_servers.problems(replace(server, **change))
    assert "Only an HTTP server" in " ".join(mcp_servers.problems(Server("x", "X", command="x", sign_in=True)))
# ── Checking a server ───────────────────────────────────────────

@pytest.mark.parametrize("change, why", [
    ({"name": "Bad Name"}, "can't name a server"),
    ({"name": "a__b"}, "can't name a server"),
    ({"name": "jarvis"}, "JARVIS's own"),
    ({"label": ""}, "label"),
    ({"command": ""}, "needs its program"),
    ({"command": "npx", "args": ["-y", "@acme/mcp"]}, "pin its version"),
    ({"command": "npx", "args": ["-y", "@acme/mcp@latest"]}, "pin its version"),
    ({"env": {"API_TOKEN": "x"}}, "looks like a secret"),
    ({"env": {"JARVIS_MASTER_KEY": "x"}}, "looks like a secret"),
    ({"secret_env": {"TOKEN": "Bad Name"}}, "can't name a secret"),
    ({"tools": {"x": Tool("maybe")}}, "access is one of"),
    ({"tools": {"x": Tool("read", "reads {a}")}}, "only write tools"),
    ({"transport": "http", "command": "", "url": "http://example.com"}, "https://"),
])
def test_bad_servers_are_refused_with_the_reason(change, why):
    server = Server("acme", "Acme", command="acme-mcp")
    for k, v in change.items():
        setattr(server, k, v)
    assert any(why in p for p in mcp_servers.problems(server)), mcp_servers.problems(server)


def test_pinned_package_fetchers_pass():
    for args in (["-y", "@acme/mcp@1.2.3"], ["acme-mcp@0.4.0"]):
        assert mcp_servers.problems(Server("acme", "Acme", command="npx", args=args)) == []
    assert mcp_servers.problems(Server("acme", "Acme", command="uvx", args=["acme-mcp==1.0"])) == []


def test_a_server_s_address_settings_are_found_even_while_unset():
    assert mcp_servers.addresses(gitlab()) == {"GITLAB_API_URL": "https://git.example.com/api/v4"}
    assert mcp_servers.addresses(github()) == {"GITHUB_HOST": ""}  # unset: the package's default, github.com
    acme = Server("acme", "Acme", command="acme-mcp", env={"ACME_BASE_URL": "https://acme.example", "PG_URL": "postgres://db",
                                                           "ACME_MODE": "fast"})
    assert mcp_servers.addresses(acme) == {"ACME_BASE_URL": "https://acme.example"}
    assert mcp_servers.addresses(Server("n", "N", transport="http", url="https://n.example/mcp")) == {}


@pytest.mark.parametrize("url", ["http://git.example.com/api/v4", "git.example.com", "https://", "ftp://git.example.com",
                                 "https://user:pw@git.example.com", "https://git.example.com/?x=1", "https://git example.com",
                                 "https://git.example.com:port/"])
def test_a_server_address_has_to_be_https(url):
    assert mcp_servers.bad_address(url)
    assert any("GITLAB_API_URL" in p for p in mcp_servers.problems(replace(gitlab(), env={"GITLAB_API_URL": url})))


def test_a_good_address_passes_and_moving_it_keeps_its_other_settings():
    for url in ("https://git.example.com/api/v4", "https://ghe.example.com", "http://localhost:8080/api"):
        assert mcp_servers.bad_address(url) == ""
    server = replace(gitlab(), read_only_env={"GITLAB_PERMISSION_MODE": "readonly", "GITLAB_API_URL": "https://old.example"})
    moved = mcp_servers.readdress(server, "GITLAB_API_URL", "https://code.example.com/api/v4")
    assert moved.env == {"GITLAB_API_URL": "https://code.example.com/api/v4", "GITLAB_PERMISSION_MODE": "modify"}
    assert moved.read_only_env == {"GITLAB_PERMISSION_MODE": "readonly"}  # research tasks reach the same server
    assert mcp_servers.readdress(moved, "GITLAB_API_URL", "").env == {"GITLAB_PERMISSION_MODE": "modify"}
    for var, url in (("GITLAB_PERMISSION_MODE", "https://x.example"), ("GITLAB_API_URL", "http://x.example")):
        with pytest.raises(ValueError):
            mcp_servers.readdress(server, var, url)


def test_a_new_address_reaches_the_server_at_its_next_start(fresh):
    seeded()
    mcp_servers.save(mcp_servers.readdress(mcp_servers.need("gitlab"), "GITLAB_API_URL", "https://code.example.com/api/v4"))
    for read_only in (False, True):
        _, env = mcp_launch.command(mcp_servers.need("gitlab"), read_only)
        assert env["GITLAB_API_URL"] == "https://code.example.com/api/v4" and env[GL_VAR] == GL_TOKEN


def test_save_refuses_a_bad_server_and_a_clash(fresh):
    mcp_servers.save(Server("acme", "Acme", command="acme-mcp"), new=True)
    with pytest.raises(ValueError, match="already"):
        mcp_servers.save(Server("acme", "Acme", command="acme-mcp"), new=True)
    with pytest.raises(ValueError, match="no other"):
        mcp_servers.save(Server("other", "Other", command="x"))


# ── Sessions ────────────────────────────────────────────────────

def test_the_voice_brain_gets_enabled_servers_reads_allowed_writes_gated_tokens_hidden(fresh):
    seeded()
    brain = cli.CliBrain()
    opts = cli._options(brain.shell, brain.jobs)
    gh = mcp_servers.get("github")
    assert {"github", "gitlab"} <= set(opts.mcp_servers)
    assert gh.tool("list_notifications") in opts.allowed_tools and gh.tool("dismiss_notification") in opts.allowed_tools
    assert gh.tool("create_pull_request") not in opts.allowed_tools  # so every call reaches the gate
    assert gh.tool("delete_repository") in opts.disallowed_tools
    assert set(brain.shell.servers) == {"github", "gitlab"} and opts.can_use_tool is brain.shell
    assert opts.env[vault.VAR] == "" and opts.env[GH_VAR] == ""
    assert "GitHub and GitLab:" in opts.system_prompt and "make the identical call again" in opts.system_prompt
    assert "own to-do list" in opts.system_prompt  # GitLab's own prompt text, after the general guidance


def test_the_mcp_config_names_the_server_never_a_secret(fresh):
    seeded()
    configs = mcp_servers.session(mcp_servers.for_voice()).configs
    for name, c in configs.items():
        assert c["command"] == sys.executable and c["args"] == ["-m", "jarvis.integrations.mcp_launch", name]
    text = json.dumps(configs)
    assert GH_TOKEN not in text and GL_TOKEN not in text and config.MASTER_KEY not in text


def test_a_change_applies_at_the_next_session_without_a_restart(fresh):
    seeded()
    brain = cli.CliBrain()
    mcp_servers.save(mcp_servers.Server(**{**mcp_servers.get("gitlab").__dict__, "enabled": False}))
    opts = cli._options(brain.shell, brain.jobs)
    assert "gitlab" not in opts.mcp_servers and "github" in opts.mcp_servers


def test_servers_whose_program_or_secret_is_missing_are_left_out(fresh, monkeypatch):
    seeded(gitlab_on=False)
    mcp_servers.save(Server(**{**mcp_servers.get("gitlab").__dict__, "enabled": True}))  # on, but no token
    assert set(mcp_servers.session(mcp_servers.for_voice()).configs) == {"github"}
    monkeypatch.setattr(mcp_servers, "which", lambda name: None)
    assert mcp_servers.session(mcp_servers.for_voice()).configs == {}


def test_research_tasks_get_research_servers_with_only_reads_allowed_outright(fresh):
    seeded()
    mcp_servers.save(Server("acme", "Acme", command="acme-mcp", tools={"look": Tool("read")}), new=True)
    opts = worker._options(worker_task(kind="research", project=None), NullReporter())
    assert set(opts.mcp_servers) >= {"github", "gitlab"} and "acme" not in opts.mcp_servers
    assert "--read-only" not in opts.mcp_servers["github"]["args"]  # changes are offered, each held for a yes
    changes = {s.tool(t) for s in mcp_servers.load() for t in s.named("write", "own")}
    assert not changes & set(opts.allowed_tools) and "mcp__gitlab__get_merge_request" in opts.allowed_tools
    assert "mcp__github__delete_repository" in opts.disallowed_tools
    assert opts.env[vault.VAR] == ""


def test_every_server_goes_through_the_launcher_and_the_voice_brains_wait_with_it(fresh):
    mcp_servers.save(Server("docs", "Docs", transport="http", url="https://mcp.example.com/mcp",
                            tools={"search": Tool("read")}), new=True)
    seeded(gitlab_on=False)
    got = mcp_servers.session(mcp_servers.for_voice(), wait=20)
    assert got.configs["docs"]["args"] == ["-m", "jarvis.integrations.mcp_launch", "docs", "--wait=20"]
    assert got.configs["github"]["args"][-1] == "--wait=20" and "mcp__docs__search" in got.allowed
    research = mcp_servers.session(mcp_servers.for_research(), ask_own=True)
    assert research.configs["github"]["args"][-1] == "github"  # tasks wait for their calls
    assert "mcp__github__dismiss_notification" not in research.allowed
    opts = cli._options(cli.CliBrain().shell, shell.Jobs())
    assert opts.mcp_servers["docs"]["args"][-1] == f"--wait={config.CONNECTION_WAIT:g}"


# ── Loading their tools on demand ───────────────────────────────

def test_the_voice_brain_loads_connections_tools_on_demand_and_its_own_up_front(fresh):
    seeded()
    brain = cli.CliBrain()
    opts = cli._options(brain.shell, brain.jobs)
    assert opts.mcp_servers["github"]["alwaysLoad"] is False and opts.mcp_servers["gitlab"]["alwaysLoad"] is False
    assert opts.mcp_servers["jarvis"]["alwaysLoad"] is True and opts.mcp_servers["shell"]["alwaysLoad"] is True
    assert sdk.TOOL_SEARCH_TOOL in opts.tools and sdk.TOOL_SEARCH_TOOL in opts.allowed_tools
    assert opts.env["ENABLE_TOOL_SEARCH"] == "true"
    assert "ToolSearch" in opts.system_prompt and "GitHub (mcp__github__…)" in opts.system_prompt


def test_research_tasks_load_connections_on_demand_and_their_own_tools_up_front(fresh):
    seeded()
    opts = worker._options(worker_task(kind="research", project=None), NullReporter())
    assert opts.mcp_servers["github"]["alwaysLoad"] is False
    assert all(opts.mcp_servers[s]["alwaysLoad"] is True for s in ("task", "jarvis", "repo"))
    assert "ToolSearch" in opts.tools and "ToolSearch" in opts.allowed_tools
    assert "ToolSearch" in opts.system_prompt


def test_a_session_without_connections_gets_no_tool_search(fresh):
    opts = cli._options(cli.CliBrain().shell, shell.Jobs())
    assert "ToolSearch" not in opts.tools and "ToolSearch" not in opts.allowed_tools
    assert "ENABLE_TOOL_SEARCH" not in opts.env and opts.mcp_servers["jarvis"]["alwaysLoad"] is True
    assert "ToolSearch" not in opts.system_prompt


def test_tool_search_off_loads_every_tool_up_front_as_before(fresh, monkeypatch):
    monkeypatch.setattr(mcp_servers, "ON_DEMAND", False)  # JARVIS_TOOL_SEARCH=false
    seeded()
    brain = cli.CliBrain()
    opts = cli._options(brain.shell, brain.jobs)
    assert "alwaysLoad" not in opts.mcp_servers["github"]  # the CLI would leave a deferred tool unloaded
    assert "ToolSearch" not in opts.tools and "ENABLE_TOOL_SEARCH" not in opts.env
    assert "ToolSearch" not in opts.system_prompt and "GitHub and GitLab:" in opts.system_prompt


def test_a_threshold_is_passed_to_the_cli_as_it_is(monkeypatch):
    monkeypatch.setattr(sdk, "TOOL_SEARCH", "auto:5")
    own = {"type": "sdk", "name": "jarvis", "instance": object()}
    got = sdk.on_demand({"tools": ["Bash"], "allowed_tools": ["WebSearch"], "env": {"A": "1"},
                         "mcp_servers": {"jarvis": own, "acme": {"type": "http", "url": "https://x", "alwaysLoad": False}}})
    assert got["env"] == {"A": "1", "ENABLE_TOOL_SEARCH": "auto:5"} and got["tools"] == ["Bash", "ToolSearch"]
    assert got["allowed_tools"] == ["WebSearch", "ToolSearch"]
    assert got["mcp_servers"]["jarvis"]["instance"] is own["instance"] and "alwaysLoad" not in own  # not changed in place


def test_the_old_github_tools_are_gone():
    assert not [t.name for t in tools.all_tools() if t.name.startswith("github")]
    assert not [t for t in worker.READ_ONLY_TOOLS if t.startswith("github")]


# ── The launcher ────────────────────────────────────────────────

def test_the_launcher_decrypts_only_that_servers_secrets_into_its_environment(fresh, monkeypatch):
    seeded()
    monkeypatch.setenv(vault.VAR, "should-not-pass")
    monkeypatch.setenv(GH_VAR, "")  # as the CLI's environment has it
    monkeypatch.setenv("ANTHROPIC_API_KEY", "should-not-pass")
    monkeypatch.setattr(mcp_launch.host, "env_keys", lambda: {"ANTHROPIC_API_KEY"})  # as if in .env
    argv, env = mcp_launch.command(mcp_servers.get("gitlab"), read_only=False)
    assert argv == ["/opt/bin/npx", "-y", "@zereight/mcp-gitlab@2.1.69"] and GL_TOKEN not in argv
    assert env[GL_VAR] == GL_TOKEN and env["GITLAB_PERMISSION_MODE"] == "modify"
    assert vault.VAR not in env and env[GH_VAR] == "" and "ANTHROPIC_API_KEY" not in env


def test_the_launcher_starts_with_every_setting_blank_as_task_sessions_give_it():
    names = set(re.findall(r'os\.environ\.get\("(\w+)"', (config.ROOT / "jarvis/core/config.py").read_text()))
    said = python("from jarvis.integrations import mcp_launch; mcp_launch.main([])", **dict.fromkeys(names, ""))
    assert "Traceback" not in said.stderr and "Starts one MCP server" in said.stderr


def test_the_launcher_adds_read_only_arguments_and_settings(fresh):
    seeded()
    argv, _ = mcp_launch.command(mcp_servers.get("github"), read_only=True)
    _, env = mcp_launch.command(mcp_servers.get("gitlab"), read_only=True)
    assert argv[-1] == "--read-only" and env["GITLAB_PERMISSION_MODE"] == "readonly"


def test_the_launcher_relays_the_server_and_refuses_one_thats_off(fresh, monkeypatch):
    seeded(gitlab_on=False)
    relayed, detached = [], []

    async def serve(server, wait, how):
        relayed.append((server.name, wait, how[0]))
        return 0

    monkeypatch.setattr(mcp_relay, "serve", serve)
    monkeypatch.setattr(mcp_relay, "detach", lambda: detached.append(True))
    for flags in ([], ["--wait=20"]):
        with pytest.raises(SystemExit) as ended:
            mcp_launch.main(["github", *flags])
        assert ended.value.code == 0
    assert relayed == [("github", 0.0, ["/opt/bin/github-mcp-server", "stdio", "--toolsets=repos,pull_requests"]),
                       ("github", 20.0, ["/opt/bin/github-mcp-server", "stdio", "--toolsets=repos,pull_requests"])]
    assert detached == [True]  # only a session whose calls may carry on in the background outlives it
    with pytest.raises(SystemExit, match="no enabled MCP server gitlab"):
        mcp_launch.main(["gitlab"])


def test_the_launcher_execs_a_server_listed_for_sorting_with_no_relay(fresh, monkeypatch):
    seeded()
    ran = []
    monkeypatch.setattr(mcp_launch.os, "execvpe", lambda program, argv, env: ran.append(argv))
    monkeypatch.setattr(mcp_relay, "serve", lambda *a: pytest.fail("relayed"))
    mcp_launch.main(["github", "--probe"])  # its own markings, for sorting its tools, unchanged
    assert ran[0][0] == "/opt/bin/github-mcp-server"


def test_the_launcher_says_when_a_secret_wont_open(fresh, monkeypatch):
    seeded(gitlab_on=False)
    monkeypatch.setattr(config, "MASTER_KEY", vault.new_key())
    with pytest.raises(SystemExit, match="isn't the key"):
        mcp_launch.main(["github"])


# ── The command ─────────────────────────────────────────────────

def test_the_command_adds_lists_and_changes_a_server(fresh, capsys):
    assert mcp_servers.main(["add", "acme", "--label", "Acme", "--command", "npx", "--arg=-y", "--arg", "acme-mcp@1.0.0",
                             "--env", "ACME_URL=https://acme.example", "--secret-env", "ACME_TOKEN=acme.token",
                             "--research"]) == 0
    mcp_servers.main(["tools", "acme", "--read", "find", "--write", "post=post {text} to {channel}", "--block", "nuke"])
    server = mcp_servers.get("acme")
    assert server.args == ["-y", "acme-mcp@1.0.0"] and server.for_research and server.env == {"ACME_URL": "https://acme.example"}
    assert server.tools == {"find": Tool("read"), "post": Tool("write", "post {text} to {channel}"),
                            "nuke": Tool("blocked")}
    mcp_servers.main(["disable", "acme"])
    assert not mcp_servers.get("acme").enabled
    mcp_servers.main([])
    out = capsys.readouterr().out
    assert "acme  Acme  [disabled, stdio, research]" in out and "ACME_TOKEN (not set)" in out


def test_set_secret_reads_stdin_never_an_argument(fresh, monkeypatch, capsys):
    mcp_servers.main(["add", "acme", "--label", "Acme", "--command", "acme-mcp", "--secret-env", "ACME_TOKEN=acme.token"])
    monkeypatch.setattr(sys, "stdin", type("In", (), {"isatty": lambda self: False, "read": lambda self: "s3cret\n"})())
    assert mcp_servers.main(["set-secret", "acme"]) == 0
    assert vault.get("acme.token") == "s3cret" and "s3cret" not in capsys.readouterr().out
    mcp_servers.main(["clear-secret", "acme"])
    assert not vault.has("acme.token")


def test_removing_a_server_forgets_the_secrets_only_it_used(fresh):
    seeded()
    mcp_servers.save(Server("mirror", "Mirror", command="x", secret_env={"T": "github.token"}), new=True)
    assert mcp_servers.remove("gitlab") == ["gitlab.token"]
    assert mcp_servers.remove("github") == [] and vault.has("github.token")  # the mirror still uses it


def test_refusals_come_back_as_a_message(fresh):
    with pytest.raises(SystemExit, match="pin its version"):
        mcp_servers.main(["add", "acme", "--label", "Acme", "--command", "npx", "--arg", "acme-mcp"])
    with pytest.raises(SystemExit, match="no nope server"):
        mcp_servers.main(["enable", "nope"])
