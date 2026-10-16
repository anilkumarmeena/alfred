"""Tests for MCP connections: registry, HTTP bridge, OAuth sign-in, voice draft/confirm, panel actions (no network)."""

import asyncio
import base64
import hashlib
import io
import json
import time
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from ai_armory.toolsets.google import drafts
from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny
from test_mcp_servers import LISTED, fresh  # noqa: F401  (fixture)

from jarvis import panel, tools
from jarvis.brain import cli
from jarvis.core import vault
from jarvis.integrations import mcp_bridge, mcp_oauth, mcp_registry, mcp_servers
from jarvis.integrations.mcp_servers import Server, Tool
from jarvis.safety import draft_gate
from jarvis.state import activity
from jarvis.tools import connections

# Registry entries (GitHub's is its real entry, trimmed)
GITHUB = {
    "name": "io.github.github/github-mcp-server", "title": "GitHub", "version": "2.0.2",
    "description": "Connect AI assistants to GitHub.", "repository": {"url": "https://github.com/github/github-mcp-server"},
    "packages": [{"registryType": "oci", "identifier": "ghcr.io/github/github-mcp-server:2.0.2",
                  "transport": {"type": "stdio"}, "runtimeArguments": [
                      {"type": "named", "name": "-p", "value": "127.0.0.1:8085:8085"},
                      {"type": "named", "name": "-e", "value": "GITHUB_OAUTH_CALLBACK_PORT=8085"},
                      {"type": "named", "name": "-e", "value": "GITHUB_PERSONAL_ACCESS_TOKEN={token}",
                       "variables": {"token": {"format": "string", "isSecret": True}}}]}],
    "remotes": [{"type": "streamable-http", "url": "https://api.githubcopilot.com/mcp/",
                 "headers": [{"name": "Authorization", "isSecret": True, "description": "PAT"}]}],
}
NOTES = {
    "name": "com.example/notes-mcp", "title": "Notes", "version": "1.4.0", "description": "Your notes.",
    "packages": [{"registryType": "npm", "identifier": "@example/notes-mcp", "version": "1.4.0",
                  "transport": {"type": "stdio"},
                  "environmentVariables": [
                      {"name": "NOTES_TOKEN", "isSecret": True, "isRequired": True, "description": "API token"},
                      {"name": "NOTES_REGION", "isRequired": True, "choices": ["eu", "us"]},
                      {"name": "NOTES_DEBUG", "default": "0"}],
                  "packageArguments": [{"type": "named", "name": "--verbose"}]}],
}
ODD = {
    "name": "io.github.someone/odd", "version": "0.1.0",
    "remotes": [{"type": "sse", "url": "https://odd.example/sse"}],
    "packages": [{"registryType": "mcpb", "identifier": "https://odd.example/odd.mcpb"},
                 {"registryType": "npm", "identifier": "odd-mcp", "version": "0.1.0",
                  "packageArguments": [{"type": "named", "name": "--key", "isSecret": True, "isRequired": True}]}],
}


@pytest.fixture
def runners(monkeypatch):
    monkeypatch.setattr(mcp_registry, "which", lambda name: f"/opt/bin/{name}")


