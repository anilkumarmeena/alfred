"""FCM push for the native Android app (its web view can't do Web Push); same alerts as jarvis.notify.push.

The app posts its registration token to /api/push/native with service "fcm"; tokens live in data/fcm_devices.json.
Each push is an FCM HTTP v1 data message, authorized by an OAuth token from the Firebase service-account key in
secrets/fcm_service_account.json. UNREGISTERED, SENDER_ID_MISMATCH or a rejected token drops the device; other errors
(a bad payload, an outage) never do, so one bug can't wipe every phone.
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
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from jarvis.config import FCM_SERVICE_ACCOUNT_FILE
from jarvis.notify import apns
from jarvis.notify.push import SEND_TIMEOUT, TTL
from jarvis.storage import profile

log = logging.getLogger("jarvis.notify.fcm")
TOKEN_URL = "https://oauth2.googleapis.com/token"  # fixed: a key file can't redirect the signed assertion elsewhere
SEND_URL = "https://fcm.googleapis.com/v1/projects/{project}/messages:send"
SCOPE = "https://www.googleapis.com/auth/firebase.messaging"
ACCESS_LIFETIME = 50 * 60  # Google's tokens last 1 h
MAX_PAYLOAD = 4000  # bytes of data; FCM's limit is 4096
_TOKEN = re.compile(r"^[A-Za-z0-9_:-]{100,4096}$")
_TOPIC = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z][A-Za-z0-9_]*)+$")  # Android package name
_PROJECT = re.compile(r"^[a-z][a-z0-9-]{4,61}[a-z0-9]$")
GONE = {"UNREGISTERED", "SENDER_ID_MISMATCH"}
NOT_SET_UP = ("FCM isn't set up on this computer: save the Firebase project's service-account key as "
              "secrets/fcm_service_account.json, then restart JARVIS.")


# ── Devices ─────────────────────────────────────────────────────


def validate(info: object) -> dict:
    """Validate the app's registration: FCM token and package name (topic)."""
    if not isinstance(info, dict):
        raise ValueError("device must be an object")
    token = str(info.get("token", ""))
    if not _TOKEN.match(token):
        raise ValueError("token is missing or malformed")
    topic = info.get("topic")
    if not isinstance(topic, str) or len(topic) > 155 or not _TOPIC.match(topic):
        raise ValueError("topic must be the app's package name")
    return {"token": token, "topic": topic}


class Devices(apns.Devices):
    """Registered Android app installs; JSON file shared by the web app and task runner."""

    validate = staticmethod(validate)

    def __init__(self, path: Path | None = None) -> None:
        super().__init__(path or profile.current().fcm_devices)


# ── Service account ─────────────────────────────────────────────


def account(path: Path | None = None) -> dict | None:
    """The service-account key's project, email and RSA key; None if missing or unusable."""
    path = path or FCM_SERVICE_ACCOUNT_FILE
    try:
        info = json.loads(path.read_text())
        project, email = info["project_id"], info["client_email"]
        key = _private_key(info["private_key"])
    except FileNotFoundError:
        return None
    except (OSError, ValueError, KeyError, TypeError) as e:
        log.warning("%s is unusable: %s", path.name, type(e).__name__)  # never log the key
        return None
    if not (isinstance(project, str) and _PROJECT.match(project) and isinstance(email, str) and "@" in email):
        log.warning("%s has no valid project_id and client_email", path.name)
        return None
    return {"project": project, "email": email, "key": key}


def configured() -> bool:
    return account() is not None


