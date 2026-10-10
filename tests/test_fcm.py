"""Tests for jarvis.notify.fcm: devices, service account, access token, sending, and the endpoint."""

import asyncio
import base64
import json
import logging
import stat

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from conftest import use_profile
from jarvis import panel, server
from jarvis.notify import apns, fcm, push
from jarvis.notify.fcm import Devices

TOKEN = "fA1b2C3d4E5:APA91b" + "x" * 140
OTHER = "eZ9y8X7w6V5:APA91b" + "y" * 140
PHONE = {"token": TOKEN, "topic": "com.example.jarvis", "service": "fcm"}
PROJECT = "jarvis-push-1234"


@pytest.fixture
def account(tmp_path, monkeypatch):
    """A Firebase service-account key file; returns its public key."""
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = private.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption()).decode()
    path = tmp_path / "fcm_service_account.json"
    path.write_text(json.dumps({"type": "service_account", "project_id": PROJECT, "private_key": pem,
                                "client_email": f"jarvis@{PROJECT}.iam.gserviceaccount.com",
                                "token_uri": "https://evil.example/token"}))
    monkeypatch.setattr(fcm, "FCM_SERVICE_ACCOUNT_FILE", path)
    monkeypatch.setattr(fcm, "_access", None)
    return private.public_key()


def unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


# ── Devices ─────────────────────────────────────────────────────


def test_devices_persist_one_per_token(tmp_path):
    devices = Devices(tmp_path / "devices.json")
    devices.add(PHONE, "android")
    devices.add(PHONE | {"topic": "com.example.other"}, "android")  # the same token replaces the entry
    again = Devices(tmp_path / "devices.json").all()
    assert [(d["token"], d["topic"], d["device"]) for d in again] == [(TOKEN, "com.example.other", "android")]
    assert "passcode" not in again[0] and "service" not in again[0]
    assert stat.S_IMODE((tmp_path / "devices.json").stat().st_mode) == 0o600
    assert devices.remove({TOKEN}) == 1 and devices.all() == []


@pytest.mark.parametrize("bad", [
    None,
    PHONE | {"token": "short"},
    PHONE | {"token": "../" * 50},
    PHONE | {"token": TOKEN + " "},
    PHONE | {"topic": ""},
    PHONE | {"topic": "jarvis"},
    PHONE | {"topic": "com.example/../x"},
])
def test_validate_refuses_malformed_devices(bad):
    with pytest.raises(ValueError):
        fcm.validate(bad)


def test_apns_still_refuses_fcm_tokens():
    with pytest.raises(ValueError):
        apns.validate(PHONE | {"environment": "production"})


# ── Service account and access token ────────────────────────────


def test_account_reads_the_key_file(account):
    acct = fcm.account()
    assert acct["project"] == PROJECT and acct["email"].startswith("jarvis@") and fcm.configured()


@pytest.mark.parametrize("contents", ["not json", "{}", json.dumps({"project_id": PROJECT, "client_email": "a@b",
                                                                    "private_key": "nope"})])
def test_an_unusable_key_file_means_not_set_up(tmp_path, monkeypatch, caplog, contents):
    path = tmp_path / "fcm_service_account.json"
    path.write_text(contents)
    monkeypatch.setattr(fcm, "FCM_SERVICE_ACCOUNT_FILE", path)
    with caplog.at_level(logging.WARNING, logger="jarvis.notify.fcm"):
        assert not fcm.configured()
    assert "nope" not in caplog.text


def test_assertion_is_an_rs256_jwt_for_googles_token_endpoint(account):
    jwt = fcm.assertion(fcm.account(), now=1000)
    header, claims, sig = jwt.split(".")
    assert json.loads(unb64(header)) == {"alg": "RS256", "typ": "JWT"}
    c = json.loads(unb64(claims))
    assert (c["aud"], c["scope"], c["iat"], c["exp"]) == (fcm.TOKEN_URL, fcm.SCOPE, 1000, 4600)
    assert c["aud"] == "https://oauth2.googleapis.com/token"  # never the key file's token_uri
    account.verify(unb64(sig), f"{header}.{claims}".encode(), padding.PKCS1v15(), hashes.SHA256())


# ── Sending ─────────────────────────────────────────────────────


class Reply:
    def __init__(self, status: int, body: dict | None = None) -> None:
        self.status_code, self.body = status, body

    def json(self):
        if self.body is None:
            raise ValueError("no JSON")
        return self.body


def fcm_error(status: int, code: str, message: str = "", status_name: str = "") -> Reply:
    details = [{"@type": "type.googleapis.com/google.firebase.fcm.v1.FcmError", "errorCode": code}] if code else []
    return Reply(status, {"error": {"code": status, "message": message, "status": status_name or code,
                                    "details": details}})


