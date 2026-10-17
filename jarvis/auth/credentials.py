"""JARVIS's own keys (LiveKit key/secret, APNs key id, Google OAuth client), encrypted in the vault.

Entered only in the control panel, never by voice; write-only there (shown as set/unset). Logs name the key, never
the value. Until the vault has one, the .env variable (or the Google client JSON file) is used; adopt() moves them
into the vault and strips .env. If the vault can't be opened, .env stays in use so a bad master key never locks out.
"""

import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path

from jarvis import config
from jarvis.auth import vault

log = logging.getLogger("jarvis.auth.credentials")

MOST = 8000  # max chars entered in the panel (Google client JSON is ~400)


@dataclass(frozen=True)
class Credential:
    name: str  # vault secret name
    var: str  # .env fallback variable
    label: str
    pattern: str  # regex for panel input (Google client is JSON, checked separately)
    hint: str  # shown in the panel
    required: bool = False  # needed for calls: replaceable, not clearable

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
                           "The OAuth client's JSON from Google Cloud (Desktop app or Web application), pasted whole. "
                           "Used when a Google account signs in: JARVIS → Google accounts.")
ALL = {c.name: c for c in (LIVEKIT_KEY, LIVEKIT_SECRET, APNS_KEY_ID, GOOGLE_CLIENT)}
VARS = [c.var for c in ALL.values()]  # stripped from every session/command env


def need(name: str) -> Credential:
    if name not in ALL:
        raise ValueError(f"JARVIS keeps no key called {name}.")
    return ALL[name]


def _from_env(c: Credential) -> str:
    """Fallback value: the .env variable, or the Google client file's contents."""
    if c is not GOOGLE_CLIENT:
        return os.environ.get(c.var, "").strip()
    try:
        return Path(config.GOOGLE_CLIENT_SECRET).expanduser().read_text().strip() if config.GOOGLE_CLIENT_SECRET else ""
    except OSError:
        return ""


def _kept(c: Credential) -> str:
    """Vault value; "" if absent or the vault can't be opened (logged by key name only)."""
    try:
        return vault.get(c.name) if vault.has(c.name) else ""
    except vault.VaultError as e:
        log.warning("%s can't come from the vault, so .env's is used: %s", c.label, e)
        return ""


def get(c: Credential) -> str:
    """Vault value, else the .env fallback, else ""."""
    return _kept(c) or _from_env(c)


def source(c: Credential) -> str:
    """"vault", ".env", "file" (Google client file) or "" if unset."""
    if vault.has(c.name):
        return "vault"
    if _from_env(c):
        return "file" if c is GOOGLE_CLIENT else ".env"
    return ""


def _checked(c: Credential, value: str) -> str:
    """Normalised value; ValueError explains the problem without echoing the value."""
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
    """Store in the vault, creating the master key if needed. Raises ValueError on failure."""
    value = _checked(c, value)
    try:
        vault.ensure()
        vault.put(c.name, value)
    except vault.VaultError as e:
        raise ValueError(str(e)) from None
    log.warning("%s set in the vault", c.label)  # name only, never the value


def clear(c: Credential) -> bool:
    """Remove the vault value; any .env fallback applies again."""
    if c.required:
        raise ValueError(f"JARVIS can't take calls without its {c.label}: replace it instead.")
    return vault.remove(c.name)


def adopt() -> list[str]:
    """Move .env credentials the vault lacks into the vault and strip them from .env; return moved labels.

    On vault failure .env is left as is. The Google client file is never deleted."""
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


def google_client() -> dict:
    """The Google OAuth client's JSON (vault, else the fallback file), checked; {} when none is set. Raises
    ValueError, without the value, when it isn't a client."""
    value = get(GOOGLE_CLIENT)
    return json.loads(_checked(GOOGLE_CLIENT, value)) if value else {}
