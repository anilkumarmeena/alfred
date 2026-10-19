"""Tests for alfred.notify.push: subscriptions, the VAPID key, sending, runner task events, web endpoints."""

import asyncio
import json
import logging
import stat
from types import SimpleNamespace

import pytest
from py_vapid.utils import b64urldecode
from pywebpush import WebPushException

from alfred import server
from alfred.notify import apns, push
from alfred.notify.push import Subscriptions, send_push, validate
from alfred.tasks import events, runner
from conftest import use_profile

KEYS = {"p256dh": "BNcRdreALRFXTkOOUHK1EtK2wtaz5Ry4YfYCA_0QTpQtUbVlUls0VJXg7A8u-Ts1XbjhazAkj7I99e8QcYP7DkM",
        "auth": "tBHItJI5svbpez7KI4CCXg"}
IPHONE = {"endpoint": "https://web.push.apple.com/QGuQyavXutnMH0tvbA8BYTaeGAkc", "keys": KEYS}
CHROME = {"endpoint": "https://fcm.googleapis.com/fcm/send/abc:def", "keys": KEYS}


def mode(path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


# ── Subscriptions ───────────────────────────────────────────────


def test_subscriptions_persist_one_per_device(tmp_path):
    subs = Subscriptions(tmp_path / "subs.json")
    assert subs.all() == []
    subs.add(IPHONE, "iphone")
    subs.add(CHROME, "browser")
    subs.add(IPHONE | {"expirationTime": None}, "iphone")  # the same device again replaces, not duplicates

    again = Subscriptions(tmp_path / "subs.json").all()  # e.g. the runner, reading what the web app saved
    assert [(s["endpoint"], s["device"]) for s in again] == [(CHROME["endpoint"], "browser"), (IPHONE["endpoint"], "iphone")]
    assert again[1]["keys"] == KEYS and "expirationTime" not in again[1]
    assert mode(tmp_path / "subs.json") == 0o600


def test_oldest_subscription_goes_past_the_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(push, "MAX_DEVICES", 2)
    subs = Subscriptions(tmp_path / "subs.json")
    for i in range(3):
        subs.add({"endpoint": f"https://web.push.apple.com/device{i}", "keys": KEYS})
    assert [s["endpoint"][-7:] for s in subs.all()] == ["device1", "device2"]


def test_remove(tmp_path):
    subs = Subscriptions(tmp_path / "subs.json")
    subs.add(IPHONE)
    subs.add(CHROME)
    assert subs.remove([IPHONE["endpoint"], "https://web.push.apple.com/unknown"]) == 1
    assert subs.remove([IPHONE["endpoint"]]) == 0
    assert [s["endpoint"] for s in subs.all()] == [CHROME["endpoint"]]


def test_unreadable_subscriptions_file_means_no_devices(tmp_path):
    (tmp_path / "subs.json").write_text("{nope")
    assert Subscriptions(tmp_path / "subs.json").all() == []


@pytest.mark.parametrize("bad", [
    None,
    {"endpoint": "http://web.push.apple.com/x", "keys": KEYS},  # not https
    {"endpoint": "https://evil.example.com/x", "keys": KEYS},  # not a push service
    {"endpoint": "https://push.apple.com.evil.example/x", "keys": KEYS},
    {"endpoint": "https://127.0.0.1/x", "keys": KEYS},
    {"endpoint": IPHONE["endpoint"]},  # no keys
    {"endpoint": IPHONE["endpoint"], "keys": {"p256dh": KEYS["p256dh"], "auth": "<script>"}},
])
def test_validate_refuses_anything_but_a_push_service_subscription(bad):
    with pytest.raises(ValueError):
        validate(bad)


def test_validate_accepts_the_big_browsers():
    for endpoint in ("https://web.push.apple.com/abc", "https://fcm.googleapis.com/fcm/send/x",
                     "https://updates.push.services.mozilla.com/wpush/v2/x", "https://wns2-par02p.notify.windows.com/w/?token=x"):
        assert validate({"endpoint": endpoint, "keys": KEYS})["endpoint"] == endpoint


# ── VAPID key ───────────────────────────────────────────────────


def test_vapid_key_is_made_once_and_kept_private(tmp_path, caplog):
    key_file = tmp_path / "vapid_private.pem"
    with caplog.at_level(logging.DEBUG):
        public = push.public_key(key_file)
    assert mode(key_file) == 0o600
    assert push.public_key(key_file) == public  # the same key on every later call
    raw = b64urldecode(public.encode())
    assert len(raw) == 65 and raw[0] == 4  # an uncompressed P-256 point, as browsers want it
    pem = key_file.read_text()
    assert "PRIVATE KEY" in pem
    body = "".join(pem.splitlines()[1:-1])
    assert body[:40] not in caplog.text  # the private key never reaches the log


# ── Sending ─────────────────────────────────────────────────────


class FakePushService:
    """Fake pywebpush.webpush_async; answers each endpoint with the given HTTP status."""

    def __init__(self, status: dict[str, int] | None = None, error: Exception | None = None) -> None:
        self.status, self.error, self.calls = status or {}, error, []

    async def __call__(self, subscription_info, data, vapid_private_key, vapid_claims, **kw):
        self.calls.append({"info": subscription_info, "data": json.loads(data), "claims": dict(vapid_claims),
                           "key": vapid_private_key} | kw)
        vapid_claims["aud"] = "https://filled.in.by.webpush"  # the real one mutates the claims dict it's given
        if self.error:
            raise self.error
        code = self.status.get(subscription_info["endpoint"], 201)
        if code > 202:
            raise WebPushException(f"Push failed: {code}", response=SimpleNamespace(status=code, text=""))


@pytest.fixture
def devices(tmp_path):
    subs = Subscriptions(tmp_path / "subs.json")
    subs.add(IPHONE, "iphone")
    subs.add(CHROME, "browser")
    return subs


def send(subs, tmp_path, *args, **kw):
    return asyncio.run(send_push(*args, subscriptions=subs, key_file=tmp_path / "vapid.pem",
                                 app_devices=apns.Devices(tmp_path / "app-devices.json"), **kw))


def test_send_push_reaches_every_device(devices, tmp_path, monkeypatch):
    service = FakePushService()
    monkeypatch.setattr(push, "webpush_async", service)
    assert send(devices, tmp_path, "Task 3 is done", "Task 3, retry logic, is done.", "/?task=3", "task-3") == 2

    assert [c["info"]["endpoint"] for c in service.calls] == [IPHONE["endpoint"], CHROME["endpoint"]]
    for c in service.calls:
        assert c["info"]["keys"] == KEYS
        assert c["data"] == {"title": "Task 3 is done", "body": "Task 3, retry logic, is done.", "url": "/?task=3", "tag": "task-3"}
        assert c["claims"] == {"sub": push.contact()}  # fresh each time: no aud left from another push service
        assert c["ttl"] == push.TTL
        assert isinstance(c["key"], push.Vapid)
    assert (tmp_path / "vapid.pem").exists()


def test_expired_subscriptions_are_dropped_and_others_kept(devices, tmp_path, monkeypatch):
    monkeypatch.setattr(push, "webpush_async", FakePushService({IPHONE["endpoint"]: 410, CHROME["endpoint"]: 500}))
    assert send(devices, tmp_path, "Mail", "New email from Alice.") == 0
    assert [s["endpoint"] for s in devices.all()] == [CHROME["endpoint"]]  # a server error isn't an unsubscribe

    monkeypatch.setattr(push, "webpush_async", FakePushService({CHROME["endpoint"]: 404}))
    send(devices, tmp_path, "Mail", "Another.")
    assert devices.all() == []


def test_network_failure_keeps_the_subscription_and_logs_no_secrets(devices, tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(push, "webpush_async", FakePushService(error=OSError(f"connect to {IPHONE['endpoint']} failed")))
    with caplog.at_level(logging.INFO, logger="alfred.notify.push"):
        assert send(devices, tmp_path, "Calendar", "Standup starts in 10 minutes.") == 0
    assert len(devices.all()) == 2
    assert "web.push.apple.com" in caplog.text and "OSError" in caplog.text
    assert "QGuQyavXutnMH0tvbA8BYTaeGAkc" not in caplog.text  # the endpoint's secret path stays out of the log


def test_no_devices_sends_nothing_and_makes_no_key(tmp_path, monkeypatch):
    service = FakePushService()
    monkeypatch.setattr(push, "webpush_async", service)
    assert send(Subscriptions(tmp_path / "none.json"), tmp_path, "Calendar", "Standup.") == 0
    assert service.calls == [] and not (tmp_path / "vapid.pem").exists()


def test_long_text_is_trimmed_to_fit_a_push(devices, tmp_path, monkeypatch):
    service = FakePushService()
    monkeypatch.setattr(push, "webpush_async", service)
    send(devices, tmp_path, "T" * 500, "B" * 10000)
    assert len(service.calls[0]["data"]["title"]) == 120 and len(service.calls[0]["data"]["body"]) == 1000


# ── Task events in the runner ──────────────────────────────────


@pytest.fixture
def pushes(tmp_path, monkeypatch):
    """Runner on a temp database, desktop notifications off, pushes recorded."""
    use_profile(monkeypatch, tmp_path)
    monkeypatch.setattr(events, "PUSH_ENABLED", True)
    sent = []

    async def no_notify(title, text):
        pass

    async def record(title, body, url, tag):
        sent.append((title, body, url, tag))

    monkeypatch.setattr(events, "mac_notify", no_notify)
    monkeypatch.setattr(events, "send_push", record)
    return sent


async def _finish_all(statuses: list[str]) -> runner.Runner:
    r = runner.Runner()
    for status in statuses:
        t = r.store.create("research", f"Look into {status}", "Find out.", None)
        r._finish(t["id"], status, f"Task {t['id']}, Look into {status}, {status}.")
    while r._bg:
        await asyncio.gather(*list(r._bg))
    return r


def test_task_done_failed_or_waiting_pushes_and_opens_on_the_task(pushes):
    asyncio.run(_finish_all(["done", "failed", "waiting", "stopped"]))
    assert pushes == [
        ("Task 1 is done", "Task 1, Look into done, done.", "/?task=1", "task-1"),
        ("Task 2 failed", "Task 2, Look into failed, failed.", "/?task=2", "task-2"),
        ("Task 3 has a question", "Task 3, Look into waiting, waiting.", "/?task=3", "task-3"),
    ]  # stopping a task yourself isn't news


def test_no_task_pushes_when_push_is_off(pushes, monkeypatch):
    monkeypatch.setattr(events, "PUSH_ENABLED", False)
    asyncio.run(_finish_all(["done", "waiting"]))
    assert pushes == []


def test_a_finished_task_run_pushes_its_summary(pushes, monkeypatch):
    async def run(task, prompt, reporter):
        return runner.Outcome("Found three options.\nDetails.", "", 0.01)

    monkeypatch.setattr(runner, "run", run)

    async def go():
        r = runner.Runner()
        r.start("research", "LiveKit changes", "What changed in LiveKit 1.9?")
        while r.jobs or r._bg:
            await asyncio.gather(*list(r.jobs.values()), *list(r._bg))

    asyncio.run(go())
    assert pushes == [("Task 1 is done", "Your task, LiveKit changes, is done. Found three options.", "/?task=1", "task-1")]


def test_push_failure_in_the_runner_is_only_logged(pushes, monkeypatch, caplog):
    async def broken(*args):
        raise RuntimeError("secret-endpoint-path")

    monkeypatch.setattr(events, "send_push", broken)
    with caplog.at_level(logging.WARNING, logger="alfred.tasks.runner"):
        r = asyncio.run(_finish_all(["done"]))
    assert r.store.get(1)["status"] == "done"
    assert "push failed: RuntimeError" in caplog.text and "secret-endpoint-path" not in caplog.text


# ── Web endpoints ───────────────────────────────────────────────


class Request:
    """Minimal aiohttp request for the handlers (the sandboxed test run can't open a port)."""

    remote = "100.64.0.2"

    def __init__(self, body: dict | None = None) -> None:
        self.body = body or {}

    async def json(self) -> dict:
        return self.body


@pytest.fixture
def site(tmp_path, monkeypatch):
    monkeypatch.setenv("ALFRED_PASSCODE", "123456")  # no login yet: the first-setup passcode
    monkeypatch.setattr(server, "PUSH_ENABLED", True)
    monkeypatch.setattr(push, "VAPID_KEY_FILE", tmp_path / "vapid.pem")
    use_profile(monkeypatch, tmp_path)
    server._failures.clear()
    return tmp_path


def reply(handler, body: dict | None = None) -> tuple[int, dict]:
    r = asyncio.run(handler(Request(body)))
    return r.status, json.loads(r.text)


def test_web_push_key_subscribe_and_unsubscribe(site):
    status, key = reply(server.push_key)
    assert status == 200 and key["enabled"] is True and key["key"] == push.public_key(site / "vapid.pem")

    assert reply(server.push_subscribe, {"passcode": "000000", "subscription": IPHONE})[0] == 401
    bad = {"endpoint": "https://evil.example.com/x", "keys": KEYS}
    assert reply(server.push_subscribe, {"passcode": "123456", "subscription": bad})[0] == 400
    assert reply(server.push_subscribe, {"passcode": "123456", "device": "iPhone", "subscription": IPHONE}) == (200, {"ok": True})
    assert [(s["endpoint"], s["device"]) for s in Subscriptions(site / "push_subscriptions.json").all()] == [(IPHONE["endpoint"], "iphone")]

    assert reply(server.push_unsubscribe, {"passcode": "000000", "endpoint": IPHONE["endpoint"]})[0] == 401
    assert reply(server.push_unsubscribe, {"passcode": "123456", "endpoint": IPHONE["endpoint"]}) == (200, {"ok": True, "removed": 1})
    assert Subscriptions(site / "push_subscriptions.json").all() == []


def test_web_push_off(site, monkeypatch):
    monkeypatch.setattr(server, "PUSH_ENABLED", False)
    status, key = reply(server.push_key)
    assert key["enabled"] is False and "key" not in key and "ALFRED_PUSH" in key["error"]
    assert reply(server.push_subscribe, {"passcode": "123456", "subscription": IPHONE})[0] == 409
    assert not (site / "vapid.pem").exists() and not (site / "push_subscriptions.json").exists()


def test_wrong_passcodes_for_push_count_towards_the_lockout(site):
    for _ in range(server.MAX_FAILURES):
        reply(server.push_subscribe, {"passcode": "000000", "subscription": IPHONE})
    assert reply(server.push_subscribe, {"passcode": "123456", "subscription": IPHONE})[0] == 429


def test_service_worker_is_served_from_the_root():
    routes = {(r.method, r.resource.canonical) for r in server.app().router.routes()}
    assert {("GET", "/sw.js"), ("GET", "/api/push/key"), ("POST", "/api/push/subscribe"),
            ("POST", "/api/push/unsubscribe"), ("POST", "/api/push/native")} <= routes
    if not (server.WEB_DIR / "sw.js").is_file():
        pytest.skip("the web app isn't built here (make web-build)")
    r = asyncio.run(server.service_worker(Request()))
    assert r.headers["Service-Worker-Allowed"] == "/"
    code = (server.WEB_DIR / "sw.js").read_text()
    assert "notificationclick" in code and "showNotification" in code


