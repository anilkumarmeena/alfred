"""The task walls' own checks on this computer (make host-check): what a task's host command can and can't reach,
run inside the real walls (jarvis.safety.host), plus signing in to the user's own git hosts over SSH.

  uv run python -m jarvis.apps.host_check check
"""

import asyncio
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

from jarvis import projects
from jarvis.config import DB_FILE, HOME_DIR, ROOT, RUNNER_SOCKET, TASK_CACHE_DIR
from jarvis.core import platform
from jarvis.llm import claude_logins
from jarvis.safety.commands import JARVIS_SECRETS
from jarvis.safety.host import PS, _real, run

_SCP_REMOTE = re.compile(r"((?:[^@/:\s]+@)?[^@/:\s]+):")  # git's short SSH form, user@host:path


def _ssh_remote(url: str) -> str:
    """Where an SSH git remote signs in (ssh://user@host:port, or user@host), or '' for any other remote."""
    if re.match(r"(git\+)?ssh(\+git)?://", url):
        return "ssh://" + url.split("://", 1)[1].split("/", 1)[0]
    m = _SCP_REMOTE.match(url)
    return m[1] if m and "://" not in url else ""


def _git_hosts() -> list[str]:
    """Where the SSH git remotes of JARVIS's own repo and the user's projects on this computer sign in, each once."""
    try:
        folders = [ROOT, *(p.path for p in projects.load() if p.node is None)]
    except Exception:  # an unreadable project list leaves JARVIS's own repo
        folders = [ROOT]
    found: list[str] = []
    for folder in folders:
        try:
            urls = subprocess.run(["git", "-C", str(folder), "config", "--get-regexp", r"^remote\..*\.url$"],
                                  capture_output=True, text=True, timeout=10).stdout
        except (OSError, subprocess.TimeoutExpired):
            continue
        for line in urls.splitlines():
            if (where := _ssh_remote(line.partition(" ")[2].strip())) and where not in found:
                found.append(where)
    return found


def _ssh_checks() -> list[tuple[str, str, bool]]:
    """Signing in over SSH through the agent to each of the user's own git hosts; none when no remote uses SSH.
    ssh exits 255 only when it couldn't connect or sign in; otherwise with what the host's shell returned."""
    return [(f"signs in to {where} over SSH, through the agent",
             f"ssh -o BatchMode=yes -o ConnectTimeout=15 -T {shlex.quote(where)} </dev/null >/dev/null 2>&1; "
             "test $? -ne 255", True) for where in _git_hosts()]


def _home_write() -> list[tuple[str, str, bool]]:
    """Writing in JARVIS_HOME, when it isn't the checkout (checked just above)."""
    mark = HOME_DIR / ".host-check"
    return [] if HOME_DIR.resolve() == ROOT.resolve() else [
        ("writes in JARVIS_HOME", f"touch {mark} && rm {mark}", False)]


def _mac_checks() -> list[tuple[str, str, bool]]:  # (what, command, should succeed)
    return [
        ("reads JARVIS's .env", f"head -c1 {HOME_DIR / '.env'}", False),
        ("reads JARVIS's .env.example (a template)", f"head -c1 {ROOT / '.env.example'}", True),
        ("reads secrets/", f"ls {JARVIS_SECRETS[0]}", False),
        ("lists ~/.ssh", "ls ~/.ssh", False),
        ("reads an SSH key file", "head -c1 ~/.ssh/id_ed25519 || head -c1 ~/.ssh/id_rsa || head -c1 ~/.ssh/id_ecdsa", False),
        ("reaches the SSH agent", "ssh-add -l >/dev/null 2>&1; test $? -ne 2", True),
        *_ssh_checks(),
        ("reads the keychain files", "ls ~/Library/Keychains", False),
        ("writes tasks.db", f"touch {DB_FILE}", False),
        ("writes in JARVIS's folder", f"touch {ROOT / '.host-check'} && rm {ROOT / '.host-check'}", False),
        *_home_write(),
        ("reaches the runner's socket", f"curl -s --max-time 5 --unix-socket {RUNNER_SOCKET} http://runner/tasks", False),
        ("keeps JARVIS's keys out of its environment", 'test -z "$JARVIS_PASSCODE$LIVEKIT_API_SECRET"', True),
        ("reads other processes' environments (ps eww)", f"{PS} eww -p 1 >/dev/null", False),
        ("reads the voice session logs", f"ls {claude_logins.voice_logs()}", True),
        ("reads tasks.db", f"sqlite3 -readonly {DB_FILE} 'select count(*) from tasks'", True),
        ("writes the task cache", f"touch {TASK_CACHE_DIR / '.host-check'} && rm {TASK_CACHE_DIR / '.host-check'}", True),
        ("reaches PyPI over HTTPS", "curl -sI --max-time 20 https://pypi.org/simple/ | head -1 | grep -q 200", True),
        ("finds the code-signing identities", "security find-identity -v -p codesigning | grep -Eq '^ *[1-9][0-9]* valid'", True),
    ]


