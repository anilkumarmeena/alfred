"""Tests for what background tasks may read: the user's projects and voice session logs, never secrets."""

import asyncio
import json
from pathlib import Path

import pytest
from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny
from test_in_place import NullReporter, worker_task

from conftest import use_task_cache
from jarvis.core import platform, profile
from jarvis.safety import shell
from jarvis.tasks import access, briefs, gate, worker


@pytest.fixture
def mac(tmp_path, monkeypatch):
    """A projects folder with JARVIS (secrets, Claude config, session logs) and one other project."""
    projects = tmp_path / "projects"
    jarvis = projects / "jarvis"
    secrets = jarvis / "secrets"
    claude = secrets / "claude"
    logs = claude / "projects" / "-Users-me-projects-jarvis-data-shell"
    other_sessions = claude / "projects" / "-Users-me-projects-jarvis-data-worktrees-jarvis-3"
    for folder in (logs, other_sessions, jarvis / "logs", projects / "video" / "config" / "secrets", tmp_path / "cache"):
        folder.mkdir(parents=True)
    files = {
        "log": logs / "abc.jsonl", "agent_log": jarvis / "logs" / "agent.log", "code": projects / "video" / "render.py",
        "env": jarvis / ".env", "env_local": projects / "video" / ".env.local", "token": secrets / "google-work.json",
        "login": claude / ".credentials.json", "settings": claude / "settings.json", "other_session": other_sessions / "x.jsonl",
        "nested": projects / "video" / "config" / "secrets" / "api.txt", "key": projects / "video" / "server.pem",
        "ssh": projects / "video" / "id_ed25519",
    }
    for file in files.values():
        file.write_text("x")
    monkeypatch.setattr(worker, "TASK_READ_DIRS", [projects])
    monkeypatch.setattr(briefs, "TASK_READ_DIRS", [projects])
    monkeypatch.setattr(gate, "SECRETS_DIR", secrets)
    monkeypatch.setattr(gate, "CLAUDE_CONFIG_DIR", str(claude))
    monkeypatch.setattr(profile.Profile, "voice_logs", property(lambda _: logs))
    use_task_cache(monkeypatch, tmp_path / "cache")
    worktree = jarvis / "data" / "worktrees" / "jarvis-9"
    worktree.mkdir(parents=True)
    return {"projects": projects, "worktree": worktree, "logs": logs, **files}


def call(opts, tool: str, **args):
    return asyncio.run(opts.can_use_tool(tool, args, None))


@pytest.mark.parametrize("kind", ["code_change", "code_question"])
def test_tasks_read_the_projects_and_the_session_logs(mac, kind, monkeypatch):
    monkeypatch.setattr(worker.projects, "find", lambda name: type("P", (), {"path": mac["worktree"]}))
    opts = worker._options(worker_task(kind=kind, worktree=str(mac["worktree"])), NullReporter())
    for name in ("code", "log", "agent_log"):
        assert isinstance(call(opts, "Read", file_path=str(mac[name])), PermissionResultAllow), name
    assert isinstance(call(opts, "Grep", pattern="x", path=str(mac["projects"])), PermissionResultAllow)
    assert isinstance(call(opts, "Glob", pattern="*.jsonl", path=str(mac["logs"])), PermissionResultAllow)
    assert isinstance(call(opts, "Read", file_path="/etc/hosts"), PermissionResultDeny)  # only the projects
    prompt = opts.system_prompt["append"]
    assert str(mac["projects"]) in prompt and str(mac["logs"]) in prompt and "copied in" in prompt


def test_other_projects_are_read_only(mac):
    opts = worker._options(worker_task(worktree=str(mac["worktree"])), NullReporter())
    for tool, arg in (("Edit", "file_path"), ("Write", "file_path")):
        assert isinstance(call(opts, tool, **{arg: str(mac["code"])}), PermissionResultDeny)
    assert isinstance(call(opts, "Write", file_path=str(mac["worktree"] / "new.py")), PermissionResultAllow)


@pytest.mark.parametrize("name", ["env", "env_local", "token", "login", "settings", "other_session", "nested", "key", "ssh"])
def test_secrets_stay_hidden_from_the_file_tools(mac, name):
    opts = worker._options(worker_task(worktree=str(mac["worktree"])), NullReporter())
    denied = call(opts, "Read", file_path=str(mac[name]))
    assert isinstance(denied, PermissionResultDeny) and "secrets" in denied.message


