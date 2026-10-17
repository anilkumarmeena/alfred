"""APNs push for the native iOS app (its web view can't do Web Push); same alerts as jarvis.notify.push.

The app posts its device token to /api/push/native; tokens live in data/push_devices.json. Each push is an HTTP/2
request authenticated with an ES256 JWT from secrets/apns_key.p8, its key id (vault) and JARVIS_APNS_TEAM_ID.
Sandbox host for Xcode builds. A 410 or BadDeviceToken drops the device.
"""

import asyncio
import base64
import functools
import json
import logging
import re
import time
from pathlib import Path

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

from jarvis.auth import credentials
from jarvis.config import APNS_KEY_FILE, APNS_TEAM_ID
from jarvis.core.files import locked, write_atomic
from jarvis.notify.push import MAX_DEVICES, SEND_TIMEOUT, TTL
from jarvis.storage import profile

log = logging.getLogger("jarvis.notify.apns")
HOSTS = {"production": "https://api.push.apple.com", "development": "https://api.sandbox.push.apple.com"}
JWT_LIFETIME = 50 * 60  # APNs rejects tokens over 1 h old or refreshed more often than every 20 min
MAX_PAYLOAD = 4096  # bytes, APNs alert limit
_TOKEN = re.compile(r"^[0-9a-f]{64,200}$")
_TOPIC = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]{0,154}$")  # bundle id
GONE = {"BadDeviceToken", "Unregistered", "DeviceTokenNotForTopic"}
NOT_SET_UP = ("APNs isn't set up on this computer: put the APNs auth key in secrets/apns_key.p8, enter its key ID in "
              "the control panel (Settings → Logins and keys) and set JARVIS_APNS_TEAM_ID in .env, then restart JARVIS.")


def key_id() -> str:
    return credentials.get(credentials.APNS_KEY_ID)


def configured() -> bool:
    return bool(APNS_TEAM_ID and key_id()) and APNS_KEY_FILE.is_file()


# ── Devices ─────────────────────────────────────────────────────


def validate(info: object) -> dict:
    """Validate the app's registration: hex token, environment and bundle id (topic)."""
    if not isinstance(info, dict):
        raise ValueError("device must be an object")
    token = str(info.get("token", "")).lower()
    if not _TOKEN.match(token):
        raise ValueError("token is missing or malformed")
    environment = info.get("environment")
    if environment not in HOSTS:
        raise ValueError("environment must be development or production")
    topic = info.get("topic")
    if not isinstance(topic, str) or not _TOPIC.match(topic):
        raise ValueError("topic must be the app's bundle id")
    return {"token": token, "environment": environment, "topic": topic}


class Devices:
    """Registered app installs; JSON file shared by the web app and task runner."""

    validate = staticmethod(validate)  # subclasses (jarvis.notify.fcm) check their own token format

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or profile.current().push_devices

    def all(self) -> list[dict]:
        try:
            data = json.loads(self.path.read_text())
        except FileNotFoundError:
            return []
        except ValueError:
            log.warning("%s is unreadable; no app devices to push to", self.path.name)
            return []
        return [d for d in data if isinstance(d, dict) and "token" in d] if isinstance(data, list) else []

    def add(self, info: object, device: str = "") -> dict:
        """Store a device's token, replacing any earlier entry with the same token."""
        dev = self.validate(info) | {"device": device[:40], "added": time.time()}
        with locked(self.path):
            devices = [d for d in self.all() if d["token"] != dev["token"]]
            write_atomic(self.path, json.dumps((devices + [dev])[-MAX_DEVICES:], indent=1), mode=0o600)
        return dev

    def remove(self, tokens: set[str]) -> int:
        with locked(self.path):
            devices = self.all()
            keep = [d for d in devices if d["token"] not in tokens]
            if len(keep) != len(devices):
                write_atomic(self.path, json.dumps(keep, indent=1), mode=0o600)
        return len(devices) - len(keep)


# ── Provider token ──────────────────────────────────────────────


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


@functools.cache
def _signing_key(path: Path) -> ec.EllipticCurvePrivateKey:
    return serialization.load_pem_private_key(path.read_bytes(), password=None)


