"""Tests for jarvis.integrations.mcp_launch beyond test_mcp_servers: HTTP relay/probe, read-only, start failures."""

import pytest
from test_mcp_servers import fresh, github, seeded  # noqa: F401  (fixture)

from jarvis.apps import mcp_launch as launcher
from jarvis.auth import vault
from jarvis.integrations import mcp_bridge, mcp_launch, mcp_relay, mcp_servers
from jarvis.integrations.mcp_servers import Server

WIKI = Server("wiki", "Wiki", transport="http", url="https://mcp.wiki.example/mcp", reviewed=True)


@pytest.fixture
def started(monkeypatch):
    """Records launches: ("relay", server, wait, how), ("bridge", server) or ("exec", argv)."""
    ran: list[tuple] = []

    async def relay(server, wait, how, runner=None):
        ran.append(("relay", server.name, wait, how))
        return 0

    async def bridge(server):
        ran.append(("bridge", server.name))
        return 0

    monkeypatch.setattr(mcp_relay, "serve", relay)
    monkeypatch.setattr(mcp_relay, "detach", lambda: ran.append(("detach",)))
    monkeypatch.setattr(mcp_bridge, "serve", bridge)
    monkeypatch.setattr(launcher.os, "execvpe", lambda program, argv, env: ran.append(("exec", argv)))
    return ran


def launch(*argv: str):
    with pytest.raises(SystemExit) as ended:
        launcher.main(list(argv))
    return ended.value.code


def test_an_http_server_is_relayed_with_no_command_and_probed_through_the_bridge(fresh, started):
    mcp_servers.save(WIKI, new=True)
    assert launch("wiki", "--wait=12.5") == 0
    assert launch("wiki", "--probe") == 0
    assert started == [("detach",), ("relay", "wiki", 12.5, None), ("bridge", "wiki")]
    with pytest.raises(ValueError, match="reached through the bridge"):
        mcp_launch.command(WIKI, read_only=False)


def test_read_only_reaches_the_relayed_command(fresh, started):
    seeded(gitlab_on=False)
    assert launch("github", "--read-only") == 0
    [(_, name, wait, (argv, env))] = started
    assert (name, wait, argv[-1]) == ("github", 0.0, "--read-only") and env["GITHUB_PERSONAL_ACCESS_TOKEN"]


def test_a_server_thats_off_or_unsorted_can_still_be_probed_but_not_run(fresh, started):
    server = github()
    server.enabled, server.reviewed = False, False
    mcp_servers.save(server, new=True)
    vault.ensure()
    vault.put("github.token", "ghp_not_a_real_token")
    launcher.main(["github", "--probe"])  # exec takes the process over, so no exit
    assert started == [("exec", ["/opt/bin/github-mcp-server", "stdio", "--toolsets=repos,pull_requests"])]
    assert launch("github") == "There's no enabled MCP server github."


def test_what_stops_a_server_starting_is_said_not_raised(fresh, started, monkeypatch):
    seeded(gitlab_on=False)
    assert "Starts one MCP server" in launch()
    assert launch("nothing") == "There's no enabled MCP server nothing."
    vault.remove("github.token")
    assert "There's no secret github.token" in launch("github")
    monkeypatch.setattr(mcp_launch, "which", lambda name: None)
    assert launch("github") == "github: github-mcp-server isn't installed."
    assert started == []