def serving(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# ── The registry ────────────────────────────────────────────────

def test_the_ways_to_run_one_are_hosted_first_and_those_jarvis_cant_say_why(runners):
    can, cant = mcp_registry.ways(GITHUB)
    assert [w.kind for w in can] == ["remote", "oci"] and not cant
    assert can[0].said() == "hosted at api.githubcopilot.com" and can[0].needs[0].secret
    can, cant = mcp_registry.ways(ODD)
    assert can == [] and len(cant) == 3
    problems = " ".join(w.problem for w in cant)
    assert "sse" in problems and "mcpb" in problems and "on the command line" in problems


def test_a_hosted_server_keeps_its_key_in_the_vault_sent_as_a_bearer_header(runners):
    server, still = mcp_registry.build(GITHUB, mcp_registry.ways(GITHUB)[0][0], "github", "GitHub")
    assert still == [] and server.transport == "http" and server.url == "https://api.githubcopilot.com/mcp/"
    assert server.headers == {"Authorization": "Bearer {AUTHORIZATION}"}
    assert server.secret_env == {"AUTHORIZATION": "github.authorization"}
    assert (server.source, server.verified, server.version) == ("io.github.github/github-mcp-server", True, "2.0.2")
    assert not server.enabled and not server.reviewed and server.for_research
    assert mcp_servers.problems(server) == []


def test_a_container_gets_its_token_through_the_environment_never_its_command_line(runners):
    server, still = mcp_registry.build(GITHUB, mcp_registry.ways(GITHUB)[0][1], "github", "GitHub")
    assert still == [] and server.command == "docker"
    assert server.args == ["run", "-i", "--rm", "-p", "127.0.0.1:8085:8085", "-e", "GITHUB_OAUTH_CALLBACK_PORT",
                           "-e", "GITHUB_PERSONAL_ACCESS_TOKEN", "ghcr.io/github/github-mcp-server:2.0.2"]
    assert server.env == {"GITHUB_OAUTH_CALLBACK_PORT": "8085"}
    assert server.secret_env == {"GITHUB_PERSONAL_ACCESS_TOKEN": "github.github_personal_access_token"}
    unpinned = json.loads(json.dumps(GITHUB))
    unpinned["packages"][0]["identifier"] = "ghcr.io/github/github-mcp-server:latest"
    with pytest.raises(ValueError, match="isn't pinned"):
        mcp_registry.build(unpinned, mcp_registry.ways(unpinned)[0][1], "github", "GitHub")


def test_a_package_takes_its_settings_said_and_its_keys_apart(runners):
    way = mcp_registry.ways(NOTES)[0][0]
    server, still = mcp_registry.build(NOTES, way, "notes", "Notes")
    assert [n.var for n in still] == ["NOTES_REGION"]  # asked for out loud, so not secret
    server, still = mcp_registry.build(NOTES, way, "notes", "Notes", {"NOTES_REGION": "eu"})
    assert still == [] and (server.command, server.args) == ("npx", ["-y", "@example/notes-mcp@1.4.0"])
    assert server.env == {"NOTES_REGION": "eu", "NOTES_DEBUG": "0"}
    assert server.secret_env == {"NOTES_TOKEN": "notes.notes_token"} and mcp_servers.problems(server) == []
    with pytest.raises(ValueError, match="one of eu, us"):
        mcp_registry.build(NOTES, way, "notes", "Notes", {"NOTES_REGION": "mars"})


def test_names_and_labels_come_from_the_entry():
    assert mcp_registry.label_of(GITHUB) == "GitHub" and mcp_registry.label_of(NOTES) == "Notes"
    assert mcp_registry.label_of({"name": "io.github.acme/linear-mcp-server"}) == "Linear"
    assert mcp_registry.slug("GitHub") == "github" and mcp_registry.slug("GitHub", ["github"]) == "github_2"
    assert mcp_registry.slug("Shell") == "shell_mcp" and mcp_registry.slug("2do") == "s_2do"


def test_a_search_drops_retired_entries_and_puts_the_best_match_first(runners):
    def answer(request: httpx.Request) -> httpx.Response:
        assert request.url.params["search"] == "github" and request.url.params["version"] == "latest"
        items = [{"server": {"name": "ai.smithery/someone-github-tools", "remotes": [
                     {"type": "streamable-http", "url": "https://x.example/mcp"}]}},
                 {"server": {"name": "io.github.old/github", "version": "1"},
                  "_meta": {"io.modelcontextprotocol.registry/official": {"status": "deprecated"}}},
                 {"server": GITHUB}]
        return httpx.Response(200, json={"servers": items})

    found = asyncio.run(mcp_registry.search("github", serving(answer)))
    assert [s["name"] for s in found] == ["io.github.github/github-mcp-server", "ai.smithery/someone-github-tools"]


def test_a_server_found_on_the_web_is_unverified_with_where_it_came_from():
    with pytest.raises(ValueError, match="where it was found"):
        mcp_registry.unlisted("acme", "Acme", "", package="acme-mcp@1.0.0")
    server = mcp_registry.unlisted("acme", "Acme", "https://blog.example/acme-mcp", package="acme-mcp@1.0.0",
                                   secrets=["ACME_KEY"])
    assert not server.verified and server.source == "https://blog.example/acme-mcp"
    assert (server.command, server.args, server.secret_env) == ("npx", ["-y", "acme-mcp@1.0.0"], {"ACME_KEY": "acme.acme_key"})
    hosted = mcp_registry.unlisted("acme", "Acme", "https://blog.example/x", url="https://mcp.acme.example/",
                                   secrets=["ACME_KEY"])
    assert hosted.headers == {"Authorization": "Bearer {ACME_KEY}"} and mcp_servers.problems(hosted) == []
    assert "pin its version" in " ".join(mcp_servers.problems(
        mcp_registry.unlisted("acme", "Acme", "https://blog.example/x", package="acme-mcp")))


# ── The bridge ──────────────────────────────────────────────────

def bridged(server: Server, lines: list[dict], handler) -> list[dict]:
    """Lines the bridge writes back for the CLI's `lines`; `handler` answers the server side."""
    out = io.StringIO()

    async def go():
        reader = asyncio.StreamReader()
        reader.feed_data("".join(json.dumps(m) + "\n" for m in lines).encode())
        reader.feed_eof()
        await mcp_bridge.serve(server, reader, out, httpx.MockTransport(handler))

    asyncio.run(go())
    return [json.loads(line) for line in out.getvalue().splitlines()]


HELLO = {"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}}
LIST = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}


def notes_http() -> Server:
    return Server("notes", "Notes", transport="http", url="https://mcp.notes.example/mcp",
                  headers={"Authorization": "Bearer {NOTES_KEY}"}, secret_env={"NOTES_KEY": "notes.notes_key"})


def test_the_bridge_adds_the_key_carries_the_session_and_reads_event_streams(fresh):
    vault.ensure()
    vault.put("notes.notes_key", "Bearer k-123")  # pasted with its Bearer prefix
    seen = []

    def answer(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.method == "DELETE":
            return httpx.Response(200)
        body = json.loads(request.content)
        if body.get("method") == "initialize":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 0, "result": {"protocolVersion": "2025-06-18"}},
                                  headers={"Mcp-Session-Id": "s-1"})
        if "id" not in body:
            return httpx.Response(202)
        events = ('event: message\ndata: {"jsonrpc": "2.0", "method": "notifications/progress"}\n\n'
                  'data: {"jsonrpc": "2.0", "id": 1,\ndata:  "result": {"tools": []}}\n\n')
        return httpx.Response(200, text=events, headers={"Content-Type": "text/event-stream"})

    got = bridged(notes_http(), [HELLO, {"jsonrpc": "2.0", "method": "notifications/initialized"}, LIST], answer)
    assert got == [{"jsonrpc": "2.0", "id": 0, "result": {"protocolVersion": "2025-06-18"}},
                   {"jsonrpc": "2.0", "method": "notifications/progress"},
                   {"jsonrpc": "2.0", "id": 1, "result": {"tools": []}}]
    assert all(r.headers["authorization"] == "Bearer k-123" for r in seen)
    assert "mcp-session-id" not in seen[0].headers
    assert all(r.headers["mcp-session-id"] == "s-1" and r.headers["mcp-protocol-version"] == "2025-06-18"
               for r in seen[1:])
    assert seen[-1].method == "DELETE"  # the session ends when the CLI's input does


