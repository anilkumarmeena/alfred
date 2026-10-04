"""Secrets JARVIS keeps for its MCP servers (tokens and the like) and for itself (its LiveKit keys and the like:
jarvis.core.credentials), encrypted in the secrets table of data/tasks.db.

Each secret is AES-256-GCM under the master key, JARVIS_MASTER_KEY: 32 random bytes, base64url, always from the
environment, which on the Mac means .env. A blob is a 12-byte nonce then the ciphertext, and the secret's name and the
key's id are bound in as associated data, so a blob copied under another name, or kept with the wrong key id, won't
open. The key id is the first 8 bytes of the key's SHA-256, in hex: enough to tell keys apart, nothing to go on.
A settings row, vault_check, holds a known text encrypted the same way, so a wrong key is caught before it's used.

The key may be "new,old,...": secrets are written under the first and read under whichever their key id names, which
is how a key is rotated. JARVIS's own processes take the key out of their environment as config loads
(config.MASTER_KEY), and the CLI sessions get it blanked, so no session, shell command or MCP server sees it. Only
jarvis.integrations.mcp_launch decrypts a server's secrets, just before it starts that server; it reads the key from .env, since
the CLI that starts it has none.

    uv run python -m jarvis.core.vault              what's kept: the key's id and the secrets' names, never a value
    uv run python -m jarvis.core.vault init         make the key and put it in .env (mode 600), or --print it once
    uv run python -m jarvis.core.vault rotate       a new key first in .env, the old kept after it; everything re-encrypted
    uv run python -m jarvis.core.vault reencrypt    everything under the first key; --drop-old then takes the rest out of .env
    uv run python -m jarvis.core.vault reset --yes  for a lost key: forget every secret and start a new key
"""

import argparse
import base64
import binascii
import hashlib
import logging
import os
import re
import sqlite3
import sys
import time
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from dotenv import dotenv_values

from jarvis.core import config, database
from jarvis.core.config import ROOT
from jarvis.core.files import write_atomic
from jarvis.state import settings

log = logging.getLogger("jarvis.core.vault")

VAR = "JARVIS_MASTER_KEY"
ENV_FILE = ROOT / ".env"  # where the key lives on the Mac; the tests point it elsewhere
CHECK = "vault_check"  # the settings row that tells a wrong key from the right one
_CHECK_TEXT = b"JARVIS vault check"
NAME = re.compile(r"[a-z0-9][a-z0-9_.-]{0,63}")  # a secret's name, e.g. github.token

_SCHEMA = """
create table secrets (
    name text primary key,
    blob blob not null,                  -- 12-byte nonce, then AES-256-GCM ciphertext and tag
    key_id text not null,                -- which key it's under; with the name, the associated data
    created real not null,
    updated real not null
)"""


class VaultError(Exception):
    """Something the user has to fix: no key, a wrong key, a secret missing."""


@dataclass(frozen=True)
class Key:
    id: str
    secret: bytes = b""

    def __repr__(self) -> str:  # never the key itself, in a log or a traceback
        return f"Key({self.id})"


def _encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def new_key() -> str:
    return _encode(os.urandom(32))


def key_id(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()[:16]


def parse(text: str) -> list[Key]:
    """The keys in a JARVIS_MASTER_KEY value, the newest first. Raises VaultError for one that isn't 32 bytes."""
    keys = []
    for n, part in enumerate(p.strip() for p in text.split(",") if p.strip()):
        try:
            raw = base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))
        except (binascii.Error, ValueError):
            raw = b""
        if len(raw) != 32:
            raise VaultError(f"Key {n + 1} in {VAR} isn't 32 bytes of base64url. Make one with: make vault ARGS=init")
        keys.append(Key(key_id(raw), raw))
    return keys


def _env_file_value() -> str:
    try:
        return (dotenv_values(ENV_FILE).get(VAR) or "").strip()
    except OSError:
        return ""


def keys() -> list[Key]:
    """The master keys: from the environment as config found it, or else .env (as jarvis.integrations.mcp_launch needs)."""
    return parse(config.MASTER_KEY or _env_file_value())


def _aad(name: str, kid: str) -> bytes:
    return f"{name}\0{kid}".encode()


def encrypt(name: str, value: bytes, key: Key) -> bytes:
    nonce = os.urandom(12)
    return nonce + AESGCM(key.secret).encrypt(nonce, value, _aad(name, key.id))


