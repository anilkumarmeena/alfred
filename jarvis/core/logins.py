"""Who may use JARVIS from the phone and browsers: each person's login, a name and a passcode of their own, kept in
secrets/logins.json (mode 600, out of every task's and command's reach like the rest of secrets/) as a salted scrypt
hash, never the passcode itself. jarvis.server checks every request's passcode here. The passcode alone says whose it
is, so no two logins share one, and the JARVIS app, which sends only a passcode, needs no change. Logins are added,
given new passcodes and removed in the control panel (Settings → Logins and keys), never by voice; every login can
reach the whole panel, as the one shared passcode could.

The first login comes from JARVIS_PASSCODE in .env: the passcode every device shared before, or the one
jarvis.setup_local makes. adopt(), as the web service starts and at the end of setup, makes it the login of the user
(the owner's profile name), so the devices already using it keep working, and takes it out of .env. With no login at all,
JARVIS_PASSCODE is checked as it is, so nobody is locked out.
"""

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import threading
import time

from jarvis.core import config, profile, vault
from jarvis.core.files import write_atomic

log = logging.getLogger("jarvis.core.logins")

VAR = "JARVIS_PASSCODE"
FILE = config.LOGINS_FILE  # the tests point it elsewhere
NAME = re.compile(r"[A-Za-z][A-Za-z0-9 ._'-]{0,31}")
SHORTEST, LONGEST = 6, 128  # a passcode's length
_SCRYPT = {"n": 2**14, "r": 8, "p": 1}  # 16 MB and about 50 ms a check
_MAXMEM = 64 * 1024 * 1024
USED_EVERY = 300  # seconds: how often a login's last use is written down
_lock = threading.Lock()
# Passcodes already checked, as an HMAC under this process's own key of the login's hash and the passcode, so a request
# after the first costs no scrypt; a new passcode changes the hash, and with it the entry
_pepper = os.urandom(32)
_known: set[str] = set()


def _read() -> list[dict]:
    try:
        return json.loads(FILE.read_text())["logins"]
    except FileNotFoundError:
        return []
    except (OSError, ValueError, KeyError, TypeError) as e:
        raise RuntimeError(f"{FILE.name} can't be read ({type(e).__name__}): fix or remove it, then make setup.") from None


def _write(rows: list[dict]) -> None:
    write_atomic(FILE, json.dumps({"logins": rows}, indent=1) + "\n", mode=0o600)


def hashed(passcode: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.scrypt(passcode.encode(), salt=salt, dklen=32, maxmem=_MAXMEM, **_SCRYPT)
    return "$".join(["scrypt", *(str(_SCRYPT[k]) for k in "nrp"), *(base64.b64encode(b).decode() for b in (salt, digest))])


def matches(stored: str, passcode: str) -> bool:
    try:
        kind, n, r, p, salt, digest = stored.split("$")
        want = base64.b64decode(digest)
        got = hashlib.scrypt(passcode.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p),
                             dklen=len(want), maxmem=_MAXMEM)
    except (ValueError, TypeError):
        return False
    return kind == "scrypt" and hmac.compare_digest(got, want)


def _memo(stored: str, passcode: str) -> str:
    return hmac.new(_pepper, f"{stored}\0{passcode}".encode(), hashlib.sha256).hexdigest()


def first_name() -> str:
    """The first login's name: the owner's (jarvis.core.profile), when JARVIS knows it and it can name one."""
    name = profile.owner().name
    known = name != "the user"  # the profile's stand-in for a name it doesn't know
    return name if known and NAME.fullmatch(name) else "Owner"


def check(passcode: str) -> str | None:
    """The name of the login whose passcode this is, or None. With no login yet, JARVIS_PASSCODE's, as the user's."""
    if not passcode:
        return None
    rows = _read()
    if not rows:
        fallback = os.environ.get(VAR, "").strip()
        return first_name() if fallback and hmac.compare_digest(passcode.encode(), fallback.encode()) else None
    if found := next((r["name"] for r in rows if _memo(r["hash"], passcode) in _known), None):
        return found
    for row in rows:
        if matches(row["hash"], passcode):
            _known.add(_memo(row["hash"], passcode))
            return row["name"]
    return None


def ready() -> bool:
    """Whether anyone can log in: a login, or JARVIS_PASSCODE for the first."""
    return bool(_read() or os.environ.get(VAR, "").strip())


def listed() -> list[dict]:
    """The logins, oldest first: name, created and last used, never the hash."""
    return [{"name": r["name"], "created": r["created"], "used": r.get("used")} for r in _read()]


def _named(rows: list[dict], name: str) -> dict:
    found = next((r for r in rows if r["name"].lower() == name.strip().lower()), None)
    if found is None:
        raise ValueError(f"There's no login called {name}.")
    return found


def _new_passcode(rows: list[dict], passcode: str, mine: dict | None = None) -> str:
    """The passcode's hash, or ValueError when it's too short or long, or another login's (the passcode says whose
    login it is)."""
    if not SHORTEST <= len(passcode) <= LONGEST or passcode != passcode.strip():
        raise ValueError(f"A passcode is {SHORTEST} to {LONGEST} characters, with no spaces at either end.")
    if any(matches(r["hash"], passcode) for r in rows if r is not mine):
        raise ValueError("Choose another passcode.")
    return hashed(passcode)


def add(name: str, passcode: str) -> None:
    name = name.strip()
    if not NAME.fullmatch(name):
        raise ValueError("A login's name starts with a letter: up to 32 letters, digits, spaces, dots, dashes and "
                         "apostrophes.")
    with _lock:
        rows = _read()
        if any(r["name"].lower() == name.lower() for r in rows):
            raise ValueError(f"There's a login called {name} already.")
        rows.append({"name": name, "hash": _new_passcode(rows, passcode), "created": time.time(), "used": None})
        _write(rows)
    log.warning("login %s added", name)


def set_passcode(name: str, passcode: str) -> None:
    with _lock:
        rows = _read()
        row = _named(rows, name)
        row["hash"] = _new_passcode(rows, passcode, row)
        _write(rows)
    log.warning("login %s has a new passcode", row["name"])


def remove(name: str) -> None:
    with _lock:
        rows = _read()
        row = _named(rows, name)
        if len(rows) == 1:
            raise ValueError("That's the only login: add another first, or nobody could connect.")
        _write([r for r in rows if r is not row])
    log.warning("login %s removed", row["name"])


def used(name: str) -> None:
    """Notes that the login was just used, at most every USED_EVERY seconds."""
    now = time.time()
    with _lock:
        rows = _read()
        row = next((r for r in rows if r["name"] == name), None)
        if row and now - (row.get("used") or 0) >= USED_EVERY:
            row["used"] = now
            _write(rows)


def adopt() -> str:
    """With no login yet, makes JARVIS_PASSCODE the user's login and takes it out of .env. Returns the login's name,
    or "" when there was nothing to do."""
    passcode, name = os.environ.get(VAR, "").strip(), first_name()
    with _lock:
        if _read() or not passcode:
            return ""
        # As it is, though shorter than a new one may be: it's the passcode the user's devices already have
        _write([{"name": name, "hash": hashed(passcode), "created": time.time(), "used": None}])
    vault.unset_env([VAR])
    log.warning("the passcode in .env is now %s's login", name)
    return name