@functools.cache
def _private_key(pem: str) -> rsa.RSAPrivateKey:
    key = serialization.load_pem_private_key(pem.encode(), password=None)
    if not isinstance(key, rsa.RSAPrivateKey):
        raise ValueError("not an RSA key")
    return key


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def assertion(acct: dict, now: float) -> str:
    """RS256 JWT asking Google for an FCM access token."""
    header = _b64(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
    claims = _b64(json.dumps({"iss": acct["email"], "scope": SCOPE, "aud": TOKEN_URL, "iat": int(now),
                              "exp": int(now) + 3600}).encode())
    sig = acct["key"].sign(f"{header}.{claims}".encode(), padding.PKCS1v15(), hashes.SHA256())
    return f"{header}.{claims}.{_b64(sig)}"


_access: tuple[float, str, str] | None = None  # (fetched at, client email, token)


async def access_token(client, acct: dict, now: float | None = None) -> str:
    """OAuth access token for FCM, cached for ACCESS_LIFETIME. Raises RuntimeError if Google refuses."""
    global _access
    now = time.time() if now is None else now
    if _access and _access[1] == acct["email"] and now - _access[0] < ACCESS_LIFETIME:
        return _access[2]
    r = await client.post(TOKEN_URL, data={"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                                           "assertion": assertion(acct, now)})
    try:
        token = r.json()["access_token"] if r.status_code == 200 else None
    except (ValueError, KeyError, TypeError):
        token = None
    if not isinstance(token, str) or not token:
        raise RuntimeError(f"Google refused the service account (HTTP {r.status_code})")
    _access = (now, acct["email"], token)
    return token


# ── Sending ─────────────────────────────────────────────────────


def data(title: str, body: str, url: str, tag: str, subtitle: str = "") -> dict[str, str]:
    """The data message the app shows itself: all strings, body trimmed to fit MAX_PAYLOAD."""
    title, body = title[:120], body[:1000]
    fields = {"title": title, "url": url, "tag": tag} | ({"subtitle": subtitle[:120]} if subtitle else {})
    while True:
        out = fields | {"body": body}
        if len(json.dumps(out).encode()) <= MAX_PAYLOAD or not body:
            return out
        body = body[:-100]


def message(token: str, fields: dict[str, str], tag: str) -> dict:
    android = {"priority": "HIGH", "ttl": f"{TTL}s"} | ({"collapse_key": tag[:64]} if tag else {})
    return {"message": {"token": token, "data": fields, "android": android}}


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(http2=True, timeout=SEND_TIMEOUT)


def _error(r) -> tuple[str, str]:
    """(FCM error code or status, message) from an error reply."""
    try:
        err = r.json()["error"]
        code = next((d.get("errorCode") for d in err.get("details", []) if isinstance(d, dict) and d.get("errorCode")),
                    err.get("status", ""))
        return str(code)[:40], str(err.get("message", ""))
    except (ValueError, KeyError, TypeError, AttributeError):
        return "", ""


async def _send(client, url: str, access: str, dev: dict, fields: dict[str, str], tag: str) -> str:
    """"ok", "gone" (token no longer valid), "unauthorized" or "failed"."""
    try:
        r = await client.post(url, json=message(dev["token"], fields, tag), headers={"authorization": f"Bearer {access}"})
    except Exception as e:
        detail = type(e).__name__
    else:
        if r.status_code == 200:
            return "ok"
        code, said = _error(r)
        if code in GONE or (code == "INVALID_ARGUMENT" and "registration token" in said.lower()):
            return "gone"
        if r.status_code == 401:
            return "unauthorized"
        detail = f"HTTP {r.status_code} {code}".strip()
    # never log the registration token
    log.warning("FCM push to %s failed: %s", dev.get("device") or "a device", detail)
    return "failed"


async def send(title: str, body: str, url: str = "/", tag: str = "", devices: Devices | None = None,
               subtitle: str = "") -> int:
    """Notify every registered Android app; returns how many FCM accepted."""
    global _access
    store = devices or Devices()
    targets = store.all()
    if not targets:
        return 0
    acct = await asyncio.to_thread(account)
    if acct is None:
        log.warning("%d Android device%s registered, but %s", len(targets), "" if len(targets) == 1 else "s", NOT_SET_UP)
        return 0
    try:
        client = _client()
    except ImportError as e:  # httpx without h2
        log.warning("FCM push not sent: %s", e)
        return 0
    async with client:
        try:
            access = await access_token(client, acct)
        except Exception as e:
            log.warning("FCM push not sent: %s", e if isinstance(e, RuntimeError) else type(e).__name__)
            return 0
        fields = data(title, body, url, tag, subtitle)
        send_url = SEND_URL.format(project=acct["project"])
        results = await asyncio.gather(*(_send(client, send_url, access, d, fields, tag) for d in targets))
    if "unauthorized" in results:
        _access = None  # fetch a fresh token next time
        log.warning("FCM refused JARVIS's access token; it's fetched afresh next time")
    if gone := {d["token"] for d, r in zip(targets, results) if r == "gone"}:
        store.remove(gone)
        log.info("dropped %d Android device%s FCM no longer knows", len(gone), "" if len(gone) == 1 else "s")
    return results.count("ok")