def decrypt(name: str, blob: bytes, kid: str, found: list[Key]) -> bytes:
    key = next((k for k in found if k.id == kid), None)
    if key is None:
        raise VaultError(f"The secret {name} is under key {kid}, which isn't in {VAR}. Put that key back (after the "
                         "current one), or set the secret again.")
    try:
        return AESGCM(key.secret).decrypt(blob[:12], blob[12:], _aad(name, kid))
    except InvalidTag:
        raise VaultError(f"The secret {name} won't open with key {kid}: it was changed or copied.") from None


# ── The table ───────────────────────────────────────────────────

def _db() -> AbstractContextManager[sqlite3.Connection]:
    return database.table("secrets", _SCHEMA)


def _checked(found: list[Key]) -> list[Key]:
    """The keys, once vault_check opens with one of them (or written now, with none yet). Raises VaultError without a
    key, or when it doesn't open."""
    if not found:
        raise VaultError(f"No {VAR} is set. Make one with: make vault ARGS=init")
    stored = settings.get(CHECK)
    if not stored:
        settings.put(CHECK, _check_row(found[0]))
        return found
    kid, _, blob = stored.partition(":")
    try:
        decrypt(CHECK, base64.b64decode(blob), kid, found)
    except (VaultError, binascii.Error, ValueError):
        raise VaultError(f"{VAR} isn't the key the secrets were kept with (key {kid}). Put that key back, or start "
                         "again with: make vault ARGS='reset --yes'") from None
    return found


def verify() -> list[Key]:
    """The master keys, checked against the secrets kept with them. Raises VaultError, saying what's wrong."""
    return _checked(keys())


def _check_row(key: Key) -> str:
    return f"{key.id}:{base64.b64encode(encrypt(CHECK, _CHECK_TEXT, key)).decode()}"


def put(name: str, value: str) -> None:
    """Keeps `value` as the secret `name`, under the newest key."""
    if not NAME.fullmatch(name):
        raise VaultError(f"'{name}' can't name a secret: lowercase letters, digits, dots, dashes and underscores.")
    if not value:
        raise VaultError("The secret is empty.")
    key = _checked(keys())[0]
    now = time.time()
    with _db() as db:
        db.execute("insert into secrets values (?, ?, ?, ?, ?) on conflict(name) do update set blob = excluded.blob, "
                   "key_id = excluded.key_id, updated = excluded.updated",
                   (name, encrypt(name, value.encode(), key), key.id, now, now))


def get(name: str) -> str:
    """The secret `name`. Raises VaultError if it isn't kept or won't open."""
    found = _checked(keys())
    with _db() as db:
        row = db.execute("select blob, key_id from secrets where name = ?", (name,)).fetchone()
    if row is None:
        raise VaultError(f"There's no secret {name}. Enter it in the control panel's Connections page, or on the "
                         "Mac: make servers ARGS='set-secret SERVER'")
    return decrypt(name, row[0], row[1], found).decode()


def has(name: str) -> bool:
    with _db() as db:
        return db.execute("select 1 from secrets where name = ?", (name,)).fetchone() is not None


def names() -> dict[str, str]:
    """Each secret's name, with the id of the key it's under."""
    with _db() as db:
        return dict(db.execute("select name, key_id from secrets order by name").fetchall())


def remove(name: str) -> bool:
    with _db() as db:
        return db.execute("delete from secrets where name = ?", (name,)).rowcount > 0


def reencrypt(found: list[Key] | None = None) -> int:
    """Every secret, and the check, under the first key; returns how many secrets moved. All or nothing."""
    found = _checked(found or keys())
    newest = found[0]
    with _db() as db:
        db.execute("begin immediate")
        moved = 0
        for name, blob, kid in db.execute("select name, blob, key_id from secrets").fetchall():
            if kid != newest.id:
                value = decrypt(name, blob, kid, found)
                db.execute("update secrets set blob = ?, key_id = ?, updated = ? where name = ?",
                           (encrypt(name, value, newest), newest.id, time.time(), name))
                moved += 1
        db.execute("commit")
    settings.put(CHECK, _check_row(newest))
    return moved


def reset() -> list[str]:
    """Forgets every secret and the check, for a key that's lost. Returns the secrets' names."""
    with _db() as db:
        gone = [r[0] for r in db.execute("select name from secrets order by name")]
        db.execute("delete from secrets")
    settings.put(CHECK, "")
    return gone


# ── The key in .env ─────────────────────────────────────────────

