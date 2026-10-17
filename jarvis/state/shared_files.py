"""Generated files the phone may fetch (e.g. rendered audio), by random token, served at /api/files/TOKEN.

Tokens map to paths in the profile's shared_files.json for SHARE_DAYS. Links are sent only in JARVIS's own pushes;
only shared paths are reachable, and unknown or expired tokens find nothing.
"""

import json
import secrets
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlencode

from jarvis.core.files import locked, write_atomic
from jarvis.storage import profile

SHARE_DAYS = 7
TOKEN = r"[A-Za-z0-9_-]{32,64}"  # must match the server route


@contextmanager
def _changing(file: Path):
    """Yield the live share map for editing, saved on exit. File-locked: multiple processes write it."""
    file.parent.mkdir(parents=True, exist_ok=True)
    with locked(file):
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
    """Share `path`; return its token."""
    token = secrets.token_urlsafe(32)
    with _changing(file or profile.current().shared_files) as shared:
        shared[token] = {"path": str(Path(path).resolve()), "shared": time.time()}
    return token


def shared(token: str, file: Path | None = None) -> Path | None:
    """Path for a live token whose file still exists, else None."""
    found = _live(_load(file or profile.current().shared_files)).get(token)
    if not found:
        return None
    path = Path(found["path"])
    return path if path.is_file() else None


def link(token: str, name: str) -> str:
    """Web-app URL for a push about a shared file; the page shows it (and plays audio)."""
    return "/?" + urlencode({"file": token, "name": name})