def test_the_bridge_answers_a_request_the_server_refused_so_the_cli_isnt_left_waiting(fresh):
    vault.ensure()
    vault.put("notes.notes_key", "k-123")
    got = bridged(notes_http(), [LIST], lambda r: httpx.Response(401))
    assert got[0]["id"] == 1 and "refused JARVIS's key" in got[0]["error"]["message"]
    got = bridged(notes_http(), [LIST], lambda r: httpx.Response(503))
    assert "answered 503" in got[0]["error"]["message"]


def test_the_bridge_refreshes_a_sign_in_the_server_refused_once(fresh):
    wiki = Server("wiki", "Wiki", transport="http", url="https://mcp.wiki.example/mcp", sign_in=True)
    vault.ensure()
    vault.put("wiki.oauth", json.dumps({"access_token": "old", "refresh_token": "r-1", "expires_at": time.time() + 3600,
                                        "client_id": "c-1", "token_endpoint": "https://auth.wiki.example/token",
                                        "resource": wiki.url}))

    def answer(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/token":
            form = parse_qs(request.content.decode())
            assert form["grant_type"] == ["refresh_token"] and form["refresh_token"] == ["r-1"]
            return httpx.Response(200, json={"access_token": "new", "expires_in": 3600})
        if request.headers["authorization"] != "Bearer new":
            return httpx.Response(401)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {"tools": []}})

    assert bridged(wiki, [LIST], answer) == [{"jsonrpc": "2.0", "id": 1, "result": {"tools": []}}]
    kept = json.loads(vault.get("wiki.oauth"))
    assert kept["access_token"] == "new" and kept["refresh_token"] == "r-1"  # refresh token kept when not re-sent


