"""Tests for voice.audio_file and tools.audio: speakable text, rendering, saving, phone sharing, the job."""

import asyncio
import json
import shutil
import signal
import tempfile
import time
import wave
from pathlib import Path

import numpy as np
import pytest

from conftest import use_profile
from jarvis import config, server, tools
from jarvis.apps import audio_file as audio_job
from jarvis.core import platform
from jarvis.core.speakable import speakable
from jarvis.media import audio_file as af
from jarvis.state import preferences, shared_files
from jarvis.tasks import client as task_client, worker


def fake_speak(text: str, voice: str, speed: float) -> np.ndarray:
    """0.1 s of tone per word, so file length tracks the text."""
    return np.full(int(af.RATE * 0.1) * len(text.split()), 0.1, dtype=np.float32)


def frames(path) -> int:
    with wave.open(str(path)) as w:
        assert (w.getnchannels(), w.getsampwidth(), w.getframerate()) == (1, 2, af.RATE)
        return w.getnframes()


mac_only = pytest.mark.skipif(not platform.MAC, reason="the Mac's encoders and wording")


@pytest.fixture
def work(tmp_path, monkeypatch):
    use_profile(monkeypatch, tmp_path)
    return tmp_path


# ── Speakable text and parts ────────────────────────────────────


def test_markdown_is_read_as_plain_speech():
    text = speakable("""# Monday briefing

**Good morning.** See [the plan](https://example.com/plan?x=(1)) and https://www.bbc.co.uk/news.
The `deploy_script` ran; my_var_name stays as it is.

- Stand-up at ten
- Lunch with Priya
2) Ship it

> A quote

```python
print("hi")
```
| Item | Owner |
|---|---|
| Docs | Ana |
---
A paragraph wrapped
across lines, as a PDF's text is.""")
    assert text.split("\n\n") == [
        "Monday briefing.",
        "Good morning. See the plan and bbc.co.uk. The deploy_script ran; my_var_name stays as it is.",
        "Stand-up at ten. Lunch with Priya. Ship it.",
        "A quote",
        'print("hi")',
        "Item, Owner.",
        "Docs, Ana.",
        "A paragraph wrapped across lines, as a PDF's text is.",
    ]


def test_parts_keep_every_word_in_order_and_within_the_limit():
    long_sentence = " ".join(f"word{i}," if i % 7 == 0 else f"word{i}" for i in range(120)) + "."
    text = "\n\n".join(["Short one. Another short one!", long_sentence, "x" * 90 + " end.", "Last."])
    found = af.parts(text, limit=60)
    assert all(len(p) <= 60 for p, _ in found)
    assert " ".join(p for p, _ in found).split() == " ".join(
        ["Short one. Another short one!", long_sentence, "x" * 60, "x" * 30, "end.", "Last."]).split()
    # a paragraph's last part is marked for a longer pause
    ends = [p for p, end in found if end]
    assert ends[0] == "Short one. Another short one!" and ends[-1] == "Last." and len(ends) == 4


def test_sentences_are_joined_up_to_the_limit_and_never_split_needlessly():
    found = af.parts("One two three. Four five six. Seven eight nine.", limit=31)
    assert found == [("One two three. Four five six.", False), ("Seven eight nine.", True)]
    assert af.parts("  \n\n  ") == []


def test_lengths_and_estimates_are_said_plainly():
    assert [af.length(s) for s in (0.2, 40, 89, 720, 3600, 3900)] == [
        "1 second", "40 seconds", "1 minute", "12 minutes", "1 hour", "1 hour 5 minutes"]
    assert af.seconds_of("word " * 250) == pytest.approx(100)
    assert af.seconds_of("word " * 250, speed=1.25) == pytest.approx(80)
    assert af.lang("bm_george") == "en-gb" and af.lang("af_heart") == "en-us" and af.lang("hf_alpha") == "hi"


def test_file_names_are_safe_and_never_overwrite(tmp_path):
    assert af.file_name('Q3: "plans"/notes?') == "Q3 plans notes"
    assert af.file_name("...") == "JARVIS audio"
    assert len(af.file_name("x" * 200)) == 80
    (tmp_path / "Brief.m4a").touch()
    (tmp_path / "Brief 2.m4a").touch()
    assert af.free_path(tmp_path, "Brief", "m4a") == tmp_path / "Brief 3.m4a"
    assert af.free_path(tmp_path, "Brief", "mp3") == tmp_path / "Brief.mp3"


