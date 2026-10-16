"""Connection and skill changes reaching live sessions: the stamps that notice them, the voice brain carrying its
conversation on into a CLI with the new setup, and a running task bringing its connections up to date in place."""

import asyncio
from dataclasses import replace

import pytest
from claude_agent_sdk import ClaudeSDKError
from test_in_place import NullReporter, worker_task
from test_mcp_servers import GH_TOKEN, GL_TOKEN, fresh, seeded  # noqa: F401  (fixture)
from test_skills import manual, skills_home  # noqa: F401  (fixture)

from jarvis.brain import cli
from jarvis.core import claude_logins, vault
from jarvis.integrations import mcp_servers
from jarvis.integrations.mcp_servers import Server, Tool
from jarvis.skills import home
from jarvis.tasks import worker
from jarvis.tasks.connection_watch import Watch

NEW_TOKEN = "glpat-another-not-real-token"


def stamps() -> dict[str, str]:
    return mcp_servers.stamps(mcp_servers.load())


# ── Noticing a change ───────────────────────────────────────────

def test_a_new_key_or_address_changes_only_that_servers_stamp_and_the_stamp_never_holds_the_key(fresh):
    seeded()
    before = stamps()
    assert stamps() == before  # nothing changed, nothing noticed
    vault.put("gitlab.token", NEW_TOKEN)
    keyed = stamps()
    assert keyed["gitlab"] != before["gitlab"] and keyed["github"] == before["github"]
    mcp_servers.save(mcp_servers.readdress(mcp_servers.need("gitlab"), "GITLAB_API_URL", "https://code.example.com/api/v4"))
    assert stamps()["gitlab"] != keyed["gitlab"]
    assert not any(token in "".join(stamps().values()) for token in (GH_TOKEN, GL_TOKEN, NEW_TOKEN))


def test_sorting_tools_again_or_clearing_a_key_changes_the_stamp(fresh):
    seeded()
    before = stamps()
    gitlab = mcp_servers.need("gitlab")
    mcp_servers.save(replace(gitlab, tools=gitlab.tools | {"list_todos": Tool("blocked")}))
    sorted_again = stamps()
    assert sorted_again["gitlab"] != before["gitlab"]
    vault.remove("gitlab.token")
    assert stamps()["gitlab"] != sorted_again["gitlab"]


def test_signing_in_changes_the_stamp_but_the_bridge_refreshing_its_token_doesnt(fresh):
    mcp_servers.save(Server("docs", "Docs", transport="http", url="https://mcp.example.com/mcp", sign_in=True,
                            tools={"search": Tool("read")}), new=True)
    vault.ensure()
    out = stamps()["docs"]
    vault.put("docs.oauth", '{"access_token": "one"}')
    signed_in = stamps()["docs"]
    vault.put("docs.oauth", '{"access_token": "two"}')
    assert signed_in != out and stamps()["docs"] == signed_in


def test_switching_a_skill_on_or_off_or_saving_it_changes_the_skills_stamp(skills_home):  # noqa: F811
    home.save("standup", "Writes the standup.", "1. Write it.")
    before = home.stamp()
    assert home.stamp() == before
    home.switch("standup", False)
    off = home.stamp()
    home.switch("standup", True)
    on = home.stamp()
    home.save("standup", "Writes the standup.", "1. Write it.\n2. Send it.")
    assert len({before, off, on, home.stamp()}) == 4 and before != off


# ── The voice brain ─────────────────────────────────────────────

def _on(name: str) -> None:
    mcp_servers.save(replace(mcp_servers.need(name), enabled=True))


CHANGES = {
    "a new key": lambda: vault.put("gitlab.token", NEW_TOKEN),
    "a new server address": lambda: mcp_servers.save(mcp_servers.readdress(
        mcp_servers.need("gitlab"), "GITLAB_API_URL", "https://code.example.com/api/v4")),
    "a connection switched off": lambda: mcp_servers.save(replace(mcp_servers.need("github"), enabled=False)),
    "a connection removed": lambda: mcp_servers.remove("github"),
    "a connection added": lambda: mcp_servers.save(Server("acme", "Acme", command="acme-mcp",
                                                          tools={"look": Tool("read")}), new=True),
    "tools sorted again": lambda: mcp_servers.save(replace(mcp_servers.need("github"), tools={})),
    "a skill saved": lambda: home.save("standup", "Writes the standup.", "1. Write it."),
}


