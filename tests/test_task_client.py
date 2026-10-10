"""Tests for jarvis.tasks.client against a fake runner on a Unix socket."""

import asyncio
from pathlib import Path

import pytest
from aiohttp import web

from jarvis.tasks import client


def test_the_runner_stopping_mid_stream_is_it_being_down_not_an_error(monkeypatch, tmp_path):
    # Relative to tmp_path: socket paths must be short (104 bytes on macOS)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(client, "RUNNER_SOCKET", Path("runner.sock"))

    async def events(request):  # one event, then the runner goes away, as on Ctrl-C
        r = web.StreamResponse()
        await r.prepare(request)
        await r.write(b'{"type": "heartbeat"}\n')
        request.transport.close()
        return r

    async def main():
        app = web.Application()
        app.router.add_get("/events", events)
        site_runner = web.AppRunner(app)
        await site_runner.setup()
        got = []
        try:
            try:
                await web.UnixSite(site_runner, str(client.RUNNER_SOCKET)).start()
            except PermissionError as e:
                pytest.skip(f"can't serve a Unix socket here (a sandbox may not allow it): {e}")
            with pytest.raises(client.RunnerUnavailable):
                async for event in client.events():
                    got.append(event)
        finally:
            await site_runner.cleanup()
        return got

    assert asyncio.run(main()) == [{"type": "heartbeat"}]
