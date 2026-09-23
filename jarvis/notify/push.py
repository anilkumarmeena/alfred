"""Web Push: heartbeat alerts and task updates reach the phone even when JARVIS isn't open.

The home-screen web app (web/) registers a service worker, subscribes with JARVIS's VAPID
public key when the user taps Enable notifications, and posts the subscription to jarvis.server,
which keeps it in data/push_subscriptions.json, one per device. The task runner calls
send_push() for heartbeat alerts and for tasks that finish, fail or ask a question. Each push
is encrypted to the device and signed with the VAPID private key, generated on first use into
secrets/ (mode 600) and never logged. A push service answering 404 or 410 means the device
has unsubscribed or the app was removed, so its subscription is dropped. send_push() also reaches the JARVIS
iPhone app, which registers with APNs instead (jarvis.notify.apns).

On unless JARVIS_PUSH=off, and nothing is sent until a device has subscribed.
"""

import asyncio
import fcntl
import functools
import json
import logging
import re
import time
from collections.abc import Awaitable, Callable, Iterable
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

import aiohttp
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from py_vapid import Vapid
from py_vapid.utils import b64urlencode
from pywebpush import WebPushException, webpush_async

from jarvis.core import profile
from jarvis.core.config import PUSH_CONTACT, VAPID_KEY_FILE
from jarvis.core.files import write_atomic

if TYPE_CHECKING:
    from jarvis.notify import apns

log = logging.getLogger("jarvis.notify.push")
# Where browsers' push services live; a subscription pointing anywhere else is refused, so the
# runner never posts to an arbitrary host. Apple (Safari, iOS home-screen apps), Google (Chrome),
# Mozilla (Firefox), Microsoft (Edge).
PUSH_HOSTS = ("push.apple.com", "fcm.googleapis.com", "push.services.mozilla.com", "notify.windows.com")
MAX_DEVICES = 10  # the oldest subscription goes when an eleventh arrives
TTL = 12 * 3600  # how long a push service keeps trying a phone that's offline
SEND_TIMEOUT = 20
_KEY = re.compile(r"^[A-Za-z0-9_-]{16,200}={0,2}$")  # base64url, as browsers encode p256dh and auth

Push = Callable[[str, str, str, str], Awaitable[object]]  # title, body, url, tag


