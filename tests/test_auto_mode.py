"""Tests for auto mode: tasks and the voice brain run as Claude Code's auto mode, with the user's Claude Code setup and
no sandbox; only ask rules and auto mode's own asking reach Alfred's gates."""

import asyncio
import json
from types import SimpleNamespace

from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny
from test_connection_calls import ctx
from test_in_place import Asking, NullReporter, env, worker_task  # noqa: F401  (env is a fixture)
from test_mcp_servers import fresh, seeded  # noqa: F401  (fresh is a fixture)

from alfred.brain import cli
from alfred.gates import draft_gate, shell
from alfred.llm import claude
from alfred.skills import home
from alfred.tasks import worker
from alfred.tasks.gate import ASKED, AutoGate


def test_auto_mode_options_load_claude_codes_setup_and_keep_the_ask_rules():
    found = claude.auto_mode(["mcp__alfred__gmail_send"], {"permissions": {"deny": ["Read(/x)"], "ask": ["Bash(rm:*)"]}})
    assert (found["permission_mode"], found["setting_sources"], found["skills"], found["sandbox"]) == (
        "auto", ["user", "project", "local"], "all", None)
    assert json.loads(found["settings"]) == {"permissions": {"deny": ["Read(/x)"],
                                                              "ask": ["Bash(rm:*)", "mcp__alfred__gmail_send"]}}
    assert claude.auto_mode()["settings"] is None
    opts = claude.sdk_options(**claude.auto_mode())
    assert opts.permission_mode == "auto" and opts.setting_sources == ["user", "project", "local"] and opts.skills == "all"
    assert claude.sdk_options().setting_sources == []  # other sessions are as they were


def test_a_code_task_gets_claude_codes_tools_and_setup_unsandboxed(env):  # noqa: F811
    opts = worker._options(worker_task(in_place=1, worktree=str(env["video"]), branch="main"), NullReporter())
    assert opts.permission_mode == "auto" and opts.sandbox is None and opts.tools is None and opts.skills == "all"
    assert opts.setting_sources == ["user", "project", "local"] and opts.cwd == str(env["video"])
    assert "AskUserQuestion" in opts.disallowed_tools and "Bash(git push:*)" in opts.disallowed_tools
    assert not any(d.startswith(("Agent", "Task")) for d in opts.disallowed_tools)  # Explore, Plan and the rest run
    assert opts.agents == worker._agents("code_change") and isinstance(opts.can_use_tool, AutoGate)
    assert worker.ARTIFACT in opts.allowed_tools and "host" not in opts.mcp_servers  # Bash runs on the computer
    prompt = opts.system_prompt["append"]
    assert opts.system_prompt["preset"] == "claude_code" and "Claude Code's auto mode" in prompt
    assert "on branch main" in prompt and "uncommitted" in prompt and "sandbox:" not in prompt
    assert "services-restart" in prompt and "request_access" in prompt and "accounts (" in prompt
    assert "shell (" not in prompt  # nothing left to grant there


def test_a_research_task_keeps_claude_codes_prompt_and_asks_about_each_connection_change(fresh):  # noqa: F811
    seeded()
    opts = worker._options(worker_task(kind="research", project=None), NullReporter())
    assert opts.system_prompt["preset"] == "claude_code" and opts.tools is None
    asked = json.loads(opts.settings)["permissions"]["ask"]
    assert {"mcp__gitlab__create_merge_request_note", "mcp__github__create_pull_request"} <= set(asked)
    assert opts.can_use_tool.connections.tools == set(asked)
    assert "mcp__alfred__gmail_search" in opts.allowed_tools and "waits for their yes" in opts.system_prompt["append"]
    assert worker.ARTIFACT in opts.allowed_tools and opts.env["CLAUDE_CODE_ARTIFACT"] == "1"  # research publishes too


