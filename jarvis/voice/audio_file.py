"""Text read into an audio file, in Kokoro's voice, to listen to later: the audio_file tool (jarvis.tools.audio) starts
one as a long job (jarvis.safety.shell.Jobs), which runs

  python -m jarvis.voice.audio_file SPEC.json

SPEC holds the text, already made speakable (`speakable`: no markdown, links or code fences), and the file to make.
The text is read in parts of at most PART_CHARS, cut at paragraphs, then sentences, then clauses (`parts`), each part
rendered on its own and joined, with a short pause between sentences and a longer one between paragraphs, into a WAV,
which is then encoded: m4a by the Mac's own afconvert (or ffmpeg), mp3 by ffmpeg or lame, wav as it is. Without an
encoder for the format the WAV is kept, and the last line says so.

It runs apart from the live voice: its own Kokoro (config.AUDIO_MODEL, the full 32-bit model by default, which is
faster than the 8-bit one on Apple silicon), in a process of lower priority on AUDIO_THREADS cores, one file at a time
(a lock in data/audio/), so live speech never waits behind it. Each part read prints a line, which job_status shows;
the last line says where the file is, and the voice says it when the job ends. Asked to, it then shares the file with
the phone (jarvis.state.shared_files) and pushes a notification that plays it.
"""

import asyncio
import fcntl
import functools
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import wave
from collections.abc import Callable
from pathlib import Path

import numpy as np

from jarvis.core import profile
from jarvis.core.config import (
    AUDIO_MODEL,
    AUDIO_THREADS,
    ROOT,
    TTS_MEMORY_ARENA,
    TTS_MODEL,
    TTS_MODEL_FALLBACKS,
)

RATE = 24000  # Kokoro's


def work_dir() -> Path:
    """Specs, the WAV being rendered, the lock, and files that couldn't go where asked: in the profile's data folder."""
    return profile.current().audio

PART_CHARS = 400  # the most read in one go: well within Kokoro's 510 phonemes, and a part a few seconds long
SENTENCE_PAUSE, PARAGRAPH_PAUSE = 0.25, 0.7  # seconds of silence between parts
WORDS_PER_SECOND = 2.5  # Kokoro's pace at speed 1, pauses and all, for estimates
MAX_CHARS = 1_000_000  # about 12 hours of audio
NICE = 10  # the render's priority, below the live voice's
# Kokoro's voices by their first letter, and the language each reads (espeak's name for it)
LANGS = {"a": "en-us", "b": "en-gb", "e": "es", "f": "fr-fr", "h": "hi", "i": "it", "p": "pt-br"}
BREW_BINS = ("/opt/homebrew/bin", "/usr/local/bin")  # where Homebrew puts ffmpeg and lame, off launchd's PATH


def lang(voice: str) -> str:
    return LANGS.get(voice[:1], "en-us")


# ── Making text speakable ───────────────────────────────────────

_FENCE = re.compile(r"^\s*(```|~~~).*$", re.MULTILINE)
_HTML = re.compile(r"<[^>\n]{1,200}>")
_IMAGE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
_LINK = re.compile(r"\[([^\]]+)\]\((?:[^()]|\([^)]*\))*\)")
_URL = re.compile(r"\b(?:https?://|www\.)(?:www\.)?([A-Za-z0-9.-]*[A-Za-z0-9])(?:[/?#:][^\s)>\]]*[^\s)>\].,;:!?'\"])?")
_EMPHASIS = re.compile(r"(\*\*|\*|~~|`)(?=\S)(.+?)(?<=\S)\1|(?<!\w)(__|_)(?=\S)(.+?)(?<=\S)\3(?!\w)")
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.*?)\s*#*\s*$")
_ITEM = re.compile(r"^\s*(?:[-*+•▪◦]|\d{1,3}[.)])\s+(.*)$")
_QUOTE = re.compile(r"^\s*>\s?")
_RULE = re.compile(r"^\s*([-*_=])(\s*\1){2,}\s*$")
_TABLE_RULE = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")
_ENDS = ".!?…:;,"


def _closed(line: str) -> str:
    """A line read as a sentence of its own: a heading or list item gets a full stop if it has none."""
    return line if not line or line[-1] in _ENDS + "\"'”’)" else line + "."