# ── Rendering ───────────────────────────────────────────────────


def test_parts_are_read_one_at_a_time_and_joined_with_pauses(tmp_path):
    text = "One two three. Four five.\n\nSix seven."
    heard, lines = [], []

    def speak(text, voice, speed):
        heard.append((text, voice, speed))
        return fake_speak(text, voice, speed)

    seconds = af.render(text, tmp_path / "out.wav", speak, "bf_emma", 1.2, lines.append)
    assert heard == [("One two three. Four five.", "bf_emma", 1.2), ("Six seven.", "bf_emma", 1.2)]
    words, pause = int(af.RATE * 0.1), int(af.RATE * af.PARAGRAPH_PAUSE)
    assert frames(tmp_path / "out.wav") == 7 * words + pause
    assert seconds == pytest.approx((7 * words + pause) / af.RATE)
    assert lines[0].startswith("Read part 1 of 2") and lines[-1].startswith("Read part 2 of 2")
    with pytest.raises(ValueError, match="nothing to read"):
        af.render(" ", tmp_path / "none.wav", speak, "bf_emma", 1.0)


def spec(folder, fmt="wav", **more) -> dict:
    return {"text": "Hello there. This is a test.", "out": str(folder / f"Test.{fmt}"), "format": fmt,
            "voice": "bm_george", "speed": 1.0, "title": "Test", "phone": False} | more


def test_a_file_is_saved_where_asked_and_says_where(work):
    said = af.make(spec(work / "Music"), fake_speak, ["bm_george"])
    assert (work / "Music" / "Test.wav").is_file() and frames(work / "Music" / "Test.wav") > 0
    assert said == f"Saved as Test.wav in {af.shown(work / 'Music')}, 1 second of audio."
    assert not list((work / "audio").glob("*.wav"))  # nothing left behind
    # a second render gets a new name, never overwrites
    af.make(spec(work / "Music"), fake_speak, ["bm_george"])
    assert (work / "Music" / "Test 2.wav").is_file()


@mac_only
def test_without_an_encoder_the_wav_is_kept_and_it_says_why(work, monkeypatch):
    monkeypatch.setattr(af, "encoders", lambda fmt: [])
    said = af.make(spec(work, "mp3"), fake_speak, ["bm_george"])
    assert (work / "Test.wav").is_file() and not (work / "Test.mp3").exists()
    assert "Saved as Test.wav" in said and "no mp3 encoder (ffmpeg or lame)" in said


def test_a_failing_encoder_leaves_the_wav_and_its_reason(work, monkeypatch):
    monkeypatch.setattr(af, "encoders", lambda fmt: [lambda wav, out: ["/bin/sh", "-c", "echo bad input >&2; exit 3"]])
    said = af.make(spec(work, "m4a"), fake_speak, ["bm_george"])
    assert (work / "Test.wav").is_file() and "sh: bad input" in said


def test_a_folder_it_cant_write_to_sends_the_file_to_its_own(work, monkeypatch):
    monkeypatch.setattr(af, "_writable", lambda folder: folder == af.work_dir())
    said = af.make(spec(work / "Downloads"), fake_speak, ["bm_george"])
    assert (work / "audio" / "Test.wav").is_file()
    assert f"couldn't write to {af.shown(work / 'Downloads')}" in said


def test_an_unknown_voice_is_refused(work):
    with pytest.raises(ValueError, match="no voice bm_nobody"):
        af.make(spec(work, voice="bm_nobody"), fake_speak, ["bm_george", "af_heart"])


@mac_only
def test_only_the_encoders_on_this_mac_are_used_best_first(monkeypatch):
    monkeypatch.setattr(af, "find_program", lambda name: f"/opt/homebrew/bin/{name}" if name != "ffmpeg" else None)
    [lame] = af.encoders("mp3")
    assert lame(Path("a.wav"), Path("a.mp3")) == ["/opt/homebrew/bin/lame", "--quiet", "-b", "64", "a.wav", "a.mp3"]
    [afconvert] = af.encoders("m4a")
    assert afconvert(Path("a.wav"), Path("a.m4a"))[:2] == ["/opt/homebrew/bin/afconvert", "-f"]
    assert af.encoders("wav") == [] and af.can_make("wav")
    assert af.programs("m4a") == "afconvert or ffmpeg"


