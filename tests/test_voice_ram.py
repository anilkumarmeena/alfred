"""Tests for jarvis.voice.ram footprint logging and the agent setting that returns freed memory to the system."""

import logging
import os
import sys

import psutil
import pytest

from jarvis.voice import ram


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS's footprint")
def test_a_process_footprint_is_read():
    assert ram.footprint(os.getpid()) > 10 * ram.MB
    assert ram.footprint(2**30) is None  # no such process


def test_the_tree_starts_with_the_main_process_and_logs_one_line(caplog):
    procs = ram.tree(psutil.Process())
    assert procs[0][:2] == ("main", os.getpid()) and procs[0][2] > 0
    with caplog.at_level(logging.INFO, logger="jarvis.voice.ram"):
        ram.log_tree()
    assert caplog.records[-1].message.startswith("voice memory ") and "main " in caplog.records[-1].message


def test_the_line_gives_the_total_and_each_process():
    line = ram.describe([("main", 1, 100 * ram.MB), ("speech", 2, 900 * ram.MB), ("spare", 3, 300 * ram.MB)])
    assert line == "voice memory 1300 MB: main 100 MB (pid 1), speech 900 MB (pid 2), spare 300 MB (pid 3)"


def test_measuring_never_breaks_a_call(monkeypatch):
    monkeypatch.setattr(ram, "tree", lambda main=None: 1 / 0)
    ram.log_tree()


def test_the_job_line_is_only_measured_at_debug(monkeypatch):
    calls = []
    monkeypatch.setattr(ram, "log_tree", lambda level, main: calls.append(main.pid))
    ram.log.setLevel(logging.INFO)
    try:
        ram.log_job()
        assert calls == []
        ram.log.setLevel(logging.DEBUG)
        ram.log_job()
        assert calls == [os.getppid()]
    finally:
        ram.log.setLevel(logging.NOTSET)


def test_no_minutes_no_watching():
    assert ram.watch(0) is None


def test_child_processes_hand_freed_memory_back(monkeypatch):
    from jarvis.apps import agent

    monkeypatch.delenv("MallocLargeCache", raising=False)
    monkeypatch.setattr(agent.config, "VOICE_MALLOC_LARGE_CACHE", True)
    agent._child_memory()
    assert "MallocLargeCache" not in os.environ
    monkeypatch.setattr(agent.config, "VOICE_MALLOC_LARGE_CACHE", False)
    agent._child_memory()
    assert os.environ["MallocLargeCache"] == "0"


def test_without_a_footprint_the_size_is_rss(monkeypatch):
    """Linux has no phys_footprint: VmRSS it is."""
    monkeypatch.setattr(ram, "_LIBPROC", None)
    proc = psutil.Process()
    assert ram.footprint(proc.pid) is None and ram.size(proc) > 10 * ram.MB


def test_libproc_is_only_looked_for_on_macos(monkeypatch):
    monkeypatch.setattr(ram.platform, "MAC", False)
    assert ram._libproc() is None


def test_on_linux_glibc_keeps_large_blocks_returnable(monkeypatch):
    """glibc's mmap threshold otherwise grows to the largest block freed, and later ones stay on the heap."""
    from jarvis.apps import agent

    monkeypatch.delenv("MALLOC_MMAP_THRESHOLD_", raising=False)
    monkeypatch.delenv("MallocLargeCache", raising=False)
    monkeypatch.setattr(agent.config, "VOICE_MALLOC_LARGE_CACHE", False)
    monkeypatch.setattr(agent.platform, "LINUX", False)
    agent._child_memory()
    assert "MALLOC_MMAP_THRESHOLD_" not in os.environ
    monkeypatch.setattr(agent.platform, "LINUX", True)
    agent._child_memory()
    assert os.environ["MALLOC_MMAP_THRESHOLD_"] == "131072"
