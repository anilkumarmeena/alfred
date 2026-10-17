"""Tests for jarvis.server: passcode-protected endpoints, bad input, lockout, and LiveKit token contents.

Handlers are called directly; the sandboxed test run can't open a port.
"""

import asyncio
import json
import logging
import re

import pytest
from livekit import api

from conftest import use_profile
from jarvis import server
from jarvis.auth import logins
from jarvis.state import todo
from jarvis.voice import quiet

KEY, SECRET = "devkey", "a-livekit-secret-long-enough-to-sign-with"
LOGIN, RIGHT, WRONG = "Robin", "123456", "000000"  # one login, hashed in a scratch logins.json, and its passcode
# Passcode endpoints, and public ones (guarded otherwise or with nothing to guard)
AUTHED = {"/api/token", "/api/push/subscribe", "/api/push/unsubscribe", "/api/push/native", "/api/log",
          "/api/picture", "/api/picture/drop", "/api/panel/{action}", "/api/events"}
# A machine's link signs in with its keys at the task runner; pairing takes a one-time code
PUBLIC = {"/", "/sw.js", "/api/push/key", "/api/approvals/{id}", "/api/files/{token}", "/api/google/callback",
          "/api/node", "/api/node/pair"}


class Request:
    """Minimal aiohttp request; a `body` that is an exception is raised on read."""

    remote = "100.64.0.2"

    def __init__(self, body: dict | list | Exception | None = None, host: str = "mac.tailnet.ts.net",
                 headers: dict | None = None, match_info: dict | None = None) -> None:
        self.body, self.host, self.headers, self.match_info = body, host, headers or {}, match_info or {}

    async def json(self):
        if isinstance(self.body, Exception):
            raise self.body
        return self.body


@pytest.fixture
def site(monkeypatch, tmp_path):
    monkeypatch.setattr(logins, "FILE", tmp_path / "logins.json")
    logins._known.clear()
    logins.add(LOGIN, RIGHT)
    monkeypatch.setenv("LIVEKIT_API_KEY", KEY)
    monkeypatch.setenv("LIVEKIT_API_SECRET", SECRET)
    server._failures.clear()
    yield
    server._failures.clear()


def call(handler, request: Request) -> tuple[int, dict]:
    r = asyncio.run(handler(request))
    return r.status, json.loads(r.text)


def token(body: dict | None = None, **request) -> tuple[int, dict]:
    return call(server.token, Request({"passcode": RIGHT} | (body or {}), **request))


def test_a_token_joins_a_fresh_room_of_its_own_as_the_user(site):
    status, got = token({"device": "iPhone 15!"})
    assert status == 200 and set(got) == {"token", "url", "build"}
    claims = api.TokenVerifier(KEY, SECRET).verify(got["token"])
    assert re.fullmatch(r"robin-iphone15-[0-9a-f]{4}", claims.identity)
    assert claims.name == LOGIN  # whose passcode it was
    video = claims.video
    assert video is not None and video.room_join and video.can_publish and video.can_subscribe
    assert video.can_update_own_metadata and re.fullmatch(r"jarvis-[0-9a-f]{8}", video.room or "")
    assert (claims.attributes or {})[quiet.SPEAKER] == "on"
    other = api.TokenVerifier(KEY, SECRET).verify(token()[1]["token"])
    assert other.video is not None and other.video.room != video.room  # each connection a session of its own
    assert other.identity.split("-")[-2] == "device"  # no device said


def test_a_token_carries_the_speaker_switch_and_isnt_cached(site):
    r = asyncio.run(server.token(Request({"passcode": RIGHT, "speaker": "off"})))
    assert r.headers["Cache-Control"] == "no-store"
    claims = api.TokenVerifier(KEY, SECRET).verify(json.loads(r.text)["token"])
    assert (claims.attributes or {})[quiet.SPEAKER] == "off"


@pytest.mark.parametrize("host, headers, url", [
    ("localhost:8787", {}, server.LIVEKIT_URL),
    ("127.0.0.1:8787", {}, server.LIVEKIT_URL),
    ("mac.tailnet.ts.net", {}, "wss://mac.tailnet.ts.net"),
    ("127.0.0.1:8787", {"X-Forwarded-Host": "mac.tailnet.ts.net"}, "wss://mac.tailnet.ts.net"),  # Tailscale Serve
])
def test_a_browser_on_the_mac_reaches_livekit_directly_and_the_rest_through_serve(site, host, headers, url):
    assert token(host=host, headers=headers)[1]["url"] == url