def test_on_linux_m4a_needs_ffmpeg_and_homebrew_isnt_looked_in(work, monkeypatch, tmp_path):
    monkeypatch.setattr(platform, "MAC", False)
    monkeypatch.setattr(platform, "LINUX", True)
    brew = tmp_path / "brew"
    brew.mkdir()
    (brew / "ffmpeg").write_text("#!/bin/sh")
    (brew / "ffmpeg").chmod(0o755)
    monkeypatch.setattr(af, "BREW_BINS", (str(brew),))
    monkeypatch.setattr(af.shutil, "which", lambda name: "/usr/bin/afconvert" if name == "afconvert" else None)
    assert af.find_program("ffmpeg") is None and af.encoders("m4a") == []  # afconvert is the Mac's alone
    assert af.programs("m4a") == "ffmpeg" and af.programs("mp3") == "ffmpeg or lame"
    said = af.make(spec(work, "m4a"), fake_speak, ["bm_george"])
    assert "Saved as Test.wav" in said and "no m4a encoder on this computer: install ffmpeg" in said
    assert "Mac" not in said
    monkeypatch.setattr(af.shutil, "which", lambda name: f"/usr/bin/{name}" if name == "ffmpeg" else None)
    [ffmpeg] = af.encoders("m4a")
    assert ffmpeg(Path("a.wav"), Path("a.m4a"))[0] == "/usr/bin/ffmpeg"


def _afconvert_works() -> bool:
    if not shutil.which("afconvert"):
        return False
    with tempfile.TemporaryDirectory() as d:
        wav = Path(d) / "a.wav"
        with wave.open(str(wav), "wb") as w:
            w.setnchannels(1), w.setsampwidth(2), w.setframerate(af.RATE), w.writeframes(bytes(4800))
        return af.encode(wav, Path(d) / "a.m4a", "m4a")[1] == ""


@pytest.mark.skipif(not _afconvert_works(), reason="needs the Mac's afconvert, with its AAC encoder (not in a sandbox)")
def test_m4a_is_made_with_the_macs_own_encoder(work):
    said = af.make(spec(work, "m4a"), fake_speak, ["bm_george"])
    m4a = work / "Test.m4a"
    assert m4a.is_file() and not (work / "Test.wav").exists() and said.startswith("Saved as Test.m4a")
    assert b"ftyp" in m4a.read_bytes()[:16]


@pytest.fixture
def job(work, monkeypatch):
    """Run the job in-process with Kokoro, priority and the SIGTERM handler faked."""
    monkeypatch.setattr(af, "kokoro", lambda: (fake_speak, ["bm_george", "bf_emma", "af_heart"]))
    monkeypatch.setattr(af.os, "nice", lambda n: None)
    stopping = []
    monkeypatch.setattr(af.signal, "signal", lambda sig, handler: stopping.append((sig, handler)))
    return stopping


def test_the_job_renders_one_file_at_a_time_and_tidies_up(work, job, capsys):
    file = work / "spec.json"
    file.write_text(json.dumps(spec(work)))
    assert af.main([str(file)]) == 0
    out = capsys.readouterr().out.strip().splitlines()
    assert out[-1].startswith("Saved as Test.wav") and not file.exists()  # the source text isn't kept
    assert (work / "audio" / "render.lock").exists()

    file.write_text(json.dumps(spec(work, voice="bx_none")))
    assert af.main([str(file)]) == 1
    assert capsys.readouterr().out.strip().splitlines()[-1] == (
        "It couldn't be made: Kokoro has no voice bx_none. It has bf_emma, bm_george, and others.")
    assert not file.exists()


def test_a_stopped_job_leaves_nothing_behind(work, job, monkeypatch):
    def stopped(text, voice, speed):
        job[0][1](signal.SIGTERM, None)  # what stop_job's SIGTERM runs

    monkeypatch.setattr(af, "kokoro", lambda: (stopped, ["bm_george"]))
    file = work / "spec.json"
    file.write_text(json.dumps(spec(work)))
    with pytest.raises(SystemExit) as e:
        af.main([str(file)])
    assert e.value.code == 128 + signal.SIGTERM and job[0][0] == signal.SIGTERM
    assert not file.exists() and not list((work / "audio").glob("*.wav")) and not (work / "Test.wav").exists()


