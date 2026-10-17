"""Tests for locked updates of files more than one process reads, changes and rewrites (jarvis.core.files.update):
memory.md, vocabulary.md, recent.md, the Claude login checks and usage records. Two changes at once both survive."""

import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from conftest import use_profile
from jarvis import config
from jarvis.core import files
from jarvis.llm import claude_logins, usage
from jarvis.state import memory, vocabulary
from jarvis.storage import profile
from jarvis.tasks import data_edits

PAUSE = 0.2  # long enough that two unlocked updates would both read before either wrote


def slowed(monkeypatch, module, name: str) -> None:
    """Make `module.name` (a step between reading the file and writing it) pause, so updates overlap."""
    real = getattr(module, name)

    def slow(*args, **kwargs):
        out = real(*args, **kwargs)
        time.sleep(PAUSE)
        return out

    monkeypatch.setattr(module, name, slow)


def together(*calls) -> list:
    """Run `calls` at the same time (each opens its own lock file, so flock keeps them apart as it would processes)."""
    with ThreadPoolExecutor(len(calls)) as pool:
        return [f.result() for f in [pool.submit(c) for c in calls]]


@pytest.fixture
def data(tmp_path, monkeypatch):
    use_profile(monkeypatch, tmp_path)
    return tmp_path


UPDATE = """
import sys, time
from pathlib import Path
from jarvis.core import files

def change(text):
    time.sleep(float(sys.argv[3]))
    return text + sys.argv[2] + "\\n"

files.update(Path(sys.argv[1]), change)
"""


def updating(file, line: str, pause: float = PAUSE) -> subprocess.Popen:
    """Another process adding `line` to `file` through files.update, pausing between reading and writing."""
    return subprocess.Popen([sys.executable, "-c", UPDATE, str(file), line, str(pause)], cwd=config.ROOT)


# ── The helper ──────────────────────────────────────────────────


def test_two_processes_updating_a_file_at_once_both_keep_their_change(tmp_path):
    file = tmp_path / "shared.md"
    file.write_text("first\n")
    procs = [updating(file, "from one"), updating(file, "from two")]
    assert [p.wait(timeout=30) for p in procs] == [0, 0]
    assert sorted(file.read_text().splitlines()) == ["first", "from one", "from two"]


def test_an_update_waits_while_another_process_holds_the_lock(tmp_path):
    file = tmp_path / "shared.md"
    with files.locked(file):
        proc = updating(file, "later", pause=0)
        time.sleep(1)
        assert proc.poll() is None and not file.exists()
        file.write_text("mine\n")
    assert proc.wait(timeout=30) == 0
    assert file.read_text() == "mine\nlater\n"


def test_an_update_replaces_the_file_whole_and_keeps_its_mode(tmp_path):
    file = tmp_path / "shared.md"
    file.write_text("old\n")
    file.chmod(0o640)
    before = file.stat().st_ino
    assert files.update(file, lambda text: text + "new\n") == "old\nnew\n"
    assert file.read_text() == "old\nnew\n" and file.stat().st_ino != before
    assert file.stat().st_mode & 0o777 == 0o640


def test_a_missing_file_reads_as_empty_and_an_unchanged_one_is_left_alone(tmp_path):
    file = tmp_path / "shared.md"
    files.update(file, lambda text: text + "made\n")
    assert file.read_text() == "made\n"
    before = file.stat().st_ino
    files.update(file, lambda text: text)
    assert file.stat().st_ino == before


def test_a_change_that_fails_writes_nothing(tmp_path):
    file = tmp_path / "shared.md"
    file.write_text("kept\n")

    def fail(text):
        raise ValueError("no")

    with pytest.raises(ValueError):
        files.update(file, fail)
    assert file.read_text() == "kept\n"
    assert sorted(os.listdir(tmp_path)) == [".shared.md.lock", "shared.md"]  # the lock hidden, no temp file left


# ── Its callers ─────────────────────────────────────────────────


def test_two_memories_saved_at_once_are_both_kept(data, monkeypatch):
    slowed(monkeypatch, memory, "parse")
    together(lambda: memory.remember("Likes tea", "user"), lambda: memory.remember("Uses a standing desk", "user"))
    kept = memory.facts()["user"]
    assert any("Likes tea" in line for line in kept) and any("Uses a standing desk" in line for line in kept)


def test_a_task_s_memory_edit_and_a_voice_memory_at_once_are_both_kept(data, monkeypatch):
    memory.remember("Works from home", "user")
    old = memory.facts()["user"][0]
    slowed(monkeypatch, memory, "check_text")
    together(lambda: data_edits._apply({"action": "edit", "file": "memory.md", "old": old, "new": "- Works in an office"}),
             lambda: memory.remember("Prefers mornings", "environment"))
    text = memory.read_memory()
    assert "- Works in an office" in text and "Works from home" not in text and "Prefers mornings" in text


def test_a_memory_change_that_breaks_a_limit_saves_nothing(data):
    memory.remember("Likes tea", "user")
    before = memory.read_memory()
    with pytest.raises(ValueError, match="at most"):
        memory.remember("x" * memory.FACT_MAX, "user")
    with pytest.raises(ValueError, match="No memory line"):
        memory.replace("nothing like this", "Anything")
    assert memory.read_memory() == before


def test_two_words_added_to_the_vocabulary_at_once_are_both_kept(data, monkeypatch):
    slowed(monkeypatch, vocabulary, "entries")
    together(lambda: vocabulary.add("Siobhan", ["Shivon"]), lambda: vocabulary.add("Kubernetes", learned=True))
    words = [e.word for e in vocabulary.entries()]
    assert "Siobhan" in words and "Kubernetes" in words and "JARVIS" in words


def test_two_exchanges_logged_at_once_are_both_in_recent(data, monkeypatch):
    slowed(monkeypatch, memory, "_trimmed")
    together(lambda: memory.log_exchange("first question", "first answer"),
             lambda: memory.log_exchange("second question", "second answer"))
    recent = memory.read_recent()
    assert "first answer" in recent and "second answer" in recent


def test_recent_is_replaced_whole_never_rewritten_in_place(data):
    memory.log_exchange("hello", "Hi.")
    before = (recent := profile.current().recent_file).stat().st_ino
    memory.log_exchange("again", "Hi again.")
    assert recent.stat().st_ino != before and "Hi again." in memory.read_recent()


def test_two_logins_checked_at_once_both_keep_their_result(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CLAUDE_LOGINS_DIR", tmp_path / "claude-logins")
    slowed(monkeypatch, claude_logins, "checks")
    together(lambda: claude_logins.note("work", True, "fine"), lambda: claude_logins.note("home", False, "signed out"))
    found = json.loads((tmp_path / "claude-logins" / "checks.json").read_text())
    assert found["work"]["ok"] is True and found["home"]["ok"] is False


class Window:
    def __init__(self, kind: str) -> None:
        self.rate_limit_type, self.status, self.resets_at, self.utilization, self.overage_status = \
            kind, "allowed", 9e9, 0.5, None


def test_two_usage_records_at_once_both_keep_their_window(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CLAUDE_LOGINS_DIR", tmp_path / "claude-logins")
    monkeypatch.setattr(usage, "USAGE_FILE", tmp_path / "usage.json")
    slowed(monkeypatch, usage, "_read")
    together(lambda: usage.record(Window("five_hour"), login=""), lambda: usage.record(Window("seven_day"), login=""))
    assert set(usage.of("")) == {"five_hour", "seven_day"}