def _rewrite_env(drop: list[str], add: str = "", path: Path | None = None) -> None:
    """.env without the lines setting the variables in `drop`, then the line `add`, atomically and mode 600."""
    path = path or ENV_FILE
    try:
        lines = path.read_text().splitlines()
    except FileNotFoundError:
        lines = []
    setting = re.compile(rf"\s*(export\s+)?({'|'.join(map(re.escape, drop))})\s*=")
    lines = [l for l in lines if not setting.match(l)] + ([add] if add else [])
    write_atomic(path, "\n".join(lines) + "\n", mode=0o600)


def write_key(value: str, path: Path | None = None) -> None:
    """Sets JARVIS_MASTER_KEY in .env, replacing any line for it and keeping the rest, atomically and mode 600."""
    _rewrite_env([VAR], f"{VAR}={value}", path)
    config.MASTER_KEY = value  # this process uses it from now on


def unset_env(names: list[str]) -> None:
    """Takes the variables `names` out of .env, and out of this process's environment, once the vault holds what they
    held. The rest of .env is kept as it is."""
    if names and ENV_FILE.exists():
        _rewrite_env(names)
    for name in names:
        os.environ.pop(name, None)


def init(print_only: bool = False) -> str:
    """Makes the master key, unless there is one; returns what to tell the user. The key goes into .env, or with
    `print_only` is printed once, for an environment set up some other way."""
    if found := keys():
        _checked(found)
        return f"There's a master key already (key {found[0].id})."
    value = new_key()
    if print_only:
        config.MASTER_KEY = value
        _checked(keys())
        return (f"Set this in JARVIS's environment as {VAR}; it isn't kept anywhere else and won't be shown again:\n"
                f"{value}")
    write_key(value)
    _checked(keys())
    return f"Made the master key (key {keys()[0].id}) and put it in {ENV_FILE.name} as {VAR}."


def ensure() -> None:
    """A master key, made now (into .env) if there's none. Raises VaultError if it can't be."""
    if not keys():
        log.warning(init())


def rotate() -> str:
    found = _checked(keys())
    value = ",".join([new_key(), *(_encode(k.secret) for k in found)])
    fresh = parse(value)
    write_key(value)  # first, with the old keys still after the new: if this fails, nothing has moved
    moved = reencrypt(fresh)
    return (f"New key {fresh[0].id} first in {VAR}; {moved} secret{'s' * (moved != 1)} re-encrypted. The old key stays "
            "after it until: make vault ARGS='reencrypt --drop-old'")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m jarvis.core.vault", description="JARVIS's encrypted secrets.")
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("status", help="the key's id and the secrets' names (the default)")
    init_ = commands.add_parser("init", help="make the master key")
    init_.add_argument("--print", action="store_true", help="print it once instead of writing it into .env")
    commands.add_parser("rotate", help="a new master key; everything re-encrypted under it")
    again = commands.add_parser("reencrypt", help="everything under the first key")
    again.add_argument("--drop-old", action="store_true", help="then take the other keys out of .env")
    reset_ = commands.add_parser("reset", help="forget every secret and make a new key, for a lost key")
    reset_.add_argument("--yes", action="store_true")
    args = parser.parse_args(argv)
    try:
        command = args.command or "status"
        if command == "status":
            found = keys()
            if found:
                _checked(found)
            print(f"Master key: {', '.join(k.id for k in found) if found else 'none (make vault ARGS=init)'}")
            kept = names()
            print("Secrets:" if kept else "No secrets kept.")
            for name, kid in kept.items():
                print(f"  {name}  (key {kid})")
        elif command == "init":
            print(init(args.print))
        elif command == "rotate":
            print(rotate())
        elif command == "reencrypt":
            moved = reencrypt()
            print(f"{moved} secret{'s' * (moved != 1)} re-encrypted under key {keys()[0].id}.")
            if args.drop_old and len(keys()) > 1:
                write_key(_encode(keys()[0].secret))
                print(f"Only key {keys()[0].id} is left in {VAR}.")
        elif command == "reset":
            if not args.yes:
                sys.exit("This forgets every secret for good. Run it again with --yes.")
            write_key(new_key())  # first: if .env can't take it, nothing is forgotten
            gone = reset()
            _checked(keys())
            print(f"Forgot {', '.join(gone) or 'nothing'}; new key {keys()[0].id}. Set the servers' secrets again "
                  "with: make servers ARGS='set-secret SERVER'")
    except VaultError as e:
        sys.exit(str(e))
    return 0


if __name__ == "__main__":
    sys.exit(main())