@pytest.mark.parametrize("change", CHANGES)
def test_a_change_mid_conversation_restarts_the_cli_on_the_same_conversation_and_says_so(fresh, skills_home, voice,  # noqa: F811
                                                                                        change):
    seeded()
    v = voice()
    asyncio.run(v.say("check my merge requests"))
    asyncio.run(v.say("anything else?"))
    assert len(v.clis) == 1 and v.resumed == [None]  # nothing changed: the same CLI
    CHANGES[change]()
    asyncio.run(v.say("try again"))
    assert len(v.clis) == 2 and v.clis[0].closed and v.resumed == [None, "s"]  # the session the first one reported
    assert cli.RELOADED in v.cli.prompts[-1] and "try again" in v.cli.prompts[-1]
    asyncio.run(v.say("thanks"))
    assert len(v.clis) == 2 and cli.RELOADED not in v.cli.prompts[-1]  # said once


def test_a_connection_switched_on_mid_conversation_comes_in_at_the_next_turn(fresh, voice):  # noqa: F811
    seeded(gitlab_on=False)
    v = voice()
    asyncio.run(v.say("hello"))
    _on("gitlab")
    vault.put("gitlab.token", GL_TOKEN)
    asyncio.run(v.say("check gitlab"))
    assert len(v.clis) == 2 and v.resumed == [None, "s"]


def test_carrying_on_that_fails_starts_a_new_conversation_rather_than_none(fresh, voice, monkeypatch):  # noqa: F811
    seeded()
    v = voice()
    asyncio.run(v.say("hello"))
    made = cli.ClaudeSDKClient

    def started(options):
        client = made(options)
        if v.resumed[-1]:
            async def refused() -> None:
                raise ClaudeSDKError("no conversation found")
            client.connect = refused
        return client

    monkeypatch.setattr(cli, "ClaudeSDKClient", started)
    vault.put("gitlab.token", NEW_TOKEN)
    assert asyncio.run(v.say("try again")) == "Okay."
    assert v.resumed == [None, "s", None] and v.cli.connected


def test_the_cli_carries_on_a_session_on_the_login_holding_it_or_starts_afresh(fresh, monkeypatch):  # noqa: F811
    brain = cli.CliBrain()
    monkeypatch.setattr(claude_logins, "resume_on", lambda session: "work" if session == "known" else None)
    monkeypatch.setattr(claude_logins, "env", lambda login: {"CLAUDE_CONFIG_DIR": f"/logins/{login or 'mac'}"})
    carried = cli._options(brain.shell, brain.jobs, resume="known")
    assert carried.resume == "known" and carried.env["CLAUDE_CONFIG_DIR"] == "/logins/work"
    lost = cli._options(brain.shell, brain.jobs, resume="gone")
    assert lost.resume is None
    assert cli._options(brain.shell, brain.jobs).resume is None


# ── Running tasks ───────────────────────────────────────────────

class Client:
    """Records the MCP controls a task's watch sends to its CLI."""

    def __init__(self, fails: bool = False) -> None:
        self.sent: list[tuple] = []
        self.fails = fails

    async def reconnect_mcp_server(self, name: str) -> None:
        self.sent.append(("reconnect", name))
        if self.fails:
            raise Exception("Server not found")

    async def toggle_mcp_server(self, name: str, enabled: bool) -> None:
        self.sent.append(("on" if enabled else "off", name))


def watching() -> Watch:
    live = Watch()
    live.use(mcp_servers.session(mcp_servers.for_research(), read_only=True).servers)
    return live