def test_secrets_folders_cant_be_searched(mac):
    opts = worker._options(worker_task(worktree=str(mac["worktree"])), NullReporter())
    assert isinstance(call(opts, "Grep", pattern="token", path=str(mac["token"].parent)), PermissionResultDeny)
    assert isinstance(call(opts, "Glob", pattern="*", path=str(mac["login"].parent)), PermissionResultDeny)


def denied_paths(opts) -> list[str]:
    rules = json.loads(opts.settings)["permissions"]["deny"]
    return [r.removeprefix("Read(").removesuffix(")").replace("//", "/", 1) for r in rules]


def test_deny_rules_hide_secrets_from_the_sandbox_too(mac):
    rules = denied_paths(worker._options(worker_task(worktree=str(mac["worktree"])), NullReporter()))
    literal = [Path(r.removesuffix("/**")) for r in rules if "*" not in r.removesuffix("/**")]
    for name in ("token", "login", "settings", "other_session"):
        assert any(mac[name].resolve().is_relative_to(p) for p in literal), name
    projects = str(mac["projects"].resolve())
    assert f"{projects}/**/.env" in rules and f"{projects}/**/.env.*" in rules and f"{projects}/**/*.pem" in rules
    assert f"{projects}/video/**/secrets/**" in rules
    assert "~/.ssh/**" in rules


def test_deny_rules_leave_the_session_logs_readable(mac):
    rules = denied_paths(worker._options(worker_task(worktree=str(mac["worktree"])), NullReporter()))
    logs = mac["log"].resolve()
    for rule in rules:
        prefix = Path(rule.split("*")[0].rstrip("/"))
        if "/**/secrets/**" in rule:  # covers logs only if a secrets folder is below its prefix
            assert not (logs.is_relative_to(prefix) and "secrets" in logs.relative_to(prefix).parts), rule
        elif "*" not in rule.removesuffix("/**"):
            assert not logs.is_relative_to(Path(rule.removesuffix("/**"))), rule


@pytest.mark.parametrize("in_place", [0, 1])
def test_code_change_commands_reach_any_host_but_stay_boxed_in(mac, in_place):
    opts = worker._options(worker_task(worktree=str(mac["worktree"]), in_place=in_place), NullReporter())
    sandbox = opts.sandbox
    assert sandbox["enabled"] and not sandbox["allowUnsandboxedCommands"]
    assert sandbox["network"]["allowedDomains"] == ["*"]
    if platform.MAC:  # a Seatbelt rule: Linux's sandbox has no Mach lookups
        assert "com.apple.SystemConfiguration.configd" in sandbox["network"]["allowMachLookup"]  # or uv crashes
    else:
        assert "allowMachLookup" not in sandbox["network"]
    assert isinstance(call(opts, "Bash", command="curl https://example.com", dangerouslyDisableSandbox=True), PermissionResultDeny)
    assert "~/.ssh/**" in denied_paths(opts) and "~/Library/Keychains/**" in denied_paths(opts)
    assert "any host" in opts.system_prompt["append"]


def test_code_questions_and_the_voice_shell_keep_the_network_closed(mac, monkeypatch):
    monkeypatch.setattr(worker.projects, "find", lambda name: type("P", (), {"path": mac["worktree"]}))
    opts = worker._options(worker_task(kind="code_question", worktree=str(mac["worktree"])), NullReporter())
    assert opts.sandbox["network"] == {"allowedDomains": []}
    assert shell.SANDBOX["network"] == {"allowedDomains": []}


def test_research_tasks_still_read_no_files(mac):
    opts = worker._options(worker_task(kind="research", project=None), NullReporter())
    assert "permissions" not in json.loads(opts.settings)
    assert isinstance(call(opts, "Read", file_path=str(mac["code"])), PermissionResultDeny)


