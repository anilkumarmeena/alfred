"""The phone's mic and earphone handling: runs the web app's tests (web/tests/, for web/src/lib/mic.ts)
with vitest, and checks the endpoint the page traces earphone presses to. Run with: make test"""

import asyncio
import json
import logging
import shutil
import subprocess

import pytest

from jarvis import server
from jarvis.core import config

WEB = config.ROOT / "web"


@pytest.mark.skipif(not shutil.which("npm") or not (WEB / "node_modules").is_dir(),
                    reason="needs Node.js and the web app's packages (make web-install)")
def test_web_mic():
    done = subprocess.run(["npm", "test"], cwd=WEB, capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stdout + done.stderr


class Request:
    """Just enough of aiohttp's request for the handler (the sandboxed test run can't open a port)."""

    remote = "100.64.0.2"

    def __init__(self, body) -> None:
        self.body = body

    async def json(self):
        return self.body


def reply(body) -> tuple[int, dict]:
    r = asyncio.run(server.phone_log(Request(body)))
    return r.status, json.loads(r.text)


def test_phone_trace_lands_in_the_web_log(monkeypatch, caplog):
    monkeypatch.setenv("JARVIS_PASSCODE", "123456")  # no login yet: the first-setup passcode
    server._failures.clear()
    assert ("POST", "/api/log") in {(r.method, r.resource.canonical) for r in server.app().router.routes()}

    assert reply({"passcode": "000000", "lines": ["x"]})[0] == 401
    assert reply({"passcode": "123456", "lines": "not a list"})[0] == 400
    lines = ["10:00:00.000 togglemicrophone(off): mute", "evil\nFAKE LOG LINE", "x" * 1000] + ["more"] * 60
    with caplog.at_level(logging.INFO, logger="jarvis.server"):
        assert reply({"passcode": "123456", "device": "iPhone", "lines": lines}) == (200, {"ok": True})
    logged = [r.getMessage() for r in caplog.records if r.name == "jarvis.server" and r.levelno == logging.INFO]
    assert logged[0] == "iphone: 10:00:00.000 togglemicrophone(off): mute"
    assert logged[1] == "iphone: evil?FAKE LOG LINE"  # one line per line: no forged entries
    assert len(logged[2]) == len("iphone: ") + 300
    assert len(logged) == server.MAX_LOG_LINES