def test_a_task_restarts_a_connection_whose_key_or_address_changed_once_and_leaves_the_others(fresh):
    seeded()
    live, client = watching(), Client()
    asyncio.run(live.check(client))
    assert client.sent == []
    vault.put("gitlab.token", NEW_TOKEN)
    asyncio.run(live.check(client))
    asyncio.run(live.check(client))
    assert client.sent == [("reconnect", "gitlab")]
    mcp_servers.save(mcp_servers.readdress(mcp_servers.need("gitlab"), "GITLAB_API_URL", "https://code.example.com/api/v4"))
    asyncio.run(live.check(client))
    assert client.sent == [("reconnect", "gitlab")] * 2


def test_a_task_drops_a_connection_switched_off_and_takes_it_back_when_switched_on(fresh):
    seeded()
    live, client = watching(), Client()
    mcp_servers.save(replace(mcp_servers.need("github"), enabled=False))
    asyncio.run(live.check(client))
    assert client.sent == [("off", "github")]
    assert live.refused("mcp__github__list_notifications", {}) == "The github connection is switched off now."
    _on("github")
    asyncio.run(live.check(client))
    assert client.sent == [("off", "github"), ("on", "github")]
    assert live.refused("mcp__github__list_notifications", {}) == ""


def test_a_task_drops_a_connection_whose_key_was_cleared_or_that_was_taken_off_research(fresh):
    seeded()
    live, client = watching(), Client()
    vault.remove("gitlab.token")
    mcp_servers.save(replace(mcp_servers.need("github"), for_research=False))
    asyncio.run(live.check(client))
    assert sorted(client.sent) == [("off", "github"), ("off", "gitlab")]


def test_a_tool_sorted_away_from_reading_mid_task_is_refused(fresh):
    seeded()
    live = watching()
    gitlab = mcp_servers.need("gitlab")
    assert live.refused("mcp__gitlab__list_todos", {}) == ""
    mcp_servers.save(replace(gitlab, tools=gitlab.tools | {"list_todos": Tool("blocked")}))
    asyncio.run(live.check(Client()))
    assert live.refused("mcp__gitlab__list_todos", {}) == "list_todos isn't available."
    assert live.refused("mcp__gitlab__get_merge_request", {}) == ""
    assert live.refused("mcp__jarvis__memory_read", {}) == "" and live.refused("WebSearch", {}) == ""


def test_the_task_hears_once_which_connection_was_restarted(fresh):
    seeded()
    live = watching()
    vault.put("gitlab.token", NEW_TOKEN)
    asyncio.run(live.check(Client()))
    tell = live.told[0].hooks[0]
    said = asyncio.run(tell({"hook_event_name": "PostToolUse", "tool_name": "WebSearch"}, None, None))
    assert said["hookSpecificOutput"]["additionalContext"].startswith("GitLab restarted with new settings")
    assert asyncio.run(tell({"hook_event_name": "PostToolUse", "tool_name": "WebSearch"}, None, None)) == {}


def test_a_restart_the_cli_refuses_is_tried_once_and_the_task_carries_on(fresh):
    seeded()
    live, client = watching(), Client(fails=True)
    vault.put("gitlab.token", NEW_TOKEN)
    asyncio.run(live.check(client))
    asyncio.run(live.check(client))
    assert client.sent == [("reconnect", "gitlab")] and live.news == []


def test_research_tasks_watch_their_connections_and_refuse_calls_to_one_switched_off(fresh):
    seeded()
    live = Watch()
    opts = worker._options(worker_task(kind="research", project=None), NullReporter(), live)
    assert set(live.running) == {"github", "gitlab"}
    mcp_servers.save(replace(mcp_servers.need("github"), enabled=False))
    asyncio.run(live.check(Client()))
    call = {"hook_event_name": "PreToolUse", "tool_name": "mcp__github__list_notifications", "tool_input": {}}
    said = [asyncio.run(hook(call, "t1", None)) for m in opts.hooks["PreToolUse"] for hook in m.hooks]
    assert any(s.get("hookSpecificOutput", {}).get("permissionDecision") == "deny" for s in said)


def test_a_session_without_connections_has_nothing_to_watch():
    asyncio.run(asyncio.wait_for(Watch().run(Client(), every=60), 1))  # ends at once