_jwt: tuple[float, str] | None = None


def provider_token(now: float | None = None) -> str:
    """ES256 provider JWT, cached for JWT_LIFETIME."""
    global _jwt
    now = time.time() if now is None else now
    if _jwt and now - _jwt[0] < JWT_LIFETIME:
        return _jwt[1]
    header = _b64(json.dumps({"alg": "ES256", "kid": key_id()}).encode())
    claims = _b64(json.dumps({"iss": APNS_TEAM_ID, "iat": int(now)}).encode())
    der = _signing_key(APNS_KEY_FILE).sign(f"{header}.{claims}".encode(), ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(der)
    token = f"{header}.{claims}.{_b64(r.to_bytes(32, 'big') + s.to_bytes(32, 'big'))}"
    _jwt = (now, token)
    return token


# ── Sending ─────────────────────────────────────────────────────


def payload(title: str, body: str, url: str, tag: str, subtitle: str = "", category: str = "",
            data: dict | None = None) -> bytes:
    """APNs JSON payload, body trimmed to fit MAX_PAYLOAD. `category` selects the app's action buttons; `data` (e.g.
    an approval's id and nonce) is merged alongside `url`."""
    title, body = title[:120], body[:1000]
    alert = {"title": title, "body": body} | ({"subtitle": subtitle[:120]} if subtitle else {})
    aps = {"alert": alert, "sound": "default", "thread-id": tag or "jarvis"} | ({"category": category} if category else {})
    while True:
        alert["body"] = body
        encoded = json.dumps((data or {}) | {"aps": aps, "url": url, "tag": tag}).encode()
        if len(encoded) <= MAX_PAYLOAD or not body:
            return encoded
        body = body[:-100]


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(http2=True, timeout=SEND_TIMEOUT)


async def _send(client, dev: dict, data: bytes, jwt: str, tag: str) -> str:
    """"ok", "gone" (token no longer valid) or "failed"."""
    headers = {"authorization": f"bearer {jwt}", "apns-topic": dev["topic"], "apns-push-type": "alert",
               "apns-priority": "10", "apns-expiration": str(int(time.time()) + TTL)}
    if tag:
        headers["apns-collapse-id"] = tag[:64]  # newer push with the same tag replaces the old
    try:
        r = await client.post(f"{HOSTS[dev['environment']]}/3/device/{dev['token']}", content=data, headers=headers)
    except Exception as e:
        detail = type(e).__name__
    else:
        if r.status_code == 200:
            return "ok"
        try:
            said = r.json()
        except ValueError:
            said = None
        reason = str(said.get("reason", ""))[:60] if isinstance(said, dict) else ""
        if r.status_code == 410 or reason in GONE:
            return "gone"
        detail = f"HTTP {r.status_code} {reason}".strip()
    # never log the device token
    log.warning("APNs push to %s (%s) failed: %s", dev.get("device") or "a device", dev["environment"], detail)
    return "failed"


async def send(title: str, body: str, url: str = "/", tag: str = "", devices: Devices | None = None,
               subtitle: str = "", category: str = "", data: dict | None = None) -> int:
    """Notify every registered app; returns how many APNs accepted."""
    store = devices or Devices()
    targets = store.all()
    if not targets:
        return 0
    if not configured():
        log.warning("%d app device%s registered, but %s", len(targets), "" if len(targets) == 1 else "s", NOT_SET_UP)
        return 0
    try:
        jwt = await asyncio.to_thread(provider_token)
        client = _client()
    except Exception as e:  # unreadable key, or httpx without h2
        log.warning("APNs push not sent: %s: %s", type(e).__name__, e if isinstance(e, ImportError) else "")
        return 0
    async with client:
        data = payload(title, body, url, tag, subtitle, category, data)
        results = await asyncio.gather(*(_send(client, d, data, jwt, tag) for d in targets))
    if gone := {d["token"] for d, r in zip(targets, results) if r == "gone"}:
        store.remove(gone)
        log.info("dropped %d app device%s APNs no longer knows", len(gone), "" if len(gone) == 1 else "s")
    return results.count("ok")