# ── The phone ───────────────────────────────────────────────────


def test_a_shared_file_is_found_by_its_token_until_it_expires(tmp_path, monkeypatch):
    index = tmp_path / "shared.json"
    (tmp_path / "a.m4a").write_bytes(b"x")
    token = shared_files.share(tmp_path / "a.m4a", index)
    assert len(token) >= 32 and shared_files.shared(token, index) == tmp_path / "a.m4a"
    assert shared_files.shared("nope" * 10, index) is None
    assert oct(index.stat().st_mode & 0o777) == "0o600"
    assert shared_files.link(token, "My brief.m4a") == f"/?file={token}&name=My+brief.m4a"
    (tmp_path / "a.m4a").unlink()
    assert shared_files.shared(token, index) is None  # gone from the Mac
    (tmp_path / "a.m4a").write_bytes(b"x")
    later = time.time() + shared_files.SHARE_DAYS * 86400 + 1
    monkeypatch.setattr(shared_files.time, "time", lambda: later)
    assert shared_files.shared(token, index) is None
    shared_files.share(tmp_path / "a.m4a", index)  # sharing another drops the expired ones
    assert token not in json.loads(index.read_text())


def test_sent_to_the_phone_as_a_push_that_plays_it(work, monkeypatch):
    monkeypatch.setattr(config, "PUSH_ENABLED", True)
    pushed = []

    async def send_push(title, body, url="/", tag=""):
        pushed.append((title, body, url, tag))
        return 1

    from jarvis.notify import push
    monkeypatch.setattr(push, "send_push", send_push)
    said = af.make(spec(work, phone=True), fake_speak, ["bm_george"], audio_job.deliver)
    assert said.endswith("Sent to the phone.")
    [(title, body, url, tag)] = pushed
    assert title == "Audio ready" and body == "Test: 1 second. Tap to listen."
    token = url.split("file=")[1].split("&")[0]
    assert shared_files.shared(token) == work / "Test.wav" and tag == f"file-{token[:12]}"

    pushed.clear()
    monkeypatch.setattr(push, "send_push", lambda *a, **k: asyncio.sleep(0, 0))
    assert af.make(spec(work, phone=True), fake_speak, ["bm_george"], audio_job.deliver).endswith(
        "no phone has JARVIS's notifications on.")
    monkeypatch.setattr(config, "PUSH_ENABLED", False)
    assert "notifications are off on the computer" in af.make(spec(work, phone=True), fake_speak, ["bm_george"], audio_job.deliver)


class Request:
    def __init__(self, token: str) -> None:
        self.match_info = {"token": token}


def test_the_web_app_serves_a_shared_file_and_nothing_else(tmp_path, monkeypatch):
    use_profile(monkeypatch, tmp_path)
    (tmp_path / "Brief é.m4a").write_bytes(b"audio")
    token = shared_files.share(tmp_path / "Brief é.m4a")
    found = asyncio.run(server.shared_file(Request(token)))
    assert found._path == tmp_path / "Brief é.m4a"
    assert found.headers["Content-Disposition"] == "inline; filename*=UTF-8''Brief%20%C3%A9.m4a"
    assert asyncio.run(server.shared_file(Request("x" * 43))).status == 404
    routes = {r.resource.canonical for r in server.app().router.routes() if r.method == "GET"}
    assert "/api/files/{token}" in routes


# ── The tool and its job ────────────────────────────────────────


class FakeRunner:
    """Runner's start_command as the client sees it: records each job."""

    def __init__(self) -> None:
        self.launched = []

    async def start_command(self, command, title, folder, timeout_minutes=None, summarize=True, auto_fix=True,
                            report=False):
        self.launched.append((command, title, folder, summarize, auto_fix, report))
        return {"id": 7, "title": title}


