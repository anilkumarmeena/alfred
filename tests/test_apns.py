"""Tests for jarvis.notify.apns: devices, provider token, sending, and the endpoint."""

import asyncio
import base64
import json
import logging
import stat

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

from conftest import use_profile
from jarvis import server
from jarvis.notify import apns, push
from jarvis.notify.apns import Devices

TOKEN = "a1" * 32
OTHER = "b2" * 32
PHONE = {"token": TOKEN.upper(), "environment": "development", "topic": "com.example.jarvis"}


@pytest.fixture
def key(tmp_path, monkeypatch):
    """A P-256 APNs auth key (PKCS#8 PEM) and its settings."""
    private = ec.generate_private_key(ec.SECP256R1())
    path = tmp_path / "apns_key.p8"
    path.write_bytes(private.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))
    monkeypatch.setattr(apns, "APNS_KEY_FILE", path)
    monkeypatch.setenv("JARVIS_APNS_KEY_ID", "ABC123DEFG")  # not in the vault, so read from .env
    monkeypatch.setattr(apns, "APNS_TEAM_ID", "TEAM123456")
    monkeypatch.setattr(apns, "_jwt", None)
    apns._signing_key.cache_clear()
    return private.public_key()


def unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


# ── Devices ─────────────────────────────────────────────────────


def test_devices_persist_one_per_token(tmp_path):
    devices = Devices(tmp_path / "devices.json")
    assert devices.all() == []
    devices.add(PHONE, "iphone")
    devices.add(PHONE | {"environment": "production"}, "iphone")  # the same token replaces the entry
    again = Devices(tmp_path / "devices.json").all()
    assert [(d["token"], d["environment"], d["topic"], d["device"]) for d in again] == [
        (TOKEN, "production", "com.example.jarvis", "iphone")]
    assert "passcode" not in again[0]
    assert stat.S_IMODE((tmp_path / "devices.json").stat().st_mode) == 0o600
    assert devices.remove({TOKEN}) == 1 and devices.all() == []


@pytest.mark.parametrize("bad", [
    None,
    PHONE | {"token": "abc"},
    PHONE | {"token": "zz" * 32},
    PHONE | {"token": "../" * 30},
    PHONE | {"environment": "staging"},
    PHONE | {"topic": ""},
    PHONE | {"topic": "com.example.jarvis/../x"},
])
def test_validate_refuses_malformed_devices(bad):
    with pytest.raises(ValueError):
        apns.validate(bad)


# ── Provider token ──────────────────────────────────────────────


def test_provider_token_is_an_es256_jwt_reused_for_50_minutes(key):
    jwt = apns.provider_token(now=1000)
    header, claims, sig = jwt.split(".")
    assert json.loads(unb64(header)) == {"alg": "ES256", "kid": "ABC123DEFG"}
    assert json.loads(unb64(claims)) == {"iss": "TEAM123456", "iat": 1000}
    raw = unb64(sig)
    assert len(raw) == 64
    der = encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big"))
    key.verify(der, f"{header}.{claims}".encode(), ec.ECDSA(hashes.SHA256()))  # raises if it's wrong
    assert apns.provider_token(now=1000 + 49 * 60) == jwt
    assert apns.provider_token(now=1000 + 51 * 60) != jwt


# ── Sending ─────────────────────────────────────────────────────


class Reply:
    def __init__(self, status: int, body: dict | None = None) -> None:
        self.status_code, self.body = status, body

    def json(self):
        if self.body is None:
            raise ValueError("no JSON")
        return self.body


class FakeAPNs:
    """Fake httpx.AsyncClient(http2=True): answers each device token with the given reply."""

    def __init__(self, replies: dict[str, Reply] | None = None, error: Exception | None = None) -> None:
        self.replies, self.error, self.calls, self.closed = replies or {}, error, [], False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        self.closed = True

    async def post(self, url, content, headers):
        self.calls.append({"url": url, "data": json.loads(content), "headers": headers})
        if self.error:
            raise self.error
        return self.replies.get(url.rsplit("/", 1)[1], Reply(200))


@pytest.fixture
def phones(tmp_path):
    devices = Devices(tmp_path / "devices.json")
    devices.add(PHONE, "iphone")
    devices.add({"token": OTHER, "environment": "production", "topic": "com.example.jarvis"}, "ipad")
    return devices


def send(devices, *args):
    return asyncio.run(apns.send(*args, devices=devices))


def test_send_reaches_every_app_on_its_apns_server(key, phones, monkeypatch):
    service = FakeAPNs()
    monkeypatch.setattr(apns, "_client", lambda: service)
    assert send(phones, "Task 3 is done", "Task 3, retry logic, is done.", "/?task=3", "task-3") == 2
    assert [c["url"] for c in service.calls] == [
        f"https://api.sandbox.push.apple.com/3/device/{TOKEN}", f"https://api.push.apple.com/3/device/{OTHER}"]
    for c in service.calls:
        assert c["data"] == {"aps": {"alert": {"title": "Task 3 is done", "body": "Task 3, retry logic, is done."},
                                     "sound": "default", "thread-id": "task-3"}, "url": "/?task=3", "tag": "task-3"}
        h = c["headers"]
        assert h["authorization"] == f"bearer {apns.provider_token()}"
        assert (h["apns-topic"], h["apns-push-type"], h["apns-priority"], h["apns-collapse-id"]) == (
            "com.example.jarvis", "alert", "10", "task-3")
    assert service.closed


