"""Tests for alfred.integrations.mcp_launch beyond test_mcp_servers: HTTP relay/probe, read-only, start failures,
npx packages run as node."""

import json
import os

import pytest
from test_mcp_servers import GL_TOKEN, GL_VAR, fresh, github, seeded  # noqa: F401  (fixture)

from alfred.apps import mcp_launch as launcher
from alfred.auth import vault
from alfred.integrations import mcp_bridge, mcp_launch, mcp_relay, mcp_servers
from alfred.integrations.mcp_servers import Server

WIKI = Server("wiki", "Wiki", transport="http", url="https://mcp.wiki.example/mcp", reviewed=True)


@pytest.fixture
def started(monkeypatch):
    """Records launches: ("relay", server, wait, how, read_only), ("bridge", server) or ("exec", argv)."""
    ran: list[tuple] = []

    async def relay(server, wait, how, runner=None, read_only=False):
        ran.append(("relay", server.name, wait, how, read_only))
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
    assert started == [("detach",), ("relay", "wiki", 12.5, None, False), ("bridge", "wiki")]
    with pytest.raises(ValueError, match="reached through the bridge"):
        mcp_launch.command(WIKI, read_only=False)


def test_read_only_reaches_the_relayed_command(fresh, started):
    seeded(gitlab_on=False)
    assert launch("github", "--read-only") == 0
    [(_, name, wait, (argv, env), read_only)] = started
    assert (name, wait, argv[-1], read_only) == ("github", 0.0, "--read-only", True)
    assert env["GITHUB_PERSONAL_ACCESS_TOKEN"]


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


# ── npx packages as node ────────────────────────────────────────

def installed(cache, version="2.1.69", bins=None, first="#!/usr/bin/env node\n"):
    """A copy of the GitLab package as npx leaves it in `cache`; its entry point."""
    package = cache / "_npx" / "0504ecbbdb697501" / "node_modules" / "@zereight" / "mcp-gitlab"
    (package / "build").mkdir(parents=True)
    (package / "build" / "index.js").write_text(first + "console.log('hi')\n")
    bins = bins or {"mcp-gitlab": "build/index.js", "zereight-mcp-gitlab": "build/index.js"}
    (package / "package.json").write_text(json.dumps({"name": "@zereight/mcp-gitlab", "version": version, "bin": bins}))
    return package / "build" / "index.js"


def test_a_package_npx_installed_runs_as_node_with_its_settings_and_keys(fresh):
    seeded()
    server = mcp_servers.get("gitlab")
    server.args.append("--verbose")
    entry = installed(fresh / "npm")
    argv, env = mcp_launch.command(server, read_only=True)
    assert argv == ["/opt/bin/node", str(entry.resolve()), "--verbose"]
    assert env["PATH"].split(os.pathsep)[0] == str(fresh / "npm/_npx/0504ecbbdb697501/node_modules/.bin")
    assert env[GL_VAR] == GL_TOKEN and env["GITLAB_PERMISSION_MODE"] == "readonly"


@pytest.mark.parametrize("version, bins, first", [
    ("2.1.70", None, "#!/usr/bin/env node\n"),  # another version: npx fetches the pinned one
    ("2.1.69", {"a": "build/index.js", "b": "build/index.js"}, "#!/usr/bin/env node\n"),  # npx would ask which
    ("2.1.69", None, "#!/bin/sh\n"),  # not node's
    ("2.1.69", "../../../../evil.js", "#!/usr/bin/env node\n"),  # outside its package
])
def test_npx_runs_a_package_it_hasnt_installed_as_pinned(fresh, version, bins, first):
    seeded()
    installed(fresh / "npm", version, bins, first)
    (fresh / "npm/_npx/evil.js").write_text("#!/usr/bin/env node\n")
    argv, _ = mcp_launch.command(mcp_servers.get("gitlab"), read_only=False)
    assert argv == ["/opt/bin/npx", "-y", "@zereight/mcp-gitlab@2.1.69"]


def test_only_a_plain_pinned_npx_command_is_run_as_node(fresh):
    installed(fresh / "npm")
    for args in (["-p", "@zereight/mcp-gitlab@2.1.69", "mcp-gitlab"], ["-y", "@zereight/mcp-gitlab"], []):
        assert mcp_launch.direct(args) is None
    assert mcp_launch.direct(["--yes", "@zereight/mcp-gitlab@2.1.69", "stdio"])[0][2:] == ["stdio"]