def speakable(text: str) -> str:
    """Text as it should be read: markdown's marks, code fences, HTML tags and link targets gone (a bare link is read
    as its site's name), headings, list items and table rows each a sentence of their own, and paragraphs kept apart
    by a blank line. Other line breaks inside a paragraph, as in text from a PDF, are only spaces."""
    text = _FENCE.sub("", text.replace("\r\n", "\n").replace("\r", "\n"))
    text = _HTML.sub(" ", text)
    text = _IMAGE.sub(r"\1", text)
    text = _LINK.sub(r"\1", text)
    text = _URL.sub(lambda m: m[1].removeprefix("www.").rstrip("."), text)
    paragraphs, lines = [], []

    def end_paragraph() -> None:
        if lines:
            paragraphs.append(" ".join(lines))
            lines.clear()

    for raw in text.split("\n"):
        line = _QUOTE.sub("", raw).strip()
        if not line or _RULE.match(line) or _TABLE_RULE.match(line):
            end_paragraph()
            continue
        if heading := _HEADING.match(line):
            end_paragraph()
            paragraphs.append(_closed(_unmarked(heading[1])))
            continue
        if item := _ITEM.match(line):
            line = _closed(_unmarked(item[1]))
        elif line.startswith("|") and line.endswith("|"):
            line = _closed(", ".join(c.strip() for c in line.strip("|").split("|") if c.strip()))
        else:
            line = _unmarked(line)
        lines.append(line)
    end_paragraph()
    return "\n\n".join(p for p in (re.sub(r"\s+", " ", p).strip() for p in paragraphs) if p)


def _unmarked(line: str) -> str:
    for _ in range(3):  # nested: **_both_**
        line = _EMPHASIS.sub(lambda m: m[2] if m[1] else m[4], line)
    return line


# ── Cutting it into parts ───────────────────────────────────────

_SENTENCE = re.compile(r"(?<=[.!?…])[\"'”’)\]]*\s+")
_CLAUSE = re.compile(r"(?<=[,;:—–])\s+|\s+(?=[—–]\s)")


def _pieces(text: str, splitter: re.Pattern) -> list[str]:
    return [p for p in (s.strip() for s in splitter.split(text)) if p]


def _pack(pieces: list[str], limit: int) -> list[str]:
    """Pieces joined into runs of at most `limit` characters, in order."""
    runs: list[str] = []
    for piece in pieces:
        if runs and len(runs[-1]) + 1 + len(piece) <= limit:
            runs[-1] += " " + piece
        else:
            runs.append(piece)
    return runs


def _words(text: str, limit: int) -> list[str]:
    """Text cut between words into runs of at most `limit` characters (a word longer than that is cut too)."""
    words = [w[i:i + limit] for w in text.split() for i in range(0, len(w), limit)]
    return _pack(words, limit)


def parts(text: str, limit: int = PART_CHARS) -> list[tuple[str, bool]]:
    """The text in parts to read one at a time, each with whether it ends a paragraph. A paragraph's sentences are
    joined into parts of up to `limit` characters; a longer sentence is cut at its clauses, and a longer clause
    between words."""
    found = []
    for paragraph in (p.strip() for p in re.split(r"\n\s*\n", text)):
        if not paragraph:
            continue
        pieces = []
        for sentence in _pieces(paragraph, _SENTENCE):
            if len(sentence) <= limit:
                pieces.append(sentence)
                continue
            for clause in _pieces(sentence, _CLAUSE):
                pieces.extend([clause] if len(clause) <= limit else _words(clause, limit))
        runs = _pack(pieces, limit)
        found += [(run, i == len(runs) - 1) for i, run in enumerate(runs)]
    return found


def seconds_of(text: str, speed: float = 1.0) -> float:
    """About how long the text takes to read at `speed`."""
    return len(text.split()) / (WORDS_PER_SECOND * speed)


def length(seconds: float) -> str:
    """A length to say: '40 seconds', '12 minutes', '1 hour 5 minutes'."""
    if seconds < 60:
        n = max(1, round(seconds))
        return f"{n} second{'s' * (n != 1)}"
    minutes = round(seconds / 60)
    hours, minutes = divmod(minutes, 60)
    said = [f"{n} {unit}{'s' * (n != 1)}" for n, unit in ((hours, "hour"), (minutes, "minute")) if n]
    return " ".join(said) or "1 minute"


# ── Files and encoders ──────────────────────────────────────────


def file_name(title: str) -> str:
    """A file name, without its extension, from a title: what a Mac or a phone takes, at most 80 characters."""
    name = re.sub(r"[\x00-\x1f/\\:*?\"<>|]+", " ", title)
    name = re.sub(r"\s+", " ", name).strip(" .")[:80].strip(" .")
    return name or "JARVIS audio"


def free_path(folder: Path, name: str, ext: str) -> Path:
    """`folder`/`name`.`ext`, or with ' 2', ' 3', ... if that's taken."""
    path, n = folder / f"{name}.{ext}", 1
    while path.exists():
        n += 1
        path = folder / f"{name} {n}.{ext}"
    return path


def find_program(name: str) -> str | None:
    """A program on the PATH, or where Homebrew puts it (a service's PATH often leaves that out)."""
    return shutil.which(name) or next((str(p) for d in BREW_BINS if os.access(p := Path(d) / name, os.X_OK)), None)


