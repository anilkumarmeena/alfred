"""Tests for jarvis.core.platform (which OS JARVIS runs on) and the Linux time zone fallbacks in config."""

import subprocess
import sys
from pathlib import Path

import pytest

from jarvis import config
from jarvis.config import ROOT
from jarvis.core import platform


def test_exactly_one_of_mac_and_linux_here():
    assert platform.MAC != platform.LINUX
    assert platform.SERVICE_MANAGER == ("launchd" if platform.MAC else "systemd")
    assert not platform.APPLE_SILICON or platform.MAC
    assert not platform.WSL or platform.LINUX


@pytest.mark.parametrize("name", ["win32", "cygwin"])
def test_native_windows_is_told_to_use_wsl2(name, monkeypatch):
    monkeypatch.setattr(platform, "MAC", False)
    monkeypatch.setattr(platform, "LINUX", False)
    monkeypatch.setattr(platform.sys, "platform", name)
    with pytest.raises(SystemExit) as exit:
        platform.require_supported()
    assert "WSL2" in str(exit.value.code)


def test_other_systems_are_named(monkeypatch):
    monkeypatch.setattr(platform, "MAC", False)
    monkeypatch.setattr(platform, "LINUX", False)
    monkeypatch.setattr(platform.sys, "platform", "freebsd14")
    assert platform.unsupported() == "JARVIS runs on macOS and Linux, not freebsd14."


def test_mac_and_linux_are_supported(monkeypatch):
    for mac, linux in ((True, False), (False, True)):
        monkeypatch.setattr(platform, "MAC", mac)
        monkeypatch.setattr(platform, "LINUX", linux)
        platform.require_supported()


def test_importing_jarvis_on_windows_says_wsl2_not_importerror():
    """jarvis/__init__ checks before config imports pwd, which Windows lacks."""
    done = subprocess.run([sys.executable, "-c", "import sys; sys.platform = 'win32'; import jarvis.config"],
                          cwd=ROOT, capture_output=True, text=True)
    assert done.returncode == 1 and "WSL2" in done.stderr and "Traceback" not in done.stderr


@pytest.mark.parametrize("release, wsl", [("6.6.87.2-microsoft-standard-WSL2\n", True), ("6.8.0-45-generic\n", False)])
def test_wsl_is_told_apart_by_the_kernel_release(release, wsl, tmp_path, monkeypatch):
    osrelease = tmp_path / "osrelease"
    osrelease.write_text(release)
    real_open = open
    monkeypatch.setattr(platform, "open", lambda path, *a: real_open(osrelease, *a), raising=False)
    assert platform._wsl() is wsl


def test_without_proc_it_is_not_wsl(monkeypatch):
    def missing(path, *a):
        raise FileNotFoundError(path)

    monkeypatch.setattr(platform, "open", missing, raising=False)
    assert platform._wsl() is False


# ── Time zone on Linux ─────────────────────────────────────────


@pytest.fixture
def copied_localtime(monkeypatch):
    """Linux, with /etc/localtime a copy rather than a symlink into zoneinfo."""
    monkeypatch.setattr(config.platform, "LINUX", True)
    localtime(monkeypatch, "/etc/localtime")


def localtime(monkeypatch, target: str) -> None:
    """Resolve /etc/localtime to `target`, other paths as usual."""
    real = config.os.path.realpath
    monkeypatch.setattr(config.os.path, "realpath",
                        lambda path, **kw: target if path == "/etc/localtime" else real(path, **kw))


def test_the_localtime_symlink_wins(monkeypatch):
    localtime(monkeypatch, "/usr/share/zoneinfo/Europe/Berlin")
    monkeypatch.setattr(config, "_linux_zones", lambda: pytest.fail("looked further"))
    assert config.mac_timezone() == "Europe/Berlin"


def test_linux_falls_back_to_etc_timezone_then_timedatectl_then_tz(copied_localtime, tmp_path, monkeypatch):
    etc = tmp_path / "timezone"
    monkeypatch.setattr(config.subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, "Asia/Tokyo\n"))
    monkeypatch.setenv("TZ", ":America/Chicago")
    assert list(config._linux_zones(etc)) == ["Asia/Tokyo\n", ":America/Chicago"]
    etc.write_text("Asia/Kolkata\n")
    assert next(config._linux_zones(etc)) == "Asia/Kolkata\n"
    monkeypatch.setattr(config, "_linux_zones", lambda: iter(["", "Not/AZone", ":America/Chicago"]))
    assert config.mac_timezone() == "America/Chicago"


def test_linux_without_any_zone_says_so(copied_localtime, monkeypatch):
    monkeypatch.setattr(config, "_linux_zones", lambda: iter(["", "UTC0"]))
    assert config.mac_timezone() == ""


def test_the_mac_does_not_look_further(monkeypatch):
    monkeypatch.setattr(config.platform, "LINUX", False)
    localtime(monkeypatch, "/etc/localtime")
    monkeypatch.setattr(config, "_linux_zones", lambda: pytest.fail("looked further"))
    assert config.mac_timezone() == ""


def test_no_timedatectl_is_fine(tmp_path, monkeypatch):
    def missing(cmd, **kw):
        raise FileNotFoundError(cmd[0])

    monkeypatch.setattr(config.subprocess, "run", missing)
    monkeypatch.delenv("TZ", raising=False)
    assert list(config._linux_zones(Path(tmp_path / "none"))) == [""]