# ── Signing in ──────────────────────────────────────────────────

class Auth:
    """OAuth-protected server, its authorization server, and the user's browser."""

    def __init__(self, registers: bool = True) -> None:
        self.registers = registers
        self.challenge = ""

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == "https://mcp.wiki.example/mcp":
            return httpx.Response(401, headers={"WWW-Authenticate": 'Bearer resource_metadata='
                                                '"https://mcp.wiki.example/.well-known/oauth-protected-resource/mcp"'})
        if url == "https://mcp.wiki.example/.well-known/oauth-protected-resource/mcp":
            return httpx.Response(200, json={"resource": "https://mcp.wiki.example/mcp",
                                             "authorization_servers": ["https://auth.wiki.example"]})
        if url == "https://auth.wiki.example/.well-known/oauth-authorization-server":
            return httpx.Response(200, json={
                "authorization_endpoint": "https://auth.wiki.example/authorize",
                "token_endpoint": "https://auth.wiki.example/token",
                **({"registration_endpoint": "https://auth.wiki.example/register"} if self.registers else {})})
        if url == "https://auth.wiki.example/register":
            assert json.loads(request.content)["redirect_uris"] == [mcp_oauth.REDIRECT]
            return httpx.Response(201, json={"client_id": "c-1"})
        if url == "https://auth.wiki.example/token":
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            if form["grant_type"] == "authorization_code":
                digest = hashlib.sha256(form["code_verifier"].encode()).digest()
                assert base64.urlsafe_b64encode(digest).decode().rstrip("=") == self.challenge
                assert form["code"] == "the-code" and form["resource"] == "https://mcp.wiki.example/mcp"
                return httpx.Response(200, json={"access_token": "a-1", "refresh_token": "r-1", "expires_in": 3600})
            return httpx.Response(200, json={"access_token": "a-2", "expires_in": 3600})
        return httpx.Response(404)

    async def browser(self, url: str, back_to: str, state: str | None = None) -> str:
        """Sign in at the authorize endpoint; redirect back with a code."""
        query = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
        assert url.startswith("https://auth.wiki.example/authorize?") and back_to == mcp_oauth.REDIRECT
        assert query["code_challenge_method"] == "S256" and query["client_id"] == "c-1"
        self.challenge = query["code_challenge"]
        return f"{back_to}?code=the-code&state={state or query['state']}"


WIKI = Server("wiki", "Wiki", transport="http", url="https://mcp.wiki.example/mcp", sign_in=True)


def test_signing_in_registers_jarvis_and_keeps_the_tokens_in_the_vault(fresh):
    auth = Auth()
    assert asyncio.run(mcp_oauth.needs_sign_in(WIKI.url, serving(auth)))
    said = asyncio.run(mcp_oauth.sign_in(WIKI, auth.browser, serving(auth)))
    assert said == "Signed in to Wiki."
    kept = json.loads(vault.get("wiki.oauth"))
    assert (kept["access_token"], kept["refresh_token"], kept["client_id"]) == ("a-1", "r-1", "c-1")
    assert asyncio.run(mcp_oauth.token(WIKI)) == "a-1"
    assert asyncio.run(mcp_oauth.token(WIKI, refresh=True, client=serving(auth))) == "a-2"


def test_a_sign_in_that_comes_back_wrong_or_cant_register_isnt_kept(fresh):
    auth = Auth()
    with pytest.raises(mcp_oauth.SignInError, match="JARVIS's own check"):
        asyncio.run(mcp_oauth.sign_in(WIKI, lambda url, back: auth.browser(url, back, "forged"), serving(auth)))
    with pytest.raises(mcp_oauth.SignInError, match="takes a key instead"):
        asyncio.run(mcp_oauth.sign_in(WIKI, Auth(registers=False).browser, serving(Auth(registers=False))))
    assert not vault.has("wiki.oauth")
    assert not asyncio.run(mcp_oauth.needs_sign_in(WIKI.url, serving(lambda r: httpx.Response(200, json={}))))


