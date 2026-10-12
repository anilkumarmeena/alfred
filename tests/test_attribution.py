"""Tests for commit and PR attribution: every Claude session JARVIS starts credits the assistant, not the model."""

import json
from dataclasses import replace

from claude_agent_sdk._internal.transport.subprocess_cli import SubprocessCLITransport

from jarvis.brain import sdk
from jarvis.core.config import ASSISTANT_NAME


def test_sessions_credit_the_assistant():
    settings = json.loads(sdk.sdk_options().settings)
    assert settings["attribution"]["commit"] == f"Co-Authored-By: {ASSISTANT_NAME}"
    assert ASSISTANT_NAME in settings["attribution"]["pr"]
    assert "anthropic" not in json.dumps(settings).lower()


def test_attribution_joins_the_session_s_own_settings():
    own = {"disableAllHooks": True, "permissions": {"deny": ["Read(//x)"]}}
    assert json.loads(sdk.sdk_options(settings=json.dumps(own)).settings) == own | {"attribution": sdk.ATTRIBUTION}


def test_the_cli_gets_it_alongside_the_sandbox():
    opts = sdk.sdk_options(sandbox={"enabled": True}, settings=json.dumps({"disableAllHooks": True}))
    command = SubprocessCLITransport("hi", replace(opts, cli_path="/bin/true"))._build_command()
    passed = json.loads(command[command.index("--settings") + 1])
    assert passed["attribution"] == sdk.ATTRIBUTION and passed["sandbox"]["enabled"] is True
    assert opts.setting_sources == []  # flag settings are the only ones the CLI reads