def test_stale_tokens_are_dropped_and_others_kept(key, phones, monkeypatch):
    monkeypatch.setattr(apns, "_client", lambda: FakeAPNs({TOKEN: Reply(410, {"reason": "Unregistered"}),
                                                           OTHER: Reply(500, {"reason": "InternalServerError"})}))
    assert send(phones, "Mail", "New email from Alice.") == 0
    assert [d["token"] for d in phones.all()] == [OTHER]  # a server error isn't an uninstall
    monkeypatch.setattr(apns, "_client", lambda: FakeAPNs({OTHER: Reply(400, {"reason": "BadDeviceToken"})}))
    send(phones, "Mail", "Another.")
    assert phones.all() == []


def test_failures_keep_the_device_and_log_no_token(key, phones, monkeypatch, caplog):
    monkeypatch.setattr(apns, "_client", lambda: FakeAPNs({TOKEN: Reply(403, {"reason": "InvalidProviderToken"})}))
    with caplog.at_level(logging.INFO, logger="jarvis.notify.apns"):
        assert send(phones, "Calendar", "Standup in 10 minutes.") == 1
    assert len(phones.all()) == 2
    assert "HTTP 403 InvalidProviderToken" in caplog.text and TOKEN not in caplog.text
    monkeypatch.setattr(apns, "_client", lambda: FakeAPNs(error=OSError(f"connect {TOKEN}")))
    with caplog.at_level(logging.INFO, logger="jarvis.notify.apns"):
        assert send(phones, "Calendar", "Standup.") == 0
    assert "OSError" in caplog.text and TOKEN not in caplog.text


def test_nothing_is_sent_until_apns_is_set_up(phones, monkeypatch, caplog, tmp_path):
    monkeypatch.setattr(apns, "APNS_KEY_FILE", tmp_path / "missing.p8")
    monkeypatch.setenv("JARVIS_APNS_KEY_ID", "ABC123DEFG")  # not in the vault, so read from .env
    monkeypatch.setattr(apns, "APNS_TEAM_ID", "TEAM123456")
    service = FakeAPNs()
    monkeypatch.setattr(apns, "_client", lambda: service)
    with caplog.at_level(logging.WARNING, logger="jarvis.notify.apns"):
        assert send(phones, "Calendar", "Standup.") == 0
    assert service.calls == [] and "apns_key.p8" in caplog.text
    assert len(phones.all()) == 2


def test_no_devices_sends_nothing(key, tmp_path, monkeypatch):
    service = FakeAPNs()
    monkeypatch.setattr(apns, "_client", lambda: service)
    assert send(Devices(tmp_path / "none.json"), "Calendar", "Standup.") == 0
    assert service.calls == []


def test_payload_fits_apns_limit():
    data = apns.payload("T" * 500, "ह" * 5000, "/?task=1", "task-1")
    assert len(data) <= apns.MAX_PAYLOAD
    assert len(json.loads(data)["aps"]["alert"]["title"]) == 120


def test_send_push_reaches_web_push_and_the_app_together(key, phones, tmp_path, monkeypatch):
    service = FakeAPNs()
    monkeypatch.setattr(apns, "_client", lambda: service)
    empty = push.Subscriptions(tmp_path / "subs.json")
    n = asyncio.run(push.send_push("Task 1 is done", "Done.", "/?task=1", "task-1",
                                   subscriptions=empty, key_file=tmp_path / "vapid.pem", app_devices=phones))
    assert n == 2 and len(service.calls) == 2


# ── Endpoint ────────────────────────────────────────────────────


class Request:
    remote = "100.64.0.2"

    def __init__(self, body: dict) -> None:
        self.body = body

    async def json(self) -> dict:
        return self.body


def reply(body: dict) -> tuple[int, dict]:
    r = asyncio.run(server.push_native(Request(body)))
    return r.status, json.loads(r.text)


@pytest.fixture
def site(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_PASSCODE", "123456")  # no login yet: the first-setup passcode
    monkeypatch.setattr(server, "PUSH_ENABLED", True)
    use_profile(monkeypatch, tmp_path)
    server._failures.clear()
    return tmp_path


def test_push_native_stores_the_apps_token(site, key):
    assert reply(PHONE | {"passcode": "000000"})[0] == 401
    assert reply(PHONE | {"passcode": "123456", "token": "nope"})[0] == 400
    assert reply(PHONE | {"passcode": "123456", "device": "iPhone"}) == (200, {"ok": True, "sending": True})
    assert [(d["token"], d["device"]) for d in Devices(site / "push_devices.json").all()] == [(TOKEN, "iphone")]


def test_push_native_keeps_the_token_before_apns_is_set_up(site, monkeypatch):
    monkeypatch.delenv("JARVIS_APNS_KEY_ID", raising=False)
    status, body = reply(PHONE | {"passcode": "123456"})
    assert status == 200 and body["sending"] is False and "key ID" in body["error"]
    assert len(Devices(site / "push_devices.json").all()) == 1


def test_push_native_off(site, monkeypatch):
    monkeypatch.setattr(server, "PUSH_ENABLED", False)
    assert reply(PHONE | {"passcode": "123456"})[0] == 409
    assert not (site / "push_devices.json").exists()