@contextmanager
def _locked(path: Path):
    """An exclusive lock between processes: the web app adds subscriptions, the runner drops them."""
    with open(path.with_name(path.name + ".lock"), "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield  # closing the file releases the lock


def _host(endpoint: str) -> str:
    return urlsplit(endpoint).hostname or "?"


def task_target(task_id: int) -> tuple[str, str]:
    """Where tapping a task's notification opens JARVIS, and its tag: a newer push about the same
    task, from the runner or the heartbeat, replaces the older one."""
    return f"/?task={task_id}", f"task-{task_id}"


# ── Subscriptions ───────────────────────────────────────────────


def validate(info: object) -> dict:
    """A browser's PushSubscription.toJSON(), checked and trimmed to what sending needs."""
    if not isinstance(info, dict):
        raise ValueError("subscription must be an object")
    endpoint, keys = info.get("endpoint"), info.get("keys")
    if not isinstance(endpoint, str) or len(endpoint) > 1000:
        raise ValueError("subscription endpoint is missing")
    url = urlsplit(endpoint)
    host = url.hostname or ""
    if url.scheme != "https" or not any(host == h or host.endswith("." + h) for h in PUSH_HOSTS):
        raise ValueError("subscription endpoint isn't a known push service")
    if not isinstance(keys, dict) or not all(isinstance(keys.get(k), str) and _KEY.match(keys[k]) for k in ("p256dh", "auth")):
        raise ValueError("subscription keys are missing or malformed")
    return {"endpoint": endpoint, "keys": {"p256dh": keys["p256dh"], "auth": keys["auth"]}}


class Subscriptions:
    """The devices to push to, in a JSON file shared by the web app and the task runner."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or profile.current().push_subscriptions

    def all(self) -> list[dict]:
        try:
            data = json.loads(self.path.read_text())
        except FileNotFoundError:
            return []
        except ValueError:
            log.warning("%s is unreadable; no devices to push to", self.path.name)
            return []
        return [s for s in data if isinstance(s, dict) and "endpoint" in s] if isinstance(data, list) else []

    def _save(self, subs: list[dict]) -> None:
        write_atomic(self.path, json.dumps(subs, indent=1), mode=0o600)

    def add(self, info: object, device: str = "") -> dict:
        """Store a subscription, replacing any earlier one with the same endpoint."""
        sub = validate(info) | {"device": device[:40], "added": time.time()}
        with _locked(self.path):
            subs = [s for s in self.all() if s["endpoint"] != sub["endpoint"]]
            self._save((subs + [sub])[-MAX_DEVICES:])
        return sub

    def remove(self, endpoints: Iterable[str]) -> int:
        """Forget these endpoints; returns how many were stored."""
        gone = set(endpoints)
        with _locked(self.path):
            subs = self.all()
            keep = [s for s in subs if s["endpoint"] not in gone]
            if len(keep) != len(subs):
                self._save(keep)
        return len(subs) - len(keep)


# ── VAPID key ───────────────────────────────────────────────────


def vapid(path: Path | None = None) -> Vapid:
    """JARVIS's VAPID key pair, generated the first time it's needed."""
    return _vapid(path or VAPID_KEY_FILE)


@functools.cache  # the key never changes while JARVIS runs
def _vapid(path: Path) -> Vapid:
    if not path.exists():
        with _locked(path):
            if not path.exists():  # another process may have made it while this one waited
                key = Vapid()
                key.generate_keys()
                write_atomic(path, key.private_pem(), mode=0o600)
                log.info("generated a VAPID key pair in %s", path.name)
    return Vapid.from_pem(path.read_bytes())


def public_key(path: Path | None = None) -> str:
    """The public half, as the browser's pushManager.subscribe() wants it: base64url, uncompressed point."""
    return b64urlencode(vapid(path).public_key.public_bytes(Encoding.X962, PublicFormat.UncompressedPoint))


# ── Sending ─────────────────────────────────────────────────────

def contact() -> str:
    """Who push services may reach about JARVIS's pushes: JARVIS_PUSH_CONTACT, else the default Google account's
    address. Apple refuses a made-up one (BadJwtToken), so the example address is only the last resort."""
    if PUSH_CONTACT:
        return PUSH_CONTACT
    from jarvis.integrations import accounts

    found = accounts.load()
    account = found.get(found.default) or next(iter(found.accounts), None)
    return f"mailto:{account.email}" if account and account.email else "mailto:jarvis@example.com"


async def _send(sub: dict, payload: str, key: Vapid) -> str:
    """"ok" if the push service took it, "gone" if the subscription has expired, else "failed"."""
    try:
        await webpush_async(
            {"endpoint": sub["endpoint"], "keys": sub["keys"]}, payload,
            vapid_private_key=key,
            vapid_claims={"sub": contact()},  # a fresh dict each time: webpush fills in aud for the endpoint's host
            ttl=TTL, headers={"Urgency": "high"}, timeout=aiohttp.ClientTimeout(total=SEND_TIMEOUT),
        )
        return "ok"
    except WebPushException as e:
        if e.status_code in (404, 410):
            return "gone"
        detail = f"HTTP {e.status_code}"
    except Exception as e:
        detail = type(e).__name__
    # the endpoint is a capability URL and the response may echo it, so only the host and status are logged
    log.warning("push to %s (%s) failed: %s", sub.get("device") or "a device", _host(sub["endpoint"]), detail)
    return "failed"


async def send_push(title: str, body: str, url: str = "/", tag: str = "",
                    subscriptions: Subscriptions | None = None, key_file: Path | None = None,
                    app_devices: "apns.Devices | None" = None, *, subtitle: str = "", category: str = "",
                    data: dict | None = None) -> int:
    """Show a notification on every subscribed device and every registered JARVIS app. Tapping it opens
    `url` in JARVIS; a later push with the same `tag` replaces it. Returns how many were accepted.
    In the app, `category` gives it action buttons (JARVIS_APPROVAL: Approve and Deny), and `data` goes along
    for them; Web Push has no buttons, so it gets neither."""
    from jarvis.notify import apns

    sent = await asyncio.gather(_send_web(title, body, url, tag, subscriptions, key_file),
                                apns.send(title, body, url, tag, app_devices, subtitle=subtitle, category=category, data=data),
                                return_exceptions=True)
    for r in sent:  # one failing doesn't stop the other
        if isinstance(r, BaseException):
            raise r
    return sum(r for r in sent if isinstance(r, int))


async def _send_web(title: str, body: str, url: str, tag: str,
                    subscriptions: Subscriptions | None, key_file: Path | None) -> int:
    subs = subscriptions or Subscriptions()
    devices = subs.all()
    if not devices:
        return 0
    key = await asyncio.to_thread(vapid, key_file)
    payload = json.dumps({"title": title[:120], "body": body[:1000], "url": url, "tag": tag})
    results = await asyncio.gather(*(_send(d, payload, key) for d in devices))
    if gone := [d["endpoint"] for d, r in zip(devices, results) if r == "gone"]:
        subs.remove(gone)
        log.info("dropped %d expired push subscription%s", len(gone), "" if len(gone) == 1 else "s")
    return results.count("ok")
