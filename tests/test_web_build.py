"""Tests for the web build id jarvis.web serves; an open page compares it to reload after a rebuild."""

import asyncio
import json

from jarvis import server


def test_build_id_comes_from_the_built_page(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "WEB_DIR", tmp_path)
    assert server.build_id() == ""  # not built
    (tmp_path / "index.html").write_text('<head>\n  <meta name="jarvis-build" content="6d98345078f4">\n<title>J</title>')
    assert server.build_id() == "6d98345078f4"
    (tmp_path / "index.html").write_text('<head><meta name="jarvis-build" content="a1b2c3d4e5f6"></head>')
    assert server.build_id() == "a1b2c3d4e5f6"  # read each time: a rebuild shows at once
    (tmp_path / "index.html").write_text("<head><title>dev</title></head>")
    assert server.build_id() == ""


class Request:
    """Minimal aiohttp request (the sandboxed test run can't open a port)."""

    remote = "127.0.0.1"
    host = "localhost:8787"
    headers: dict = {}

    def __init__(self, body) -> None:
        self.body = body

    async def json(self):
        return self.body


def test_token_says_which_build_is_served(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_PASSCODE", "123456")  # no login yet: the first-setup passcode
    monkeypatch.setattr(server, "WEB_DIR", tmp_path)
    monkeypatch.setenv("LIVEKIT_API_KEY", "key")
    monkeypatch.setenv("LIVEKIT_API_SECRET", "a-secret-long-enough-for-the-jwt-signer-0123456789")
    server._failures.clear()
    (tmp_path / "index.html").write_text('<meta name="jarvis-build" content="0123456789ab">')
    r = asyncio.run(server.token(Request({"passcode": "123456", "device": "browser"})))
    assert r.status == 200
    body = json.loads(r.text)
    assert body["build"] == "0123456789ab"
    assert body["token"] and body["url"]
