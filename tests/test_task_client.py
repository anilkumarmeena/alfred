"""Tests for jarvis.tasks.client against a fake runner on a Unix socket."""

import asyncio
import logging
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
        await r.write(b'{"type": "alert"}\n')
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

    assert asyncio.run(main()) == [{"type": "alert"}]


def test_a_tool_finding_the_runner_down_logs_one_line_not_a_traceback(monkeypatch, caplog):
    from jarvis import tools

    async def down(*_args, **_kwargs):
        raise client.RunnerUnavailable(client.DOWN)

    monkeypatch.setattr(client, "tasks", down)
    with caplog.at_level(logging.WARNING, logger="jarvis.tools"):
        content, is_error = asyncio.run(tools.run("task_list", {}))
    assert is_error and client.DOWN in content  # the model still hears how to start it
    [record] = [r for r in caplog.records if r.name == "jarvis.tools"]
    assert record.levelno == logging.WARNING and record.exc_info is None
