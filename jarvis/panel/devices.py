"""Control panel actions: Notification devices."""

import hashlib

from jarvis.notify import apns, fcm, push
from jarvis.panel.base import GIT_TIMEOUT, HIDDEN, LOG_LINES, SERVICE_NAMES, _id, _text, action, log  # noqa: F401

# ── Notification devices ────────────────────────────────────────


def _device_id(secret: str) -> str:
    """Non-reversible id for a push endpoint or app token."""
    return hashlib.sha256(secret.encode()).hexdigest()[:16]


def _devices(endpoint: str = "") -> list[dict]:
    web = [{"id": _device_id(s["endpoint"]), "kind": "web", "device": s.get("device") or "browser",
            "added": s.get("added"), "this": s["endpoint"] == endpoint} for s in push.Subscriptions().all()]
    app = [{"id": _device_id(d["token"]), "kind": "app", "device": d.get("device") or "iPhone", "added": d.get("added"),
            "this": False} for d in apns.Devices().all()]
    android = [{"id": _device_id(d["token"]), "kind": "app", "device": d.get("device") or "android",
                "added": d.get("added"), "this": False} for d in fcm.Devices().all()]
    return web + app + android


@action
async def devices(body: dict) -> list[dict]:
    """Devices receiving notifications; the caller's own endpoint is marked `this`."""
    return _devices(str(body.get("endpoint", "")))


@action
async def device_remove(body: dict) -> list[dict]:
    gone = _text(body, "id", 32)
    subs = push.Subscriptions()
    subs.remove([s["endpoint"] for s in subs.all() if _device_id(s["endpoint"]) == gone])
    for store in (apns.Devices(), fcm.Devices()):
        store.remove({d["token"] for d in store.all() if _device_id(d["token"]) == gone})
    return _devices(str(body.get("endpoint", "")))
