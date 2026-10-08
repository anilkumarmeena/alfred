"""Files JARVIS made that the phone may fetch, such as an audio file read aloud (jarvis.voice.audio_file).

Each shared file gets a long random token, kept with its path in data/shared_files.json for SHARE_DAYS. The web app
serves it at /api/files/TOKEN (jarvis.server) to whoever has the link, which only ever travels in JARVIS's own
pushes to the user's devices; nothing else on the Mac can be fetched that way, and an unknown or expired token finds
nothing.
"""

import fcntl
import json
import secrets
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlencode

from jarvis.core import profile
from jarvis.core.files import write_atomic

SHARE_DAYS = 7
TOKEN = r"[A-Za-z0-9_-]{32,64}"  # as the server's route matches it


@contextmanager
def _changing(file: Path):
    """The shared files, without the expired ones, to change; saved on the way out. Locked, as two processes write it."""
    file.parent.mkdir(parents=True, exist_ok=True)
    with open(file.with_name(f".{file.name}.lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        shared = _live(_load(file))
        yield shared
        write_atomic(file, json.dumps(shared, indent=1), mode=0o600)


def _load(file: Path) -> dict:
    try:
        found = json.loads(file.read_text())
    except (OSError, ValueError):
        return {}
    return found if isinstance(found, dict) else {}


def _live(shared: dict, now: float | None = None) -> dict:
    now = time.time() if now is None else now
    return {t: f for t, f in shared.items()
            if isinstance(f, dict) and now - f.get("shared", 0) < SHARE_DAYS * 86400}


def share(path: Path, file: Path | None = None) -> str:
    """Share `path` with the user's devices; returns its token."""
    token = secrets.token_urlsafe(32)
    with _changing(file or profile.current().shared_files) as shared:
        shared[token] = {"path": str(Path(path).resolve()), "shared": time.time()}
    return token


def shared(token: str, file: Path | None = None) -> Path | None:
    """The file a token shares, while it's shared and still there; else None."""
    found = _live(_load(file or profile.current().shared_files)).get(token)
    if not found:
        return None
    path = Path(found["path"])
    return path if path.is_file() else None


def link(token: str, name: str) -> str:
    """Where tapping a push about a shared file opens JARVIS: the page shows the file `name`, and plays it if it's
    audio."""
    return "/?" + urlencode({"file": token, "name": name})
