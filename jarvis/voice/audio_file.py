"""Render text to an audio file in Kokoro's voice; run as a long job by the audio_file tool.

  python -m jarvis.voice.audio_file SPEC.json

SPEC holds already-speakable text (jarvis.voice.speakable) and the output path. Text is split into `parts`
(<= PART_CHARS, at paragraph, sentence, then clause bounds), rendered with pauses into a WAV, then encoded (m4a:
afconvert on macOS, ffmpeg; mp3: ffmpeg/lame).
With no encoder the WAV is kept and the last line says so.

Isolated from live voice: own Kokoro (AUDIO_MODEL; fp32 is faster than int8 on Apple silicon), niced, AUDIO_THREADS
cores, one file at a time via a lock. Prints one progress line per part (shown by job_status); the last line is the
result. Optionally shares the file to the phone and pushes a notification that plays it.
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

from jarvis.core import platform, profile
from jarvis.core.config import (
    AUDIO_MODEL,
    AUDIO_THREADS,
    ROOT,
    TTS_MEMORY_ARENA,
    TTS_MODEL,
    TTS_MODEL_FALLBACKS,
)

RATE = 24000  # Kokoro's sample rate


def work_dir() -> Path:
    """Profile folder for specs, in-progress WAVs, the lock, and files that couldn't go where asked."""
    return profile.current().audio

PART_CHARS = 400  # per render: well under Kokoro's 510-phoneme limit
SENTENCE_PAUSE, PARAGRAPH_PAUSE = 0.25, 0.7  # seconds
WORDS_PER_SECOND = 2.5  # Kokoro at speed 1 incl. pauses, for estimates
MAX_CHARS = 1_000_000  # ~12 hours of audio
NICE = 10  # below the live voice
# Kokoro voice prefix -> espeak language
LANGS = {"a": "en-us", "b": "en-gb", "e": "es", "f": "fr-fr", "h": "hi", "i": "it", "p": "pt-br"}
BREW_BINS = ("/opt/homebrew/bin", "/usr/local/bin")  # Homebrew, missing from launchd's PATH
MAC_ONLY = {"afconvert"}  # macOS's own encoder


def lang(voice: str) -> str:
    return LANGS.get(voice[:1], "en-us")


# ── Parts ──────────────────────────────────────────────────────

_SENTENCE = re.compile(r"(?<=[.!?…])[\"'”’)\]]*\s+")
_CLAUSE = re.compile(r"(?<=[,;:—–])\s+|\s+(?=[—–]\s)")


def _pieces(text: str, splitter: re.Pattern) -> list[str]:
    return [p for p in (s.strip() for s in splitter.split(text)) if p]


def _pack(pieces: list[str], limit: int) -> list[str]:
    """Greedily join pieces into runs of <= `limit` chars."""
    runs: list[str] = []
    for piece in pieces:
        if runs and len(runs[-1]) + 1 + len(piece) <= limit:
            runs[-1] += " " + piece
        else:
            runs.append(piece)
    return runs


def _words(text: str, limit: int) -> list[str]:
    """Split at word bounds into runs of <= `limit` chars (overlong words are cut)."""
    words = [w[i:i + limit] for w in text.split() for i in range(0, len(w), limit)]
    return _pack(words, limit)


def parts(text: str, limit: int = PART_CHARS) -> list[tuple[str, bool]]:
    """(part, ends_paragraph) list. Sentences are packed up to `limit`; longer ones split at clauses, then words."""
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
    """Estimated reading time in seconds."""
    return len(text.split()) / (WORDS_PER_SECOND * speed)


def length(seconds: float) -> str:
    """Spoken duration: '40 seconds', '12 minutes', '1 hour 5 minutes'."""
    if seconds < 60:
        n = max(1, round(seconds))
        return f"{n} second{'s' * (n != 1)}"
    minutes = round(seconds / 60)
    hours, minutes = divmod(minutes, 60)
    said = [f"{n} {unit}{'s' * (n != 1)}" for n, unit in ((hours, "hour"), (minutes, "minute")) if n]
    return " ".join(said) or "1 minute"


# ── Files and encoders ──────────────────────────────────────────


def file_name(title: str) -> str:
    """Safe file stem from a title, <= 80 chars."""
    name = re.sub(r"[\x00-\x1f/\\:*?\"<>|]+", " ", title)
    name = re.sub(r"\s+", " ", name).strip(" .")[:80].strip(" .")
    return name or "JARVIS audio"


def free_path(folder: Path, name: str, ext: str) -> Path:
    """`folder`/`name`.`ext`, suffixed ' 2', ' 3', ... if taken."""
    path, n = folder / f"{name}.{ext}", 1
    while path.exists():
        n += 1
        path = folder / f"{name} {n}.{ext}"
    return path


def find_program(name: str) -> str | None:
    """Find a program on PATH or, on macOS, in Homebrew's bin dirs."""
    if not platform.MAC:
        return shutil.which(name)
    return shutil.which(name) or next((str(p) for d in BREW_BINS if os.access(p := Path(d) / name, os.X_OK)), None)