def test_code_change_and_research_tasks_can_publish_artifacts(mac, monkeypatch):
    monkeypatch.setattr(worker, "DATA_DIR", mac["projects"].parent / "data")
    for kind in ("code_change", "research"):
        opts = worker._options(worker_task(kind=kind, project=None if kind == "research" else "video-agent",
                                           worktree=str(mac["worktree"])), NullReporter())
        assert opts.env["CLAUDE_CODE_ARTIFACT"] == "1", kind
        assert "Artifact" in opts.tools and "Artifact" in opts.allowed_tools, kind
        prompt = opts.system_prompt if isinstance(opts.system_prompt, str) else opts.system_prompt["append"]
        assert "Artifact tool" in prompt and "claude.ai/code/artifact" in prompt and "local copy" in prompt, kind


def test_artifacts_switch_off_and_skip_code_questions(mac, monkeypatch):
    monkeypatch.setattr(worker.projects, "find", lambda name: type("P", (), {"path": mac["worktree"]}))
    question = worker._options(worker_task(kind="code_question", worktree=str(mac["worktree"])), NullReporter())
    monkeypatch.setattr(access, "TASK_ARTIFACTS", False)
    off = worker._options(worker_task(worktree=str(mac["worktree"])), NullReporter())
    for opts in (question, off):
        assert "CLAUDE_CODE_ARTIFACT" not in opts.env and "Artifact" not in opts.tools
        assert "claude.ai/code/artifact" not in opts.system_prompt["append"]
    assert "Artifact tool" not in off.system_prompt["append"]  # not even to ask for
    assert "artifacts (publish pages" in question.system_prompt["append"]  # a code_question can ask for it


def test_artifacts_publish_only_the_tasks_own_files(mac):
    opts = worker._options(worker_task(worktree=str(mac["worktree"])), NullReporter())
    page, url = str(mac["worktree"] / "docs" / "mockup.html"), "https://claude.ai/code/artifact/abc"
    assert isinstance(call(opts, "Artifact", file_path=page, icon="page"), PermissionResultAllow)
    assert isinstance(call(opts, "Artifact", action="publish", file_path="docs/mockup.html"), PermissionResultAllow)
    assert isinstance(call(opts, "Artifact", action="read", url=url), PermissionResultAllow)
    assert isinstance(call(opts, "Artifact", action="list"), PermissionResultAllow)
    for args in ({"file_path": str(mac["code"])}, {"file_path": str(mac["env"])}, {"file_path": "../../../../secrets/google-work.json"},
                 {"action": "publish", "url": url, "file_paths": [page, str(mac["key"])]},
                 {"action": "read", "url": url, "out_dir": str(mac["projects"])},
                 {"action": "delete", "url": url}, {"action": "upload_asset", "url": url, "file_path": page}):
        assert isinstance(call(opts, "Artifact", **args), PermissionResultDeny), args


def test_artifacts_meet_the_gate_ahead_of_the_allow_rule(mac):
    opts = worker._options(worker_task(worktree=str(mac["worktree"])), NullReporter())
    hook = opts.hooks["PreToolUse"][0].hooks[0]
    page = str(mac["worktree"] / "page.html")

    def decide(data: dict) -> dict:
        return asyncio.run(hook({"tool_name": "Artifact"} | data, "t1", None))

    assert decide({"tool_input": {"file_path": str(mac["token"])}})["hookSpecificOutput"]["permissionDecision"] == "deny"
    helper = decide({"agent_id": "a1", "agent_type": "test-runner", "tool_input": {"file_path": page}})
    assert helper["hookSpecificOutput"]["permissionDecision"] == "deny"  # JARVIS's helpers don't publish
    assert decide({"tool_input": {"file_path": page}}) == {}


def test_research_writes_pages_only_in_its_own_folder(mac, monkeypatch):
    monkeypatch.setattr(worker, "DATA_DIR", mac["projects"].parent / "data")
    opts = worker._options(worker_task(kind="research", project=None), NullReporter())
    page = str(Path(opts.cwd) / "page.html")
    assert isinstance(call(opts, "Write", file_path=page), PermissionResultAllow)
    assert isinstance(call(opts, "Artifact", file_path=page), PermissionResultAllow)
    assert isinstance(call(opts, "Write", file_path=str(mac["code"])), PermissionResultDeny)
    assert isinstance(call(opts, "Read", file_path=str(mac["code"])), PermissionResultDeny)
    assert "Edit" not in opts.tools and "Bash" not in opts.tools