def test_a_read_only_task_runs_in_auto_mode_but_changes_nothing_through_its_connections(fresh):  # noqa: F811
    seeded()
    opts = worker._options(worker_task(kind="research", project=None, read_only=1, grants=json.dumps(["accounts"])),
                           NullReporter())
    assert opts.permission_mode == "auto" and opts.setting_sources == ["user", "project", "local"]
    assert {"Edit", "Write", "NotebookEdit", "mcp__gitlab__create_merge_request_note"} <= set(opts.disallowed_tools)
    assert opts.settings is None or "ask" not in json.loads(opts.settings).get("permissions", {})  # nothing to ask


def call(gate, tool: str, agent: str | None = None, **args):
    return asyncio.run(gate(tool, args, SimpleNamespace(agent_id=agent, tool_use_id="u1")))


def test_what_auto_mode_stops_to_ask_about_goes_to_the_phone_and_a_yes_lets_it_through_once(tmp_path):
    reporter = Asking()
    gate = AutoGate(9, tmp_path, reporter)
    refused = call(gate, "Bash", command="git push --force origin main")
    [item] = reporter.asked
    assert isinstance(refused, PermissionResultDeny) and "request 12" in refused.message
    assert (item["tool"], item["command"], item["folder"]) == ("Bash", "git push --force origin main", str(tmp_path))
    assert item["why"] != ASKED  # what the command itself does
    edit = call(gate, "Edit", file_path="/etc/hosts")
    assert isinstance(edit, PermissionResultDeny) and reporter.asked[1]["command"].startswith("Edit {")
    assert reporter.asked[1]["why"] == ASKED and reporter.asked[1]["remember"]["never"]
    reporter.agreed.append("git push --force origin main")
    assert isinstance(call(gate, "Bash", command="git push --force origin main"), PermissionResultAllow)
    sub = call(gate, "Bash", agent="a1", command="rm -rf /tmp/x")
    assert isinstance(sub, PermissionResultDeny) and "only the task's own agent" in sub.message and len(reporter.asked) == 2


def test_a_connection_change_goes_to_its_own_gate(tmp_path):
    seen = []

    class Connections:
        def __init__(self) -> None:
            self.tools = {"mcp__gitlab__create_merge_request_note"}

        async def __call__(self, name, args, c):
            seen.append(name)
            return PermissionResultDeny(message="asked")

    gate = AutoGate(9, tmp_path, Asking(), Connections())
    assert call(gate, "mcp__gitlab__create_merge_request_note", body="x").message == "asked" and seen
    assert isinstance(asyncio.run(gate("mcp__gitlab__create_merge_request_note", {}, ctx())), PermissionResultDeny)


def test_the_voice_brain_runs_in_auto_mode_and_still_asks_aloud_for_sends(tmp_path, monkeypatch):
    monkeypatch.setattr(home, "home", lambda: tmp_path / "skills")
    (tmp_path / "skills").mkdir()
    brain = cli.CliBrain()
    opts = cli._options(brain.shell, brain.jobs)
    assert opts.permission_mode == "auto" and opts.sandbox is None and opts.tools is None and opts.skills == "all"
    assert {"AskUserQuestion", "EnterPlanMode"} <= set(opts.disallowed_tools) and opts.can_use_tool is brain.shell
    asked = set(json.loads(opts.settings)["permissions"]["ask"])
    assert draft_gate.gated() <= asked and shell.START_JOB not in asked  # auto mode's check clears a job
    assert "Claude Code's auto mode" in opts.system_prompt and "Commands run in a sandbox" not in opts.system_prompt


def test_alfreds_helpers_keep_their_own_model_whatever_the_task_asks():
    from alfred.tasks.subagents import own_models

    [matcher] = own_models(["code-searcher"])
    hook = matcher.hooks[0]

    def called(**args):
        return asyncio.run(hook({"tool_name": "Agent", "tool_input": args}, "u1", None))

    changed = called(subagent_type="code-searcher", model="opus", prompt="find it")
    assert changed["hookSpecificOutput"]["updatedInput"] == {"subagent_type": "code-searcher", "prompt": "find it"}
    assert called(subagent_type="Explore", model="opus", prompt="x") == {}  # Claude Code's own and the user's choose
    assert called(subagent_type="code-searcher", prompt="x") == {}