@pytest.fixture
def jobs(work, monkeypatch):
    fake = FakeRunner()
    monkeypatch.setattr(task_client, "start_command", fake.start_command)
    monkeypatch.setattr(config, "AUDIO_DIR", work / "Downloads")
    monkeypatch.setattr(config, "AUDIO_FORMAT", "m4a")
    monkeypatch.setattr(af, "encoders", lambda fmt: [lambda wav, out: ["true"]])
    monkeypatch.setattr(preferences, "tts_voice", lambda: "bm_george")
    monkeypatch.setattr(preferences, "tts_speed", lambda: 1.0)
    return fake


def run(**args):
    return asyncio.run(tools.run("audio_file", args))


def test_the_tool_starts_a_job_and_says_how_long_and_where(jobs, work):
    said, error = run(text="## Summary\n\n" + "Plenty of words here. " * 50, title="Monday briefing")
    assert not error
    assert said.startswith("Started job 7, the audio file Monday briefing: about 1 minute of audio, to be saved as "
                           f"Monday briefing.m4a in {af.shown(work / 'Downloads')}. Making it takes roughly 1 minute.")
    assert said.endswith("JARVIS says when it's done.")
    [(command, title, folder, summarize, auto_fix, report)] = jobs.launched
    assert title == "the audio file Monday briefing" and folder == str(config.ROOT)
    assert report and not summarize and not auto_fix  # its last line says what it made; nothing to fix if it fails
    assert " -m jarvis.apps.audio_file " in command
    [file] = (work / "audio").glob("*.json")
    assert oct(file.stat().st_mode & 0o777) == "0o600"  # private: holds the user's text
    assert str(file) in command
    asked = json.loads(file.read_text())
    assert asked["text"].startswith("Summary.\n\nPlenty of words here.")
    assert asked | {"text": ""} == {"text": "", "out": str(work / "Downloads" / "Monday briefing.m4a"), "format": "m4a",
                                    "voice": "bm_george", "speed": 1.0, "title": "Monday briefing", "phone": False}


def test_the_tool_reads_a_document_on_the_mac(jobs, work):
    (work / "notes.txt").write_text("Chapter one.\n\nIt was a bright cold day.")
    said, error = run(path=str(work / "notes.txt"), format="wav", voice="af_heart", phone=True)
    assert not error and "to be saved as notes.wav" in said and "then it goes to the phone" in said
    [file] = (work / "audio").glob("*.json")
    asked = json.loads(file.read_text())
    assert asked["text"] == "Chapter one.\n\nIt was a bright cold day."
    assert (asked["voice"], asked["phone"], asked["title"]) == ("af_heart", True, "notes")


def test_the_tool_names_a_file_from_the_texts_first_words(jobs):
    said, _ = run(text="Remind me: the quarterly numbers are due, and soon.")
    assert "to be saved as Remind me the quarterly numbers are.m4a" in said


@pytest.mark.parametrize("args, problem", [
    ({}, "either the text"),
    ({"text": "Hi.", "path": "x.txt"}, "not both"),
    ({"text": "Hi.", "voice": "George"}, "isn't a Kokoro voice"),
    ({"text": "```\n```"}, "nothing in it to read"),
])
def test_the_tool_refuses_what_it_cant_read(jobs, args, problem):
    said, error = run(**args)
    assert error and problem in said and not jobs.launched


@pytest.mark.parametrize("mac, how", [(True, "brew install ffmpeg"), (False, "system's package manager")])
def test_mp3_needs_an_encoder_and_says_how_to_get_one(jobs, monkeypatch, mac, how):
    monkeypatch.setattr(platform, "MAC", mac)
    monkeypatch.setattr(af, "encoders", lambda fmt: [] if fmt == "mp3" else [lambda wav, out: ["enc"]])
    said, error = run(text="Hello.", format="mp3")
    assert error and how in said and "m4a works without it" in said


def test_without_any_encoder_m4a_is_not_offered_instead(jobs, monkeypatch):
    monkeypatch.setattr(af, "encoders", lambda fmt: [])  # Linux without ffmpeg: m4a needs it too
    said, error = run(text="Hello.", format="mp3")
    assert error and "m4a works without it" not in said


def test_the_tool_is_the_voices_and_not_tasks():
    t = tools.get("audio_file")
    assert t and t.changes and not t.gated
    assert "audio_file" not in worker.READ_ONLY_TOOLS