def _linux_checks() -> list[tuple[str, str, bool]]:  # (what, command, should succeed)
    # A hidden folder is an empty tmpfs and a hidden file /dev/null, so "blocked" means nothing seen, not an error
    db_read = (f"sqlite3 -readonly {DB_FILE} 'select count(*) from tasks' || python3 -c 'import sqlite3, sys; "
               f"sqlite3.connect(\"file:\" + sys.argv[1] + \"?mode=ro\", uri=True).execute(\"select count(*) from tasks\")' {DB_FILE}")
    return [
        ("reads JARVIS's .env", f"head -c1 {HOME_DIR / '.env'} | grep -q ''", False),
        ("reads JARVIS's .env.example (a template)", f"head -c1 {ROOT / '.env.example'} | grep -q ''", True),
        # The voice logs are bound back inside the hidden secrets/, so their parent folders show (empty): files count
        ("sees a file in secrets/", f"find {JARVIS_SECRETS[0]} -type f -not -path "
                                    f"'{_real(claude_logins.voice_logs())}/*' | grep -q .", False),
        ("sees more of ~/.ssh than known_hosts and config", "ls -A ~/.ssh | grep -vxE 'known_hosts2?|config' | grep -q .",
         False),
        ("reads an SSH key file", "head -c1 ~/.ssh/id_ed25519 || head -c1 ~/.ssh/id_rsa || head -c1 ~/.ssh/id_ecdsa", False),
        ("reaches the SSH agent", "ssh-add -l >/dev/null 2>&1; test $? -ne 2", True),
        *_ssh_checks(),
        ("sees the keyring files", "ls -A ~/.local/share/keyrings | grep -q .", False),
        ("reaches the session bus (Secret Service, systemd-run)", 'test -S "${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/bus"',
         False),
        ("writes tasks.db", f"touch {DB_FILE}", False),
        ("writes in JARVIS's folder", f"touch {ROOT / '.host-check'} && rm {ROOT / '.host-check'}", False),
        *_home_write(),
        ("creates a file at the top of home (a shell start-up file)", "touch ~/.host-check && rm ~/.host-check", False),
        ("writes systemd user units", ("mkdir -p ~/.config/systemd/user && touch ~/.config/systemd/user/.host-check "
                                       "&& rm ~/.config/systemd/user/.host-check"), False),
        ("reaches the runner's socket", f"curl -s --max-time 5 --unix-socket {RUNNER_SOCKET} http://runner/tasks", False),
        ("keeps JARVIS's keys out of its environment", 'test -z "$JARVIS_PASSCODE$LIVEKIT_API_SECRET"', True),
        ("reads other processes' environments (/proc)", f"head -c1 /proc/{os.getpid()}/environ | grep -q ''", False),
        ("gains root through sudo", "sudo -n true", False),
        ("reads the voice session logs", f"ls {claude_logins.voice_logs()}", True),
        ("reads tasks.db", db_read, True),
        ("writes the task cache", f"touch {TASK_CACHE_DIR / '.host-check'} && rm {TASK_CACHE_DIR / '.host-check'}", True),
        ("reaches PyPI over HTTPS", "curl -sI --max-time 20 https://pypi.org/simple/ | head -1 | grep -q 200", True),
    ]


def _checks() -> list[tuple[str, str, bool]]:
    return _mac_checks() if platform.MAC else _linux_checks()


async def check() -> bool:
    """Run each check under the profile; True if all behave as expected."""
    good = True
    for what, command, should in _checks():
        try:
            ran = await run(command, TASK_CACHE_DIR if TASK_CACHE_DIR.is_dir() else Path.home(), timeout=60)
        except RuntimeError as e:  # the walls can't go up here
            print(f"FAIL  {e}")
            return False
        worked = ran.code == 0
        ok = worked == should
        good &= ok
        print(f"{'ok  ' if ok else 'FAIL'}  {what}: {'works' if worked else 'blocked'}"
              + ("" if ok else f" (expected {'works' if should else 'blocked'})\n      {ran.output.strip()[:300]}"))
    return good


if __name__ == "__main__":
    if sys.argv[1:] != ["check"]:
        raise SystemExit(__doc__)
    TASK_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    raise SystemExit(0 if asyncio.run(check()) else 1)
