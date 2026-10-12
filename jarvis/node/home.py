"""A machine's own files, when this checkout runs as a node (python -m jarvis.node).

Private ones live in secrets/node/, so the same walls that keep tasks out of secrets/ on the main computer keep them
out here: its key, its pairing with the main computer, and its journal. Work copies of projects go in data/worktrees/
(writable by the tasks that work in them), the rest in data/node/.
"""

import json
import os
import shutil
import socket
import subprocess

import psutil

from jarvis.core import platform, proc
from jarvis.core.config import MAX_TASKS, SECRETS_DIR
from jarvis.core.files import write_atomic
from jarvis.node import keys
from jarvis.safety import commands, host

PRIVATE = SECRETS_DIR / "node"
KEY_FILE = PRIVATE / "node.key"
PAIRING_FILE = PRIVATE / "pairing.json"  # {"node", "main", "main_public", "name"}
JOURNAL_FILE = PRIVATE / "journal.db"
# Programs worth knowing a machine has, reported at pairing and on each online check
TOOLS = ("git", "uv", "python3", "node", "npm", "ffmpeg", "xcrun", "xcodebuild", "docker", "brew", "cargo", "go")


def key() -> str:
    """This machine's private key (PEM), made on first use (mode 600)."""
    if KEY_FILE.exists():
        return KEY_FILE.read_text()
    PRIVATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    made = keys.new()
    write_atomic(KEY_FILE, made, mode=0o600)
    return made


def pairing() -> dict | None:
    try:
        found = json.loads(PAIRING_FILE.read_text())
    except (OSError, ValueError):
        return None
    return found if isinstance(found, dict) and {"node", "main", "main_public"} <= found.keys() else None


def save_pairing(node: int, main: str, main_public: str, name: str) -> None:
    PRIVATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    write_atomic(PAIRING_FILE, json.dumps({"node": node, "main": main, "main_public": main_public, "name": name}),
                 mode=0o600)


def computer_name() -> str:
    """The name the computer goes by: its macOS Computer Name, else its host name."""
    if platform.MAC:
        try:
            found = subprocess.run(["scutil", "--get", "ComputerName"], capture_output=True, text=True, timeout=5)
            if found.returncode == 0 and found.stdout.strip():
                return found.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            pass
    return socket.gethostname().split(".")[0]


async def have(names: list[str]) -> list[str] | None:
    """Which of these programs a job here finds, looked up in a login shell as jobs run (`command -v`, so shell
    builtins count); None if that couldn't be told."""
    names = [n for n in dict.fromkeys(names) if commands.PROGRAM_NAME.fullmatch(n)][:30]
    if not names:
        return []
    script = "; ".join(f"command -v -- {n} >/dev/null 2>&1 && echo {n}" for n in names)
    try:
        _, out = await proc.run_try(*host.SHELL, script, timeout=4)  # inside the online check's 5 s
    except (OSError, TimeoutError):
        return None
    return [n for n in out.split() if n in names]


def facts(version: str = "") -> dict:
    """What this machine reports about itself."""
    return {"name": computer_name(), "host": socket.gethostname().split(".")[0], "os": platform.NAME,
            "cores": os.cpu_count() or 0, "ram_gb": round(psutil.virtual_memory().total / 1e9, 1),
            "tools": [t for t in TOOLS if shutil.which(t)], "max_tasks": MAX_TASKS, "version": version}