_FFMPEG = ["-nostdin", "-y", "-loglevel", "error", "-i"]
# Each format's encoders, best first: the program, and its arguments before the WAV and between it and the file made.
# afconvert comes with every Mac; ffmpeg and lame from Homebrew. A WAV needs none.
ENCODERS = {
    "m4a": [("afconvert", ["-f", "m4af", "-d", "aac", "-b", "64000"], []),
            ("ffmpeg", _FFMPEG, ["-c:a", "aac", "-b:a", "64k", "-f", "ipod"])],
    "mp3": [("ffmpeg", _FFMPEG, ["-c:a", "libmp3lame", "-b:a", "64k", "-f", "mp3"]),
            ("lame", ["--quiet", "-b", "64"], [])],
}


def programs(fmt: str) -> str:
    """The programs that make `fmt`, to say: 'ffmpeg or lame'."""
    return " or ".join(program for program, _, _ in ENCODERS.get(fmt, []))


def _argv(program: str, before: list[str], after: list[str], wav: Path, out: Path) -> list[str]:
    return [program, *before, str(wav), *after, str(out)]


def encoders(fmt: str) -> list[Callable[[Path, Path], list[str]]]:
    """The commands on this Mac that can make a file of `fmt` from a WAV, best first, each as argv for (wav, out)."""
    return [functools.partial(_argv, path, before, after) for program, before, after in ENCODERS.get(fmt, [])
            if (path := find_program(program))]


def can_make(fmt: str) -> bool:
    return fmt == "wav" or bool(encoders(fmt))


def encode(wav: Path, out: Path, fmt: str) -> tuple[Path, str]:
    """Make `out` from `wav`, which goes. Returns the file made and "", or, when no encoder could make it, a WAV
    beside where `out` was meant to be, and why."""
    problems = []
    if fmt != "wav":
        tmp = out.with_name(f".{out.stem}.part{out.suffix}")
        for command in encoders(fmt):
            argv = command(wav, tmp)
            done = subprocess.run(argv, capture_output=True, text=True, stdin=subprocess.DEVNULL, check=False)
            if done.returncode == 0 and tmp.is_file() and tmp.stat().st_size:
                os.replace(tmp, out)
                wav.unlink(missing_ok=True)
                return out, ""
            said = (done.stderr or done.stdout).strip().splitlines()
            problems.append(f"{Path(argv[0]).name}: {said[0][:200] if said else f'exit code {done.returncode}'}")
            tmp.unlink(missing_ok=True)
        out = free_path(out.parent, out.stem, "wav")
        problems = problems or [f"no {fmt} encoder ({programs(fmt)}) on this Mac"]
    shutil.move(wav, out)
    return out, "; ".join(problems)


# ── Rendering ───────────────────────────────────────────────────

Speak = Callable[[str, str, float], np.ndarray]  # (text, voice, speed): float32 samples at RATE