class FakeGoogle:
    """Fake httpx.AsyncClient: the OAuth endpoint, then FCM answering per token."""

    def __init__(self, replies: dict[str, Reply] | None = None, oauth: Reply | None = None,
                 error: Exception | None = None) -> None:
        self.replies, self.oauth, self.error = replies or {}, oauth or Reply(200, {"access_token": "ya29.abc"}), error
        self.calls, self.oauth_calls, self.closed = [], 0, False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        self.closed = True

    async def post(self, url, json=None, data=None, headers=None):
        if url == fcm.TOKEN_URL:
            self.oauth_calls += 1
            assert data["grant_type"] == "urn:ietf:params:oauth:grant-type:jwt-bearer" and data["assertion"].count(".") == 2
            return self.oauth
        self.calls.append({"url": url, "body": json, "headers": headers})
        if self.error:
            raise self.error
        return self.replies.get(json["message"]["token"], Reply(200, {"name": "projects/x/messages/1"}))


@pytest.fixture
def phones(tmp_path):
    devices = Devices(tmp_path / "devices.json")
    devices.add(PHONE, "android")
    devices.add({"token": OTHER, "topic": "com.example.jarvis"}, "tablet")
    return devices


def send(devices, *args, **kwargs):
    return asyncio.run(fcm.send(*args, devices=devices, **kwargs))


def test_send_posts_a_high_priority_data_message_to_each_app(account, phones, monkeypatch):
    service = FakeGoogle()
    monkeypatch.setattr(fcm, "_client", lambda: service)
    assert send(phones, "Task 3 is done", "Retry logic is in.", "/?task=3", "task-3", subtitle="Task 3") == 2
    assert service.oauth_calls == 1 and service.closed
    for c, token in zip(service.calls, [TOKEN, OTHER]):
        assert c["url"] == f"https://fcm.googleapis.com/v1/projects/{PROJECT}/messages:send"
        assert c["headers"] == {"authorization": "Bearer ya29.abc"}
        assert c["body"] == {"message": {"token": token, "data": {
            "title": "Task 3 is done", "body": "Retry logic is in.", "url": "/?task=3", "tag": "task-3",
            "subtitle": "Task 3"}, "android": {"priority": "HIGH", "ttl": f"{push.TTL}s", "collapse_key": "task-3"}}}
        assert all(isinstance(v, str) for v in c["body"]["message"]["data"].values())


def test_untagged_pushes_have_no_collapse_key(account, phones, monkeypatch):
    service = FakeGoogle()
    monkeypatch.setattr(fcm, "_client", lambda: service)
    send(phones, "Mail", "New email from Alice.")
    assert "collapse_key" not in service.calls[0]["body"]["message"]["android"]


def test_the_access_token_is_reused_until_it_ages(account, phones, monkeypatch):
    service = FakeGoogle()
    monkeypatch.setattr(fcm, "_client", lambda: service)
    send(phones, "One", "1")
    send(phones, "Two", "2")
    assert service.oauth_calls == 1
    monkeypatch.setattr(fcm, "_access", (fcm._access[0] - fcm.ACCESS_LIFETIME - 1, *fcm._access[1:]))
    send(phones, "Three", "3")
    assert service.oauth_calls == 2


def test_gone_tokens_are_dropped_but_not_for_other_errors(account, phones, monkeypatch):
    monkeypatch.setattr(fcm, "_client", lambda: FakeGoogle({
        TOKEN: fcm_error(404, "UNREGISTERED", "Requested entity was not found.", "NOT_FOUND"),
        OTHER: fcm_error(500, "INTERNAL")}))
    assert send(phones, "Mail", "New email.") == 0
    assert [d["token"] for d in phones.all()] == [OTHER]  # a server error isn't an uninstall
    monkeypatch.setattr(fcm, "_client", lambda: FakeGoogle({OTHER: fcm_error(403, "SENDER_ID_MISMATCH")}))
    send(phones, "Mail", "Another.")
    assert phones.all() == []


def test_invalid_argument_drops_only_a_bad_token(account, phones, monkeypatch):
    payload_bug = fcm_error(400, "INVALID_ARGUMENT", "Invalid value at 'message.data[0].value' (TYPE_STRING)")
    monkeypatch.setattr(fcm, "_client", lambda: FakeGoogle({TOKEN: payload_bug, OTHER: payload_bug}))
    assert send(phones, "Mail", "New email.") == 0
    assert len(phones.all()) == 2  # a payload bug must never wipe every phone
    bad_token = fcm_error(400, "INVALID_ARGUMENT", "The registration token is not a valid FCM registration token")
    monkeypatch.setattr(fcm, "_client", lambda: FakeGoogle({TOKEN: bad_token}))
    assert send(phones, "Mail", "Again.") == 1
    assert [d["token"] for d in phones.all()] == [OTHER]


