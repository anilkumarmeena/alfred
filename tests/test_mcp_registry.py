"""The MCP registry (jarvis.integrations.mcp_registry), beyond tests/test_connections.py: the registry out of reach
or without the server, a search of several words, the ways JARVIS can't run a server and why, and entries built from
URLs, headers and arguments with settings and secrets in them. Every HTTP exchange is answered here.
Run with: make test"""

import asyncio

import httpx
import pytest
from test_connections import GITHUB, runners, serving  # noqa: F401  (runners: the fixture)

from jarvis.integrations import mcp_registry, mcp_servers
from jarvis.integrations.mcp_registry import Way


def entry(**fields) -> dict:
    return {"name": "com.example/thing", "version": "1.0.0"} | fields


# ── The registry ────────────────────────────────────────────────

@pytest.mark.parametrize("answer, said", [
    (lambda r: httpx.Response(404), "has no such server"),
    (lambda r: httpx.Response(503), "answered 503"),
])
def test_the_registry_answering_otherwise_says_so(answer, said):
    with pytest.raises(mcp_registry.RegistryError, match=said):
        asyncio.run(mcp_registry.fetch("com.example/thing", serving(answer)))


def test_the_registry_out_of_reach_says_so_without_the_details():
    def down(request):
        raise httpx.ConnectError("[Errno 8] nodename nor servname provided")

    with pytest.raises(mcp_registry.RegistryError, match=r"can't be reached right now \(ConnectError\)\.$"):
        asyncio.run(mcp_registry.search("notes", serving(down)))


def test_fetch_asks_for_the_latest_version_by_its_whole_name_and_refuses_a_retired_one():
    asked = []

    def answer(request: httpx.Request) -> httpx.Response:
        asked.append(request.url.raw_path.decode())
        status = "deleted" if "old" in str(request.url) else "active"
        return httpx.Response(200, json={"server": GITHUB, "_meta": {
            "io.modelcontextprotocol.registry/official": {"status": status}}})

    assert asyncio.run(mcp_registry.fetch(" io.github.github/github-mcp-server ", serving(answer))) == GITHUB
    assert asked == ["/v0/servers/io.github.github%2Fgithub-mcp-server/versions/latest"]
    with pytest.raises(mcp_registry.RegistryError, match="marks com.example/old as deleted"):
        asyncio.run(mcp_registry.fetch("com.example/old", serving(answer)))


def test_a_search_of_several_words_tries_the_longest_word_next_and_stops_at_a_find(runners):
    asked = []

    def answer(request: httpx.Request) -> httpx.Response:
        asked.append(request.url.params["search"])
        found = [{"server": entry(name="com.notion/notion-mcp", title="Notion")}] if asked[-1] == "notion" else []
        return httpx.Response(200, json={"servers": found})

    found = asyncio.run(mcp_registry.search("my notion pages", serving(answer)))
    assert asked == ["my notion pages", "notion"] and [s["name"] for s in found] == ["com.notion/notion-mcp"]


def test_the_best_match_is_its_own_name_then_its_publishers_then_one_jarvis_can_run(runners):
    named = entry(name="io.github.someone/linear-mcp")
    publisher = entry(name="app.linear/tools")
    mentioned = entry(name="io.github.other/better-linear-sync")
    other = entry(name="io.github.other/sync")
    assert sorted([other, mentioned, publisher, named], key=lambda s: -mcp_registry._score("linear", s)) == \
        [named, publisher, mentioned, other]
    assert mcp_registry._score("linear", entry(name="x.y/z", title="Linear")) > mcp_registry._score("linear", other)
    hosted = entry(name="io.github.other/linear-sync", remotes=[{"type": "streamable-http", "url": "https://x.example"}])
    assert mcp_registry._score("linear", hosted) == mcp_registry._score("linear", mentioned) + 1


# ── The ways to run one ─────────────────────────────────────────

def test_the_ways_jarvis_cant_run_each_say_why(runners, monkeypatch):
    server = entry(remotes=[{"type": "streamable-http", "url": "http://plain.example/mcp"}, "junk"],
                   packages=[{"registryType": "npm", "identifier": "a", "transport": {"type": "streamable-http"}},
                             {"registryType": "pypi", "version": "1"},
                             {"registryType": "npm", "identifier": "b", "runtimeArguments": [
                                 {"type": "named", "name": "--token", "value": "{key}",
                                  "variables": {"key": {"isSecret": True}}}]},
                             {"registryType": "nuget", "identifier": "c"}, "junk"])
    can, cant = mcp_registry.ways(server)
    assert can == [] and [w.problem for w in cant] == [
        "its URL isn't https", "the package serves over HTTP itself, which JARVIS doesn't start",
        "it doesn't name its package", "it takes its key on the command line, where other programs could read it",
        "it's nuget JARVIS can't run"]
    monkeypatch.setattr(mcp_registry, "which", lambda name: None)
    [way] = mcp_registry.ways(entry(packages=[{"registryType": "pypi", "identifier": "notes-mcp"}]))[1]
    assert way.problem == "uvx isn't installed on this Mac"
    assert way.said() == "on this Mac, as a Python package (notes-mcp)"


def test_a_way_says_what_it_needs_briefly():
    need = mcp_registry.Need("REGION", "x" * 300, required=False, default="eu", choices=["eu", "us"])
    way = Way("remote", {"url": "https://mcp.notes.example/v1"}, [need], "")
    assert way.brief(2) == {"way": 2, "runs": "hosted at mcp.notes.example", "needs": [
        {"name": "REGION", "what": "x" * 200, "optional": True, "default": "eu", "choices": ["eu", "us"]}]}
    assert Way("npm", {"identifier": "a"}, problem="no").brief(0) == {
        "way": 0, "runs": "on this Mac, as an npm package (a)", "cant": "no"}


