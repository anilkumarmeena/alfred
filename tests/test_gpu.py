"""Tests for jarvis.core.gpu: the `env` shim that adds Metal's rules to the CLI's macOS sandbox profile, and only
those, and the CLI it relies on still starting sandboxed commands through `env`."""

import os
import subprocess
from pathlib import Path

import claude_agent_sdk
import pytest

from jarvis.brain.sdk import sdk_options
from jarvis.core import gpu, platform

SHIM = str(gpu.SHIM_DIR / "env")
METAL = ['(allow iokit-open (iokit-user-client-class "AGXDeviceUserClient"))',
         '(allow mach-lookup (global-name "com.apple.MTLCompilerService"))']
BUNDLED_CLI = Path(claude_agent_sdk.__file__).parent / "_bundled" / "claude"


@pytest.fixture
def sandbox_exec(tmp_path):
    """A stand-in sandbox-exec that prints $SET and its arguments, NUL-separated."""
    fake = tmp_path / "sandbox-exec"
    fake.write_text("#!/bin/sh\nprintf '%s\\0' \"$SET\" \"$@\"\n")
    fake.chmod(0o755)
    return str(fake)


def shim(*args: str) -> list[str]:
    return subprocess.run([SHIM, *args], capture_output=True, text=True, check=True).stdout.split("\0")[:-1]


def test_the_sandbox_profile_gets_metals_rules_and_nothing_else_changes(sandbox_exec):
    profile = "(version 1)\n(deny default)"
    set_, flag, given, *rest = shim("-u", "GONE", "SET=1", sandbox_exec, "-p", profile, "/bin/zsh", "-c", "echo -p x")
    assert (set_, flag, rest) == ("1", "-p", ["/bin/zsh", "-c", "echo -p x"])
    assert given.startswith(profile + "\n")  # the CLI's own profile first, untouched
    rules = [line for line in given.splitlines() if line.startswith("(")]
    assert rules == ["(version 1)", "(deny default)", *METAL]  # plus only Metal's two allows


def test_other_calls_pass_straight_through_to_env(sandbox_exec):
    assert shim("SET=bar", "/bin/sh", "-c", "printf '%s\\0' \"$SET\"") == ["bar"]
    assert shim(sandbox_exec, "-c", "-p") == ["", "-c", "-p"]  # -p not right after sandbox-exec: no profile


def test_the_shim_is_executable_and_alone_in_its_folder():
    assert os.access(SHIM, os.X_OK) and [p.name for p in gpu.SHIM_DIR.iterdir()] == ["env"]


@pytest.mark.parametrize("mac, sandbox, shimmed", [(True, {"enabled": True}, True), (True, {"enabled": False}, False),
                                                   (True, None, False), (False, {"enabled": True}, False)])
def test_only_sandboxed_clis_on_macos_get_the_shim_first_on_path(monkeypatch, mac, sandbox, shimmed):
    monkeypatch.setattr(platform, "MAC", mac)
    env = sdk_options(sandbox=sandbox).env
    assert env.get("PATH", "").startswith(f"{gpu.SHIM_DIR}{os.pathsep}") == shimmed
    if shimmed:
        assert env["PATH"].endswith(os.environ["PATH"])


@pytest.mark.skipif(not BUNDLED_CLI.is_file(), reason="no bundled claude CLI")
def test_the_bundled_cli_still_starts_sandboxed_commands_through_env():
    """The shim works only while the CLI runs `env ... /usr/bin/sandbox-exec -p PROFILE` with env found on PATH."""
    data = BUNDLED_CLI.read_bytes()
    at = data.find(b'"/usr/bin/sandbox-exec","-p",')
    assert at > 0 and b'["env",' in data[at - 200:at]