def test_a_refused_access_token_is_fetched_afresh_next_time(account, phones, monkeypatch, caplog):
    service = FakeGoogle({TOKEN: fcm_error(401, "", "Request had invalid authentication credentials.",
                                           "UNAUTHENTICATED")})
    monkeypatch.setattr(fcm, "_client", lambda: service)
    with caplog.at_level(logging.WARNING, logger="jarvis.notify.fcm"):
        assert send(phones, "Mail", "New email.") == 1
    assert fcm._access is None and len(phones.all()) == 2


def test_failures_keep_the_device_and_log_no_token(account, phones, monkeypatch, caplog):
    monkeypatch.setattr(fcm, "_client", lambda: FakeGoogle(error=OSError(f"connect {TOKEN}")))
    with caplog.at_level(logging.INFO, logger="jarvis.notify.fcm"):
        assert send(phones, "Calendar", "Standup.") == 0
    assert "OSError" in caplog.text and TOKEN not in caplog.text and len(phones.all()) == 2


def test_google_refusing_the_service_account_sends_nothing(account, phones, monkeypatch, caplog):
    service = FakeGoogle(oauth=Reply(400, {"error": "invalid_grant"}))
    monkeypatch.setattr(fcm, "_client", lambda: service)
    with caplog.at_level(logging.WARNING, logger="jarvis.notify.fcm"):
        assert send(phones, "Calendar", "Standup.") == 0
    assert service.calls == [] and "HTTP 400" in caplog.text and len(phones.all()) == 2


def test_nothing_is_sent_until_fcm_is_set_up(phones, monkeypatch, caplog, tmp_path):
    monkeypatch.setattr(fcm, "FCM_SERVICE_ACCOUNT_FILE", tmp_path / "missing.json")
    service = FakeGoogle()
    monkeypatch.setattr(fcm, "_client", lambda: service)
    with caplog.at_level(logging.WARNING, logger="jarvis.notify.fcm"):
        assert send(phones, "Calendar", "Standup.") == 0
    assert service.calls == [] and service.oauth_calls == 0 and "fcm_service_account.json" in caplog.text


def test_no_devices_sends_nothing(account, tmp_path, monkeypatch):
    service = FakeGoogle()
    monkeypatch.setattr(fcm, "_client", lambda: service)
    assert send(Devices(tmp_path / "none.json"), "Calendar", "Standup.") == 0
    assert service.oauth_calls == 0


def test_data_fits_fcm_limit():
    fields = fcm.data("T" * 500, "ह" * 5000, "/?task=1", "task-1")
    assert len(json.dumps(fields).encode()) <= fcm.MAX_PAYLOAD
    assert len(fields["title"]) == 120 and fields["body"]


def test_send_push_reaches_web_push_ios_and_android_together(account, phones, tmp_path, monkeypatch):
    service = FakeGoogle()
    monkeypatch.setattr(fcm, "_client", lambda: service)
    n = asyncio.run(push.send_push("Task 1 is done", "Done.", "/?task=1", "task-1",
                                   subscriptions=push.Subscriptions(tmp_path / "subs.json"),
                                   key_file=tmp_path / "vapid.pem", app_devices=apns.Devices(tmp_path / "ios.json"),
                                   android_devices=phones))
    assert n == 2 and len(service.calls) == 2


# ── Endpoint and panel ──────────────────────────────────────────


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


def test_push_native_stores_an_android_token(site, account):
    assert reply(PHONE | {"passcode": "000000"})[0] == 401
    assert reply(PHONE | {"passcode": "123456", "token": "nope"})[0] == 400
    assert reply(PHONE | {"passcode": "123456", "device": "android"}) == (200, {"ok": True, "sending": True})
    assert [(d["token"], d["device"]) for d in Devices(site / "fcm_devices.json").all()] == [(TOKEN, "android")]
    assert not (site / "push_devices.json").exists()  # APNs devices untouched


def test_push_native_keeps_the_token_before_fcm_is_set_up(site, monkeypatch):
    monkeypatch.setattr(fcm, "FCM_SERVICE_ACCOUNT_FILE", site / "missing.json")
    status, body = reply(PHONE | {"passcode": "123456"})
    assert status == 200 and body["sending"] is False and "fcm_service_account.json" in body["error"]
    assert len(Devices(site / "fcm_devices.json").all()) == 1


def test_push_native_off(site, monkeypatch):
    monkeypatch.setattr(server, "PUSH_ENABLED", False)
    assert reply(PHONE | {"passcode": "123456"})[0] == 409
    assert not (site / "fcm_devices.json").exists()


def test_the_panel_lists_and_removes_android_devices(site):
    Devices(site / "fcm_devices.json").add(PHONE, "android")
    found = asyncio.run(panel.call("devices", {}))
    assert [(d["kind"], d["device"]) for d in found] == [("app", "android")]
    assert asyncio.run(panel.call("device_remove", {"id": found[0]["id"]})) == []
    assert Devices(site / "fcm_devices.json").all() == []