# ── By voice ────────────────────────────────────────────────────

class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def gate(monkeypatch, fresh, runners):
    monkeypatch.setattr(drafts, "_clock", Clock())
    monkeypatch.setattr(drafts, "_DRAFTS", {})
    entries = {"com.example/notes-mcp": NOTES, "io.github.github/github-mcp-server": GITHUB}

    async def fetch(name, client=None):
        return entries[name]

    monkeypatch.setattr(mcp_registry, "fetch", fetch)
    monkeypatch.setattr(mcp_oauth, "needs_sign_in", lambda url: asyncio.sleep(0, False))
    monkeypatch.setattr(mcp_servers, "offered", lambda server, read_only=False, timeout=0: LISTED)
    return draft_gate.DraftGate(Clock())


def run(name: str, args: dict):
    content, error = asyncio.run(tools.run(name, args))
    return (json.loads(content) if content.startswith(("{", "[")) else content), error


def confirm(gate, draft_id: str) -> tuple[str, str]:
    """Hold connection_confirm for the question, then say yes; returns (what was asked, what was done)."""
    gate.new_turn("connect it")
    assert isinstance(asyncio.run(gate("mcp__jarvis__connection_confirm", {"draft_id": draft_id}, None)),
                      PermissionResultDeny)
    asked = gate.question("")
    assert gate.new_turn("Yes.") == [draft_gate.PREFIX + draft_id]
    assert isinstance(asyncio.run(gate("mcp__jarvis__connection_confirm", {"draft_id": draft_id}, None)),
                      PermissionResultAllow)
    done, error = run("connection_confirm", {"draft_id": draft_id})
    assert not error, done
    return asked, done


def test_only_confirming_waits_for_a_yes_and_the_brain_is_told_how():
    found = {t.name: t for t in tools.all_tools() if t.name.startswith("connection")}
    assert set(found) == {"connections_list", "connection_find", "connection_add", "connection_sign_in",
                          "connection_review", "connection_enable", "connection_disable", "connection_update",
                          "connection_remove", "connection_confirm"}
    assert [n for n, t in found.items() if t.gated] == ["connection_confirm"]
    assert "mcp__jarvis__connection_confirm" in draft_gate.gated()
    brain = cli.CliBrain()
    assert "Connections (tools from MCP servers)" in cli._options(brain.shell, brain.jobs).system_prompt


def test_connecting_from_the_registry_then_sorting_its_tools_switches_it_on(gate):
    said, error = run("connection_add", {"registry_name": "com.example/notes-mcp"})
    assert not error and said["needs_settings"][0]["name"] == "NOTES_REGION" and "draft_id" not in said
    draft, _ = run("connection_add", {"registry_name": "com.example/notes-mcp", "settings": {"NOTES_REGION": "eu"}})
    assert "from the official MCP registry (com.example/notes-mcp, version 1.4.0" in draft["summary"]
    assert "NOTES_TOKEN" in draft["summary"] and "control panel" in draft["summary"]
    assert mcp_servers.get("notes") is None  # nothing until the yes
    asked, done = confirm(gate, draft["draft_id"])
    assert asked.startswith("Connect Notes") and "off for now" in done and "NOTES_TOKEN" in done
    notes = mcp_servers.get("notes")
    assert not notes.enabled and not notes.reviewed and mcp_servers.for_voice() == []

    _, error = run("connection_review", {"name": "notes"})
    assert error  # its key isn't set yet
    vault.put("notes.notes_token", "n-1")  # as the control panel does
    review, error = run("connection_review", {"name": "Notes"})
    assert not error and "Reading, which run straight away: search." in review["summary"]
    assert "Changing, each waiting for your yes: create page, delete page and mystery." in review["summary"]
    assert "delete page and mystery can delete or overwrite things" in review["summary"]
    assert "Saying yes switches it on" in review["summary"]
    _, done = confirm(gate, review["draft_id"])
    notes = mcp_servers.get("notes")
    assert notes.enabled and notes.reviewed and notes.tools["search"] == Tool("read")
    assert notes.tools["create_page"] == Tool("write", "create a page") and mcp_servers.for_voice() == [notes]
    listed, _ = run("connections_list", {})
    assert listed[0]["state"] == "on" and listed[0]["tools"] == {"read": 1, "write": 3}