def kokoro(model: Path | None = None, threads: int = AUDIO_THREADS) -> tuple[Speak, list[str]]:
    """Kokoro of this process's own, and its voices. Threads 0: a quarter of the cores (at least 2), leaving the rest
    to the live voice."""
    import onnxruntime as ort
    from kokoro_onnx import Kokoro
    from kokoro_onnx.session import resolve_providers

    model = model or next((m for m in (AUDIO_MODEL, TTS_MODEL, *TTS_MODEL_FALLBACKS) if m.exists()), AUDIO_MODEL)
    options = ort.SessionOptions()
    options.enable_cpu_mem_arena = TTS_MEMORY_ARENA
    options.intra_op_num_threads = threads or max(2, (os.cpu_count() or 8) // 4)
    engine = Kokoro.from_session(ort.InferenceSession(str(model), options, providers=resolve_providers()),
                                 str(ROOT / "models/voices-v1.0.bin"))
    print(f"Reading with Kokoro ({model.name}) on {options.intra_op_num_threads} threads.", flush=True)

    def speak(text: str, voice: str, speed: float) -> np.ndarray:
        samples, _ = engine.create(text, voice=voice, speed=speed, lang=lang(voice))
        return samples

    return speak, list(engine.get_voices())


def _silence(seconds: float) -> bytes:
    return bytes(2 * int(RATE * seconds))


def render(text: str, wav: Path, speak: Speak, voice: str, speed: float,
           progress: Callable[[str], None] = print) -> float:
    """Read `text` part by part into `wav`; returns how many seconds of audio it holds."""
    found = parts(text)
    if not found:
        raise ValueError("There's nothing to read.")
    wav.parent.mkdir(parents=True, exist_ok=True)
    frames, started = 0, time.monotonic()
    with wave.open(str(wav), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(RATE)
        for i, (part, ends_paragraph) in enumerate(found, 1):
            samples = speak(part, voice, speed)
            pcm = (np.clip(samples, -1, 1) * 32767).astype("<i2").tobytes()
            if i < len(found):
                pcm += _silence(PARAGRAPH_PAUSE if ends_paragraph else SENTENCE_PAUSE)
            out.writeframes(pcm)
            frames += len(pcm) // 2
            progress(f"Read part {i} of {len(found)}: {length(frames / RATE)} of audio so far, "
                     f"in {length(time.monotonic() - started)}.")
    return frames / RATE


def shown(path: Path) -> str:
    """A path to say, with ~ for the home folder."""
    return f"~/{path.relative_to(Path.home())}" if path.is_relative_to(Path.home()) and path != Path.home() else str(path)


def _writable(folder: Path) -> bool:
    """Whether a file can be made in `folder` (macOS may keep JARVIS out of Downloads, Desktop and Documents)."""
    check = folder / f".jarvis-{os.getpid()}.check"
    try:
        folder.mkdir(parents=True, exist_ok=True)
        check.touch()
        check.unlink()
    except OSError:
        return False
    return True


def _wait_turn(lock) -> None:
    """One file at a time: two renders would each hold a Kokoro, on a Mac that may have 8 GB."""
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("Waiting for the audio file before this one to finish.", flush=True)
        fcntl.flock(lock, fcntl.LOCK_EX)


async def send_to_phone(path: Path, title: str, seconds: float) -> str:
    """Share the file with the phone and push a notification that plays it; returns what to add to the last line."""
    from jarvis.core.config import PUSH_ENABLED
    from jarvis.notify import push
    from jarvis.state import shared_files

    if not PUSH_ENABLED:
        return " Not sent to the phone: notifications are off on the Mac (JARVIS_PUSH)."
    token = shared_files.share(path)
    try:
        sent = await push.send_push("Audio ready", f"{title}: {length(seconds)}. Tap to listen.",
                                    shared_files.link(token, path.name), f"file-{token[:12]}")
    except Exception as e:
        return f" Couldn't send it to the phone: {type(e).__name__}."
    return (" Sent to the phone." if sent else
            " Not sent to the phone: no phone has JARVIS's notifications on.")


def make(spec: dict, speak: Speak | None = None, voices: list[str] | None = None) -> str:
    """Render the file SPEC asks for; returns the last line to say. `speak` and `voices` stand in for Kokoro in tests."""
    text, voice, speed, fmt = spec["text"], spec["voice"], float(spec["speed"]), spec["format"]
    out = Path(spec["out"])
    if speak is None:
        speak, voices = kokoro()
    if voices and voice not in voices:
        alike = sorted(v for v in voices if v[:1] == voice[:1]) or sorted(voices)
        raise ValueError(f"Kokoro has no voice {voice}. It has {', '.join(alike[:8])}"
                         + (", and others." if len(voices) > len(alike[:8]) else "."))
    wav = work_dir() / f"{out.stem}.{os.getpid()}.wav"
    try:
        seconds = render(text, wav, speak, voice, speed, functools.partial(print, flush=True))
        notes = ""
        out = free_path(out.parent, out.stem, fmt)  # in case one was made since it started
        if not _writable(out.parent):  # macOS keeps JARVIS out of that folder, say
            notes += f" JARVIS couldn't write to {shown(out.parent)}, so it's in its own folder instead."
            out = free_path(work_dir(), out.stem, fmt)
        out, why = encode(wav, out, fmt)
        if why:
            notes += f" It's a WAV, as no {fmt} could be made: {why}."
    finally:
        wav.unlink(missing_ok=True)
    said = f"Saved as {out.name} in {shown(out.parent)}, {length(seconds)} of audio.{notes}"
    if spec.get("phone"):
        said += asyncio.run(send_to_phone(out, spec.get("title") or out.stem, seconds))
    return said


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("usage: python -m jarvis.voice.audio_file SPEC.json", file=sys.stderr)
        return 2
    spec_file = Path(argv[0])
    # stop_job's SIGTERM unwinds, so the half-made WAV and the spec, with the user's words, don't stay behind
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(128 + signal.SIGTERM))
    try:
        spec = json.loads(spec_file.read_text())
        try:
            os.nice(NICE)
        except OSError:  # a sandbox may not allow it; it still runs, at the usual priority
            pass
        work_dir().mkdir(parents=True, exist_ok=True)
        with open(work_dir() / "render.lock", "w") as lock:
            _wait_turn(lock)
            said = make(spec)
    except Exception as e:
        print(f"It couldn't be made: {e}" if isinstance(e, ValueError) else
              f"It couldn't be made: {type(e).__name__}: {e}", flush=True)
        return 1
    finally:
        spec_file.unlink(missing_ok=True)
    print(said, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
