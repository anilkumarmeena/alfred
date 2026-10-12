"""Tests for the Claude login JARVIS runs on: the user's own unless JARVIS_CLAUDE_CONFIG_DIR names one of JARVIS's."""

import os
import subprocess
import sys
from pathlib import Path

from jarvis.brain import sdk
from jarvis.core.config import ROOT


def config_dirs(**env: str) -> list[str]:
    """CLAUDE_OWN_CONFIG_DIR and CLAUDE_CONFIG_DIR as a fresh process reads them (set-but-empty = unset, not .env)."""
    code = "from jarvis.core import config; print(config.CLAUDE_OWN_CONFIG_DIR); print(config.CLAUDE_CONFIG_DIR)"
    env = os.environ | {"JARVIS_CLAUDE_CONFIG_DIR": "", "CLAUDE_CONFIG_DIR": ""} | env
    return subprocess.run([sys.executable, "-c", code], env=env, cwd=ROOT, capture_output=True, text=True,
                          check=True).stdout.splitlines()


def test_by_default_the_cli_gets_no_config_dir_so_it_finds_the_users_login(monkeypatch):
    monkeypatch.setattr(sdk, "CLAUDE_OWN_CONFIG_DIR", "")
    assert "CLAUDE_CONFIG_DIR" not in sdk.sdk_options().env  # even ~/.claude would miss the macOS Keychain login


def test_a_login_of_jarvis_own_is_passed_on(monkeypatch):
    monkeypatch.setattr(sdk, "CLAUDE_OWN_CONFIG_DIR", "/j/secrets/claude")
    assert sdk.sdk_options().env["CLAUDE_CONFIG_DIR"] == "/j/secrets/claude"


def test_the_cli_folder_is_the_users_unless_jarvis_has_its_own():
    assert config_dirs() == ["", str(Path.home() / ".claude")]
    assert config_dirs(CLAUDE_CONFIG_DIR="/u/claude") == ["", "/u/claude"]
    own = str(ROOT / "secrets/claude")
    assert config_dirs(JARVIS_CLAUDE_CONFIG_DIR="secrets/claude") == [own, own]  # relative to the checkout
    assert config_dirs(JARVIS_CLAUDE_CONFIG_DIR="/abs/claude") == ["/abs/claude", "/abs/claude"]