def test_the_users_choices_are_said_back_and_unknown_tools_refused(gate):
    mcp_servers.save(Server("notes", "Notes", command="npx", args=["-y", "n@1.0.0"], reviewed=False), new=True)
    review, _ = run("connection_review", {"name": "notes", "reading": ["mystery"], "off": ["delete_page"]})
    assert "Switched off: delete page." in review["summary"] and "except mystery and delete page, as you asked" in \
        review["summary"]
    said, error = run("connection_review", {"name": "notes", "off": ["nope"]})
    assert error and "no tool nope" in said


def test_a_reading_tool_becomes_its_sign_in_check_and_is_kept_when_sorted_again(gate):
    mcp_servers.save(Server("notes", "Notes", command="npx", args=["-y", "n@1.0.0"], reviewed=False), new=True)
    said, error = run("connection_review", {"name": "notes", "check": "create_page"})
    assert error and "can't be the sign-in check" in said
    review, _ = run("connection_review", {"name": "notes", "check": "search"})
    assert "Every hour I'll call search to make sure its sign-in still works." in review["summary"]
    confirm(gate, review["draft_id"])
    assert mcp_servers.get("notes").check_tool == "search"
    listed, _ = run("connections_list", {})
    assert listed[0]["sign_in_check"] == "search"
    again, _ = run("connection_review", {"name": "notes"})
    assert again["same"] and again["check"] == "search"
    moved, _ = run("connection_review", {"name": "notes", "changing": ["search"]})
    assert "Every hour" not in moved["summary"]  # no longer a reading tool, so no longer the check
    confirm(gate, moved["draft_id"])
    assert mcp_servers.get("notes").check_tool == ""


def test_keys_are_never_taken_by_voice(gate):
    said, error = run("connection_add", {"registry_name": "com.example/notes-mcp",
                                         "settings": {"NOTES_REGION": "eu", "NOTES_TOKEN": "n-1"}})
    assert error and "never said" in said and mcp_servers.get("notes") is None


def test_a_server_from_the_web_is_said_to_be_unverified_with_its_source(gate):
    draft, _ = run("connection_add", {"source": "https://blog.example/acme", "package": "acme-mcp@1.0.0",
                                      "label": "Acme", "keys": ["ACME_KEY"]})
    assert "unverified: it isn't in the official MCP registry; it was found on the web at https://blog.example/acme" \
        in draft["summary"]
    confirm(gate, draft["draft_id"])
    acme = mcp_servers.get("acme")
    assert not acme.verified and acme.source == "https://blog.example/acme" and acme.secret_env == {"ACME_KEY": "acme.acme_key"}


def test_a_hosted_server_that_asks_for_a_sign_in_is_said_so(gate, monkeypatch):
    monkeypatch.setattr(mcp_oauth, "needs_sign_in", lambda url: asyncio.sleep(0, True))
    draft, _ = run("connection_add", {"source": "https://blog.example/wiki", "url": "https://mcp.wiki.example/mcp",
                                      "label": "Wiki"})
    assert "sign in, which I'll open in my browser" in draft["summary"] and "nothing is installed" in draft["summary"]
    _, done = confirm(gate, draft["draft_id"])
    assert mcp_servers.get("wiki").sign_in and "connection_sign_in" in done


def test_signing_in_by_voice_is_noted_when_done(gate, monkeypatch):
    mcp_servers.save(WIKI, new=True)
    monkeypatch.setattr(mcp_oauth, "sign_in", lambda server, opener: asyncio.sleep(0, "Signed in to Wiki."))
    asyncio.run(connections._signing_in(mcp_servers.get("wiki")))
    assert activity.recent()[-1]["text"].startswith("Signed in to Wiki.")
    said, error = run("connection_sign_in", {"name": "notes_nope"})
    assert error and "no connection called" in said


def test_switching_off_needs_no_yes_and_on_only_once_sorted(gate):
    mcp_servers.save(Server("notes", "Notes", command="npx", args=["-y", "n@1.0.0"], reviewed=False), new=True)
    said, error = run("connection_enable", {"name": "notes"})
    assert error and "haven't been sorted" in said
    mcp_servers.save(Server("acme", "Acme", command="acme-mcp", tools={"look": Tool("read")}), new=True)
    assert "Switched Acme off" in run("connection_disable", {"name": "Acme"})[0] and not mcp_servers.get("acme").enabled
    assert "Switched Acme on" in run("connection_enable", {"name": "acme"})[0] and mcp_servers.get("acme").enabled