@pytest.mark.parametrize("body", [{"passcode": WRONG}, {}, {"passcode": None}, ["123456"], ValueError("not JSON")])
def test_a_wrong_or_missing_passcode_gets_no_token_and_counts_a_failure(site, body, caplog):
    status, got = call(server.token, Request(body))
    assert status == 401 and got == {"error": "Wrong passcode."}
    assert len(server._failures) == 1 and "wrong passcode from 100.64.0.2" in caplog.text
    assert RIGHT not in caplog.text and WRONG not in caplog.text


def test_with_no_login_set_nothing_gets_in(site, monkeypatch, tmp_path):
    monkeypatch.setattr(logins, "FILE", tmp_path / "none.json")
    monkeypatch.delenv(logins.VAR, raising=False)  # nor the first-setup passcode
    assert call(server.token, Request({"passcode": ""}))[0] == 401
    assert call(server.token, Request({}))[0] == 401


def test_too_many_wrong_passcodes_lock_everyone_out_until_the_window_passes(site, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(server.time, "time", lambda: now[0])
    for _ in range(server.MAX_FAILURES - 1):
        call(server.token, Request({"passcode": WRONG}))
    assert token()[0] == 200  # one short of the limit
    call(server.token, Request({"passcode": WRONG}))
    status, got = token()
    assert status == 429 and "Too many" in got["error"]
    assert len(server._failures) == server.MAX_FAILURES  # a locked-out request isn't counted again
    approval = asyncio.run(server.approval(Request({"nonce": "n", "decision": "approve"}, match_info={"id": "1"})))
    assert approval.status == 429  # the push's Approve button shares the lockout
    now[0] += server.FAILURE_WINDOW
    assert token()[0] == 200 and server._failures == []


def test_every_endpoint_but_the_public_ones_needs_the_passcode(site):
    routes = {r.resource.canonical: r.handler for r in server.app().router.routes() if r.resource is not None
              and not r.resource.canonical.startswith("/static")}
    assert set(routes) == AUTHED | PUBLIC  # a new endpoint is added to one or the other here, on purpose
    for path in AUTHED:
        assert getattr(routes[path], "__wrapped__", None), f"{path} isn't behind the passcode"
        status, got = call(routes[path], Request({"passcode": WRONG}, match_info={"action": "tasks"}))
        assert (path, status, got) == (path, 401, {"error": "Wrong passcode."})
    for path in PUBLIC:
        assert not hasattr(routes[path], "__wrapped__")


def test_a_shared_file_link_that_isnt_known_finds_nothing(site):
    r = asyncio.run(server.shared_file(Request(match_info={"token": "x" * 32})))
    assert r.status == 404 and len(server._failures) == 0


def test_the_phone_log_takes_lines_only_and_cleans_them(site, caplog):
    caplog.set_level(logging.INFO, "jarvis.server")
    assert call(server.phone_log, Request({"passcode": RIGHT, "lines": "one"}))[0] == 400
    lines = [f"press {i} ☃\x1b[31m" for i in range(server.MAX_LOG_LINES + 5)]
    sent = Request({"passcode": RIGHT, "device": "iPhone", "lines": lines})
    assert call(server.phone_log, sent) == (200, {"ok": True})
    logged = [r.getMessage() for r in caplog.records if r.getMessage().startswith("iphone: press")]
    assert len(logged) == server.MAX_LOG_LINES and logged[0] == "iphone: press 0 ??[31m"


def test_the_event_stream_sends_the_to_do_list_when_it_changes(site, monkeypatch, tmp_path):
    use_profile(monkeypatch, tmp_path)
    monkeypatch.setattr(server, "ACTIVITY_SECONDS", 0.01)

    async def runner_says_nothing():
        await asyncio.Event().wait()
        yield

    sent = []

    class Stream:  # records the lines; the client hangs up once the list arrives
        def __init__(self, **_) -> None: ...

        async def prepare(self, _) -> None: ...

        async def write(self, line: bytes) -> None:
            sent.append(json.loads(line))
            if sent[-1]["type"] == "hello":  # by voice, say, once the stream is watching
                asyncio.get_running_loop().call_later(0.05, todo.add, "Buy milk")
            elif sent[-1]["type"] == "todos":
                raise ConnectionResetError

    monkeypatch.setattr(server.client, "events", runner_says_nothing)
    monkeypatch.setattr(server.web, "StreamResponse", Stream)
    asyncio.run(asyncio.wait_for(server.events(Request({"passcode": RIGHT})), 5))
    assert [e["type"] for e in sent] == ["hello", "todos"]
    assert [t["text"] for t in sent[1]["todos"]] == ["Buy milk"]
