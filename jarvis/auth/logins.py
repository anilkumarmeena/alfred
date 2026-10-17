"""Phone/browser logins: name + passcode, checked against salted scrypt hashes in secrets/logins.json (0600).

The passcode alone identifies the login, so passcodes are unique. Managed only in the control panel, never by voice;
every login reaches the whole panel, but only the owner's login (the first one made, marked "owner") pairs machines
and changes or removes the owner's login. adopt() turns JARVIS_PASSCODE from .env into the owner's login and strips
it from .env. With no logins, JARVIS_PASSCODE is checked directly so nobody is locked out.

Each passcode is also kept encrypted in the vault, so `make passcode` can show it on this computer (whoever can
run that can read .env and the vault key anyway). Nothing served over HTTP ever returns it.

usage: python -m jarvis.auth.logins      show each login and its passcode
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

from jarvis import config
from jarvis.auth import vault
from jarvis.core.files import write_atomic
from jarvis.storage import profile

log = logging.getLogger("jarvis.auth.logins")

VAR = "JARVIS_PASSCODE"
FILE = config.LOGINS_FILE  # overridden by tests
NAME = re.compile(r"[A-Za-z][A-Za-z0-9 ._'-]{0,31}")
SHORTEST, LONGEST = 6, 128  # passcode length bounds
_SCRYPT = {"n": 2**14, "r": 8, "p": 1}  # 16 MB, ~50 ms per check
_MAXMEM = 64 * 1024 * 1024
USED_EVERY = 300  # seconds between writes of a login's last use
_lock = threading.Lock()
# Cache of verified (hash, passcode) pairs as HMACs under a per-process key, so repeat requests skip scrypt.
# A passcode change alters the hash, invalidating the entry.
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
    """Name for the first login: the owner's profile name if known and valid, else "Owner"."""
    name = profile.owner().name
    known = name != "the user"  # profile placeholder for an unknown name
    return name if known and NAME.fullmatch(name) else "Owner"


def check(passcode: str) -> str | None:
    """Name of the login with this passcode, or None. With no logins, checks JARVIS_PASSCODE."""
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
    """Whether any login (or JARVIS_PASSCODE) exists."""
    return bool(_read() or os.environ.get(VAR, "").strip())


def _owner_row(rows: list[dict]) -> dict | None:
    """The row marked as the owner's, else the oldest (logins made before the mark existed)."""
    return next((r for r in rows if r.get("owner")), None) or min(rows, key=lambda r: r["created"], default=None)


def owner() -> str:
    """The owner's login name: the first login made (with none yet, the name JARVIS_PASSCODE is checked as)."""
    found = _owner_row(_read())
    return found["name"] if found else first_name()


def is_owner(login: str) -> bool:
    return bool(login) and login.lower() == owner().lower()


def owner_only(login: str, what: str) -> None:
    """PermissionError unless `login` is the owner's: some things only the owner may do, even from the panel."""
    if not is_owner(login):
        raise PermissionError(f"Only {owner()}'s login, the owner's, can {what}.")


def listed() -> list[dict]:
    """Logins, oldest first: name, created, last used, whether it's the owner's (never the hash)."""
    rows = _read()
    mine = _owner_row(rows)
    return [{"name": r["name"], "created": r["created"], "used": r.get("used"), "owner": r is mine} for r in rows]


def _secret(name: str) -> str:
    """Vault name for a login's passcode copy (login names are case-insensitive and may hold spaces)."""
    return "login." + hashlib.sha256(name.strip().lower().encode()).hexdigest()[:16]


def _keep(name: str, passcode: str) -> None:
    """Keep the passcode encrypted in the vault for `make passcode`; without a vault key it's just not kept."""
    try:
        vault.put(_secret(name), passcode)
    except vault.VaultError as e:
        log.warning("login %s's passcode isn't kept for make passcode: %s", name, e)


def shown() -> list[tuple[str, str | None]]:
    """(name, passcode) for each login, oldest first; None where no copy is kept. For the terminal only."""
    out = []
    for row in _read():
        try:
            out.append((row["name"], vault.get(_secret(row["name"]))))
        except vault.VaultError:
            out.append((row["name"], None))
    return out


def _named(rows: list[dict], name: str) -> dict:
    found = next((r for r in rows if r["name"].lower() == name.strip().lower()), None)
    if found is None:
        raise ValueError(f"There's no login called {name}.")
    return found


def _new_passcode(rows: list[dict], passcode: str, mine: dict | None = None) -> str:
    """Hash a new passcode; ValueError if wrong length or already used by another login."""
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
        rows.append({"name": name, "hash": _new_passcode(rows, passcode), "created": time.time(), "used": None}
                    | ({} if rows else {"owner": True}))
        _write(rows)
    _keep(name, passcode)
    log.warning("login %s added", name)


def _guard_owner(rows: list[dict], row: dict, by: str | None, what: str) -> None:
    """Only the owner changes the owner's login. `by` is the login asking (None: this computer's command line)."""
    if by is not None and row is _owner_row(rows) and by.lower() != row["name"].lower():
        raise PermissionError(f"Only {row['name']} can {what} their own login: it's the owner's.")


def set_passcode(name: str, passcode: str, by: str | None = None) -> None:
    with _lock:
        rows = _read()
        row = _named(rows, name)
        _guard_owner(rows, row, by, "change the passcode of")
        row["hash"] = _new_passcode(rows, passcode, row)
        _write(rows)
    _keep(row["name"], passcode)
    log.warning("login %s has a new passcode", row["name"])


def remove(name: str, by: str | None = None) -> None:
    """Remove a login. When the owner removes their own, the oldest login left becomes the owner's."""
    with _lock:
        rows = _read()
        row = _named(rows, name)
        _guard_owner(rows, row, by, "remove")
        if len(rows) == 1:
            raise ValueError("That's the only login: add another first, or nobody could connect.")
        _write([r for r in rows if r is not row])
    vault.remove(_secret(row["name"]))
    log.warning("login %s removed", row["name"])


def used(name: str) -> None:
    """Record a login's use, writing at most every USED_EVERY seconds."""
    now = time.time()
    with _lock:
        rows = _read()
        row = next((r for r in rows if r["name"] == name), None)
        if row and now - (row.get("used") or 0) >= USED_EVERY:
            row["used"] = now
            _write(rows)


def adopt() -> str:
    """If there are no logins, make JARVIS_PASSCODE the owner's and strip it from .env; return the name or ""."""
    passcode, name = os.environ.get(VAR, "").strip(), first_name()
    with _lock:
        if _read() or not passcode:
            return ""
        # Skip length rules: existing devices already use this passcode
        _write([{"name": name, "hash": hashed(passcode), "created": time.time(), "used": None, "owner": True}])
    _keep(name, passcode)
    vault.unset_env([VAR])
    log.warning("the passcode in .env is now %s's login", name)
    return name


def main() -> int:
    found = shown()
    if not found:
        print("No logins yet: make setup makes the first one.")
        return 1
    for name, passcode in found:
        print(f"{name}: {passcode}" if passcode else
              f"{name}: not kept (set before passcodes were kept, or with no vault key). Set a new one in the "
              "control panel, or if it's the only login: rm secrets/logins.json && make setup")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