# ── Making the entry ────────────────────────────────────────────

def test_a_hosted_entry_fills_its_url_and_headers_settings_said_secrets_kept_apart(runners):
    found = entry(remotes=[{
        "type": "streamable-http", "url": "https://{region}.notes.example/mcp",
        "variables": {"region": {"description": "Your region", "isRequired": True, "choices": ["eu", "us"]}},
        "headers": [{"name": "X-Api-Key", "value": "{api_key}", "variables": {"api_key": {"isSecret": True}}},
                    {"name": "X-Workspace", "description": "Workspace id", "isRequired": True},
                    {"name": "X-Client", "value": "jarvis", "isRequired": False}]}])
    way = mcp_registry.ways(found)[0][0]
    assert [n.var for n in way.needs] == ["region", "api_key", "X-Workspace", "X-Client"]
    server, still = mcp_registry.build(found, way, "notes", "Notes")
    assert sorted(n.var for n in still) == ["X-Workspace", "region"]
    server, still = mcp_registry.build(found, way, "notes", "Notes", {"region": "eu", "X-Workspace": "w1"})
    assert still == [] and server.url == "https://eu.notes.example/mcp"
    assert server.headers == {"X-Api-Key": "{api_key}", "X-Workspace": "w1", "X-Client": "jarvis"}
    assert server.secret_env == {"api_key": "notes.api_key"} and mcp_servers.problems(server) == []
    with pytest.raises(ValueError, match="JARVIS can't run Notes that way: it's nuget"):
        mcp_registry.build(found, Way("other", {}, problem="it's nuget JARVIS can't run"), "notes", "Notes")


def test_a_python_package_is_pinned_and_takes_its_arguments_as_given(runners):
    found = entry(packages=[{
        "registryType": "pypi", "identifier": "notes-mcp", "version": "2.1.0",
        "environmentVariables": [{"name": "NOTES_KEY", "isSecret": True},  # optional: left out unless asked for
                                 {"name": "NOTES_HOME", "value": "/tmp/notes"}],
        "runtimeArguments": [{"type": "named", "name": "--python", "value": "3.12"}],
        "packageArguments": [{"type": "positional", "value": "serve"},
                             {"type": "named", "name": "--workspace", "isRequired": True, "description": "Workspace"},
                             {"type": "named", "name": "--quiet", "isRequired": False}]}])
    way = mcp_registry.ways(found)[0][0]
    _, still = mcp_registry.build(found, way, "notes", "Notes")
    assert [n.var for n in still] == ["--workspace"]
    server, still = mcp_registry.build(found, way, "notes", "Notes", {"--workspace": "w1", "NOTES_KEY": "on"})
    assert still == [] and server.command == "uvx"
    assert server.args == ["--python", "3.12", "notes-mcp==2.1.0", "serve", "--workspace", "w1"]
    assert server.env == {"NOTES_HOME": "/tmp/notes"} and server.secret_env == {"NOTES_KEY": "notes.notes_key"}


def test_a_secret_an_argument_would_carry_is_refused():
    arg = {"type": "named", "name": "--key", "value": "{k}", "variables": {"k": {"isSecret": True}}}
    with pytest.raises(ValueError, match="on the command line"):
        mcp_registry._arguments([arg], mcp_registry.Building("notes", {}), oci=False)


def test_names_for_secrets_and_variables_are_safe():
    assert mcp_registry._env_var("X-Api-Key") == "X_API_KEY" and mcp_registry._env_var("2fa code") == "V_2FA_CODE"
    assert mcp_registry._env_var("***") == "SECRET"
    assert mcp_registry._secret_name("notes", "Api Key!") == "notes.api_key"
    assert mcp_registry.slug("The Big Notes MCP Server for Teams and Everyone") == "the_big_notes_for_teams_and"
    assert mcp_registry.slug("MCP") == "server" and mcp_registry.slug("jarvis") == "jarvis_mcp"
    assert mcp_registry.label_of({"name": "x/y", "title": "T" * 41}) == "Y"
    assert mcp_registry.label_of({"name": ""}) == "Server"


def test_a_server_found_on_the_web_runs_from_pypi_or_with_several_keys():
    py = mcp_registry.unlisted("notes", "Notes", "https://blog.example/notes", package="notes-mcp==1.0",
                               settings={"NOTES_HOME": 1})
    assert (py.command, py.args, py.env) == ("uvx", ["notes-mcp==1.0"], {"NOTES_HOME": "1"})
    hosted = mcp_registry.unlisted("notes", "Notes", "https://blog.example/notes", url=" https://mcp.notes.example ",
                                   secrets=["api-key", "WORKSPACE_TOKEN"])
    assert hosted.url == "https://mcp.notes.example" and hosted.transport == "http"
    assert hosted.headers == {"Authorization": "Bearer {API_KEY}", "Workspace-Token": "{WORKSPACE_TOKEN}"}
    assert hosted.secret_env == {"API_KEY": "notes.api_key", "WORKSPACE_TOKEN": "notes.workspace_token"}
    with pytest.raises(ValueError, match="through npx \\(npm\\) or uvx"):
        mcp_registry.unlisted("notes", "Notes", "https://blog.example/notes", package="notes", runner="docker")