def test_removing_says_what_goes_with_it_and_waits_for_the_yes(gate):
    mcp_servers.save(Server("notes", "Notes", command="npx", args=["-y", "n@1.0.0"], secret_env={"T": "notes.t"},
                            for_research=True, source="com.example/notes-mcp", verified=True,
                            tools={"search": Tool("read"), "create_page": Tool("write")}), new=True)
    vault.ensure()
    vault.put("notes.t", "n-1")
    draft, _ = run("connection_remove", {"name": "notes"})
    assert draft["summary"].startswith("Remove Notes: its 2 tools go from my conversations and from background "
                                       "research tasks. Its key (T) is deleted.")
    assert mcp_servers.get("notes")  # still there until the yes
    _, done = confirm(gate, draft["draft_id"])
    assert done == "Removed Notes and its 1 secret." and mcp_servers.get("notes") is None and not vault.has("notes.t")


def test_updating_keeps_its_tools_and_state_and_says_the_versions(gate, monkeypatch):
    server, _ = mcp_registry.build(NOTES, mcp_registry.ways(NOTES)[0][0], "notes", "Notes", {"NOTES_REGION": "eu"})
    mcp_servers.save(Server(**{**server.__dict__, "version": "1.3.0", "enabled": True, "reviewed": True,
                               "tools": {"search": Tool("read")}}), new=True)
    draft, _ = run("connection_update", {"name": "notes"})
    assert "Update Notes from version 1.3.0 to 1.4.0" in draft["summary"]
    confirm(gate, draft["draft_id"])
    notes = mcp_servers.get("notes")
    assert notes.version == "1.4.0" and "n@" not in notes.args[1] and notes.args[1] == "@example/notes-mcp@1.4.0"
    assert notes.enabled and notes.reviewed and notes.tools == {"search": Tool("read")}
    assert run("connection_update", {"name": "notes"})[0] == "Notes is up to date, at version 1.4.0."


def test_finding_falls_back_to_the_web_when_the_registry_has_nothing(gate, monkeypatch):
    monkeypatch.setattr(mcp_registry, "search", lambda query: asyncio.sleep(0, []))
    said, _ = run("connection_find", {"query": "acme"})
    assert said["found"] == [] and "WebSearch" in said["next"] and "unverified" in said["next"]
    monkeypatch.setattr(mcp_registry, "search", lambda query: asyncio.sleep(0, [GITHUB]))
    said, _ = run("connection_find", {"query": "github"})
    entry = said["found"][0]
    assert entry["registry_name"] == GITHUB["name"] and entry["publisher"] == "io.github.github"
    assert entry["ways"][0]["runs"] == "hosted at api.githubcopilot.com"


# ── The control panel ───────────────────────────────────────────

def call(name: str, body: dict | None = None):
    return asyncio.run(panel.call(name, body or {}))


def test_the_panel_takes_a_key_but_never_shows_one(fresh):
    mcp_servers.save(notes_http(), new=True)
    shown = call("servers")[0]
    assert shown["secrets"] == [{"var": "NOTES_KEY", "set": False}] and "control panel" in shown["problem"]
    after = call("server_secret", {"name": "notes", "var": "NOTES_KEY", "value": "  k-123 \n"})
    assert vault.get("notes.notes_key") == "k-123" and "k-123" not in json.dumps(after)
    assert after[0]["secrets"] == [{"var": "NOTES_KEY", "set": True}]
    for bad in ({"var": "OTHER", "value": "x"}, {"var": "NOTES_KEY", "value": ""}):
        with pytest.raises(ValueError):
            call("server_secret", {"name": "notes"} | bad)
    call("server_secret_clear", {"name": "notes", "var": "NOTES_KEY"})
    assert not vault.has("notes.notes_key")


