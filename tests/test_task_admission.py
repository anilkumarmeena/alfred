"""Tests for when a task starts on this computer besides a free slot: memory to run it in (alfred.core.memory)."""

import asyncio
from types import SimpleNamespace

import pytest
from test_in_place import env  # noqa: F401  (a fixture)
from test_launch_status import gated_run
from test_nudges import settle, until

from alfred.core import memory, platform
from alfred.tasks import runner


@pytest.mark.parametrize("then", ["memory eases", "the first ends"])
def test_while_memory_is_short_only_the_first_task_starts(env, monkeypatch, then):
    monkeypatch.setattr(runner, "MEMORY_STEP", 0.01)
    gate = asyncio.Event()
    began = gated_run(monkeypatch, gate)
    short = [True]

    async def main():
        r = runner.Runner(capacity=3)
        r.short_of_memory = lambda: short[0]
        first = r.start("research", "first", "One")  # none runs yet: it starts whatever memory says
        await until(lambda: began == ["first"])
        second = r.start("research", "second", "Two")
        await asyncio.sleep(0.05)
        held = r.store.get(second["id"]), list(began)
        if then == "memory eases":
            short[0] = False
            await until(lambda: began == ["first", "second"])
        gate.set()
        await settle(r)
        logged = [e["text"] for e in r.store.history(second["id"]) if e["kind"] == "memory"]
        return first, second, held, logged, [r.store.get(t["id"])["status"] for t in (first, second)]

    first, second, (held, began_then), logged, ended = asyncio.run(main())
    assert first["status"] == "running"
    assert second["status"] == held["status"] == "queued"
    assert second["activity"] == held["activity"] == runner.MEMORY_SHORT
    assert began_then == ["first"] and logged and ended == ["done", "done"]


def test_memory_is_short_at_macos_warning_level_or_with_little_available_on_linux(monkeypatch):
    monkeypatch.setattr(platform, "MAC", True)
    for level, short in ((1, False), (2, True), (4, True), (None, False)):
        monkeypatch.setattr(memory, "_mac_level", lambda level=level: level)
        assert memory.short() is short
    monkeypatch.setattr(platform, "MAC", False)
    for available, short in ((8, False), (0.5, True)):
        monkeypatch.setattr(memory.psutil, "virtual_memory", lambda a=available: SimpleNamespace(available=a, total=16))
        assert memory.short() is short


def test_this_computers_memory_reads_without_failing():
    assert memory.short() in (True, False)
    if platform.MAC:
        assert memory._mac_level() in (1, 2, 4)
