"""JARVIS's own keys: the LiveKit API key and secret, the APNs auth key's id and the Google OAuth client. They're kept
encrypted in the vault (jarvis.core.vault) and entered in the control panel (Settings → Logins and keys), never by
voice; there they're only ever written, and shown back as set or not. Logs name which one changed, never what to.

Until the vault has one, its variable in .env stands in (for the Google client, the JSON file GOOGLE_CLIENT_SECRET
names, or a client_secret*.json in secrets/: config.GOOGLE_CLIENT_SECRET), which is how a first setup starts:
jarvis.setup_local writes the LiveKit keys into .env. adopt(), as the web service starts and at the end of setup,
then moves each into the vault and takes its line out of .env. When the vault can't be opened (its master key is
missing or wrong), .env's stays in use, so a broken key never locks JARVIS out.
"""

import json
import logging
import os
import re
import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from jarvis.core import config, vault
from jarvis.core.files import write_atomic

log = logging.getLogger("jarvis.core.credentials")

MOST = 8000  # characters of a value entered in the panel: the Google client's JSON is about 400


@dataclass(frozen=True)
class Credential:
    name: str  # its secret's name in the vault
    var: str  # the .env variable that stands in until the vault has it
    label: str
    pattern: str  # what a value entered in the panel must look like (the Google client's is JSON, checked apart)
    hint: str  # what it is and where it comes from, shown in the panel
    required: bool = False  # JARVIS can't take calls without it, so it can be replaced but not cleared

    def __repr__(self) -> str:
        return f"Credential({self.name})"


LIVEKIT_KEY = Credential("livekit.api_key", "LIVEKIT_API_KEY", "LiveKit API key", r"[A-Za-z0-9_-]{3,64}",
                         "The local LiveKit server's key, made by make setup. Letters, digits, - and _.", True)
LIVEKIT_SECRET = Credential("livekit.api_secret", "LIVEKIT_API_SECRET", "LiveKit API secret",
                            r"[A-Za-z0-9_-]{32,128}",
                            "Signs the phone's call tokens. 32 or more letters, digits, - and _.", True)
APNS_KEY_ID = Credential("apns.key_id", "JARVIS_APNS_KEY_ID", "APNs key ID", r"[A-Z0-9]{10}",
                         "The 10-character ID of the APNs auth key in secrets/apns_key.p8, from developer.apple.com.")
GOOGLE_CLIENT = Credential("google.client", "GOOGLE_CLIENT_SECRET", "Google OAuth client", "",
                           "The Desktop app client's JSON from Google Cloud (APIs & Services → Credentials), pasted "
                           "whole. Used when a Google account signs in on the Mac.")
ALL = {c.name: c for c in (LIVEKIT_KEY, LIVEKIT_SECRET, APNS_KEY_ID, GOOGLE_CLIENT)}
VARS = [c.var for c in ALL.values()]  # kept out of every session's and command's environment (jarvis.safety.host)


def need(name: str) -> Credential:
    if name not in ALL:
        raise ValueError(f"JARVIS keeps no key called {name}.")
    return ALL[name]


def _from_env(c: Credential) -> str:
    """What stands in for the credential until the vault has it: its variable, or the Google client's file."""
    if c is not GOOGLE_CLIENT:
        return os.environ.get(c.var, "").strip()
    try:
        return Path(config.GOOGLE_CLIENT_SECRET).expanduser().read_text().strip() if config.GOOGLE_CLIENT_SECRET else ""
    except OSError:
        return ""


def _kept(c: Credential) -> str:
    """The vault's value, or "" when it has none or can't be opened (said in the log, which names only the key)."""
    try:
        return vault.get(c.name) if vault.has(c.name) else ""
    except vault.VaultError as e:
        log.warning("%s can't come from the vault, so .env's is used: %s", c.label, e)
        return ""


def get(c: Credential) -> str:
    """The credential: the vault's, else what .env has; "" when neither has it."""
    return _kept(c) or _from_env(c)


def source(c: Credential) -> str:
    """Where it comes from: "vault", ".env" (or the Google client's file, "file"), or "" when it isn't set."""
    if vault.has(c.name):
        return "vault"
    if _from_env(c):
        return "file" if c is GOOGLE_CLIENT else ".env"
    return ""


def _checked(c: Credential, value: str) -> str:
    """The value as it's kept, or ValueError saying what's wrong with it, never the value itself."""
    value = value.strip()
    if not value or len(value) > MOST:
        raise ValueError(f"Paste the {c.label}.")
    if c is not GOOGLE_CLIENT:
        if not re.fullmatch(c.pattern, value):
            raise ValueError(f"That isn't a {c.label}. {c.hint}")
        return value
    try:
        found = json.loads(value)
    except ValueError:
        raise ValueError("That isn't the client's JSON: paste the whole file Google Cloud gives.") from None
    client = next((found[k] for k in ("installed", "web") if isinstance(found, dict) and isinstance(found.get(k), dict)),
                  {})
    if not (client.get("client_id") and client.get("client_secret")):
        raise ValueError("That JSON isn't an OAuth client: it needs a client_id and a client_secret.")
    return json.dumps(found, separators=(",", ":"))


def put(c: Credential, value: str) -> None:
    """Keeps the credential in the vault, making the master key if there's none. ValueError says why it can't."""
    value = _checked(c, value)
    try:
        vault.ensure()
        vault.put(c.name, value)
    except vault.VaultError as e:
        raise ValueError(str(e)) from None
    log.warning("%s set in the vault", c.label)  # which, never what


def clear(c: Credential) -> bool:
    """Forgets the vault's value: .env's, if it has one, is used again."""
    if c.required:
        raise ValueError(f"JARVIS can't take calls without its {c.label}: replace it instead.")
    return vault.remove(c.name)


def adopt() -> list[str]:
    """Moves each credential .env still holds into the vault, where the vault has none, and takes its line out of .env.
    Returns the labels of those moved. Leaves .env as it is when the vault can't take them. The Google client's file
    stays where it is, for the user to remove."""
    moved = []
    for c in ALL.values():
        if vault.has(c.name) or not (value := _from_env(c)):
            continue
        try:
            vault.ensure()
            vault.put(c.name, value)
        except vault.VaultError as e:
            log.warning("%s stays in .env for now: %s", c.label, e)
            continue
        moved.append(c)
    vault.unset_env([c.var for c in moved])
    return [c.label for c in moved]


@contextmanager
def google_client_file() -> Iterator[str]:
    """The Google OAuth client JSON's path, for a sign-in: the vault's, written for the while to a file in secrets/
    only the user can read, and removed after; else the file that stands in for it; "" with neither."""
    if not (kept := _kept(GOOGLE_CLIENT)):
        yield config.GOOGLE_CLIENT_SECRET
        return
    path = config.SECRETS_DIR / f".google-client-{secrets.token_hex(4)}.json"
    write_atomic(path, kept, mode=0o600)
    try:
        yield str(path)
    finally:
        path.unlink(missing_ok=True)