def gitlab_with_a_key(url: str = "https://gitlab.com/api/v4") -> None:
    mcp_servers.save(Server("gitlab", "GitLab", command="npx", args=["-y", "@zereight/mcp-gitlab@2.1.69"],
                            env={"GITLAB_API_URL": url, "GITLAB_PERMISSION_MODE": "modify"},
                            secret_env={"GITLAB_PERSONAL_ACCESS_TOKEN": "gitlab.token"},
                            tools={"whoami": Tool("read")}), new=True)
    vault.ensure()
    vault.put("gitlab.token", "glpat-not-a-real-token")


def test_the_panel_shows_and_changes_the_server_a_connection_reaches(fresh):
    gitlab_with_a_key()
    assert call("servers")[0]["addresses"] == [{"var": "GITLAB_API_URL", "value": "https://gitlab.com/api/v4"}]
    said = call("server_address", {"name": "gitlab", "var": "GITLAB_API_URL", "value": " https://gitlab.com/api/v4/ "})
    assert said["forgot"] == [] and vault.has("gitlab.token")  # the same host keeps its key
    assert mcp_servers.get("gitlab").env["GITLAB_API_URL"] == "https://gitlab.com/api/v4/"
    assert said["servers"][0]["addresses"][0]["value"] == "https://gitlab.com/api/v4/"


def test_moving_a_connection_to_another_host_forgets_its_keys(fresh):
    gitlab_with_a_key()
    said = call("server_address", {"name": "gitlab", "var": "GITLAB_API_URL", "value": "https://git.example.com/api/v4"})
    assert said["forgot"] == ["GITLAB_PERSONAL_ACCESS_TOKEN"] and not vault.has("gitlab.token")
    assert said["servers"][0]["secrets"] == [{"var": "GITLAB_PERSONAL_ACCESS_TOKEN", "set": False}]
    assert "glpat" not in json.dumps(said)
    server = mcp_servers.get("gitlab")
    assert server.env == {"GITLAB_API_URL": "https://git.example.com/api/v4", "GITLAB_PERMISSION_MODE": "modify"}
    vault.put("gitlab.token", "glpat-not-a-real-token")
    assert call("server_address", {"name": "gitlab", "var": "GITLAB_API_URL", "value": ""})["forgot"] == ["GITLAB_PERSONAL_ACCESS_TOKEN"]
    assert "GITLAB_API_URL" not in mcp_servers.get("gitlab").env  # back to the package's default, gitlab.com


@pytest.mark.parametrize("body, why", [
    ({"var": "GITLAB_API_URL", "value": "http://git.example.com"}, "https://"),
    ({"var": "GITLAB_PERMISSION_MODE", "value": "https://git.example.com"}, "no server address"),
    ({"var": "NODE_OPTIONS", "value": "https://git.example.com"}, "no server address"),
])
def test_the_panel_changes_only_a_server_address_and_only_to_https(fresh, body, why):
    gitlab_with_a_key()
    with pytest.raises(ValueError, match=why):
        call("server_address", {"name": "gitlab"} | body)
    assert mcp_servers.get("gitlab").env["GITLAB_API_URL"] == "https://gitlab.com/api/v4" and vault.has("gitlab.token")


def test_the_panel_switches_one_on_only_once_its_tools_are_sorted(fresh):
    mcp_servers.save(Server("notes", "Notes", command="npx", args=["-y", "n@1.0.0"], reviewed=False,
                            source="com.example/notes-mcp", verified=True, version="1.0.0"), new=True)
    shown = call("servers")[0]
    assert (shown["reviewed"], shown["verified"], shown["source"]) == (False, True, "com.example/notes-mcp")
    with pytest.raises(ValueError, match="haven't been sorted"):
        call("server_switch", {"name": "notes", "on": True})
    mcp_servers.save(Server("acme", "Acme", command="acme-mcp", tools={"look": Tool("read")}), new=True)
    shown = call("server_switch", {"name": "acme", "on": False})
    assert not mcp_servers.get("acme").enabled and shown[1]["tool_list"] == [{"name": "look", "access": "read"}]


def test_the_panel_shows_what_removing_takes_before_it_removes(fresh):
    mcp_servers.save(Server("acme", "Acme", command="acme-mcp", tools={"look": Tool("read")}), new=True)
    assert call("server_impact", {"name": "acme"})["impact"].startswith("Remove Acme: its 1 tool goes from my conversations.")
    assert call("server_remove", {"name": "acme"}) == [] and mcp_servers.get("acme") is None