_FFMPEG = ["-nostdin", "-y", "-loglevel", "error", "-i"]
# Encoders per format, best first: (program, args before WAV, args between WAV and output)
ENCODERS = {
    "m4a": [("afconvert", ["-f", "m4af", "-d", "aac", "-b", "64000"], []),
            ("ffmpeg", _FFMPEG, ["-c:a", "aac", "-b:a", "64k", "-f", "ipod"])],
    "mp3": [("ffmpeg", _FFMPEG, ["-c:a", "libmp3lame", "-b:a", "64k", "-f", "mp3"]),
            ("lame", ["--quiet", "-b", "64"], [])],
}


def _offered(fmt: str) -> list[tuple[str, list[str], list[str]]]:
    """ENCODERS for `fmt` that can exist on this OS."""
    return [e for e in ENCODERS.get(fmt, []) if platform.MAC or e[0] not in MAC_ONLY]


def programs(fmt: str) -> str:
    """Encoder names for `fmt`, e.g. 'ffmpeg or lame'."""
    return " or ".join(program for program, _, _ in _offered(fmt))


def missing(fmt: str) -> str:
    """Why `fmt` can't be made here, naming what to install."""
    if platform.MAC:
        return f"no {fmt} encoder ({programs(fmt)}) on this computer"
    return f"no {fmt} encoder on this computer: install {programs(fmt)}"


def _argv(program: str, before: list[str], after: list[str], wav: Path, out: Path) -> list[str]:
    return [program, *before, str(wav), *after, str(out)]


def encoders(fmt: str) -> list[Callable[[Path, Path], list[str]]]:
    """Installed encoders for `fmt`, best first, each a (wav, out) -> argv builder."""
    return [functools.partial(_argv, path, before, after) for program, before, after in _offered(fmt)
            if (path := find_program(program))]


def can_make(fmt: str) -> bool:
    return fmt == "wav" or bool(encoders(fmt))


def encode(wav: Path, out: Path, fmt: str) -> tuple[Path, str]:
    """Encode `wav` (consumed) to `out`. Returns (out, ""), or (fallback WAV, reason) if no encoder worked."""
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
        problems = problems or [missing(fmt)]
    shutil.move(wav, out)
    return out, "; ".join(problems)


# ── Rendering ───────────────────────────────────────────────────

Speak = Callable[[str, str, float], np.ndarray]  # (text, voice, speed) -> float32 samples at RATE


def kokoro(model: Path | None = None, threads: int = AUDIO_THREADS) -> tuple[Speak, list[str]]:
    """Load a process-local Kokoro; returns (speak, voices). threads=0 means a quarter of the cores (min 2)."""
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
    """Render `text` into `wav`; returns audio seconds."""
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
    """Path for display, with ~ for home."""
    return f"~/{path.relative_to(Path.home())}" if path.is_relative_to(Path.home()) and path != Path.home() else str(path)


def _writable(folder: Path) -> bool:
    """True if `folder` is writable (macOS privacy may block Downloads, Desktop, Documents)."""
    check = folder / f".jarvis-{os.getpid()}.check"
    try:
        folder.mkdir(parents=True, exist_ok=True)
        check.touch()
        check.unlink()
    except OSError:
        return False
    return True


def _wait_turn(lock) -> None:
    """Serialize renders: two Kokoro instances can exhaust an 8 GB machine."""
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("Waiting for the audio file before this one to finish.", flush=True)
        fcntl.flock(lock, fcntl.LOCK_EX)


async def send_to_phone(path: Path, title: str, seconds: float) -> str:
    """Share the file and push a play notification; returns text to append to the result line."""
    from jarvis.core.config import PUSH_ENABLED
    from jarvis.notify import push
    from jarvis.state import shared_files

    if not PUSH_ENABLED:
        return " Not sent to the phone: notifications are off on the computer (JARVIS_PUSH)."
    token = shared_files.share(path)
    try:
        sent = await push.send_push("Audio ready", f"{title}: {length(seconds)}. Tap to listen.",
                                    shared_files.link(token, path.name), f"file-{token[:12]}")
    except Exception as e:
        return f" Couldn't send it to the phone: {type(e).__name__}."
    return (" Sent to the phone." if sent else
            " Not sent to the phone: no phone has JARVIS's notifications on.")


def make(spec: dict, speak: Speak | None = None, voices: list[str] | None = None) -> str:
    """Render SPEC; returns the result line. `speak`/`voices` replace Kokoro in tests."""
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
        out = free_path(out.parent, out.stem, fmt)  # may have been taken meanwhile
        if not _writable(out.parent):  # e.g. blocked by macOS privacy
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
    # Unwind on SIGTERM so the partial WAV and the spec (user text) are deleted
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(128 + signal.SIGTERM))
    try:
        spec = json.loads(spec_file.read_text())
        try:
            os.nice(NICE)
        except OSError:  # sandbox may forbid it; run at normal priority
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
