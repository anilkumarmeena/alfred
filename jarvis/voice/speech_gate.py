"""Speech gate: drop clips with no real speech before Whisper's invented text (e.g. "Thank you.") reaches the brain.

Mostly audio checks; words count only when they match a known phantom, so a quiet real "yes"/"okay" survives.
  no speech    Silero finds < MIN_SPEECH_SECONDS        whisper      no_speech >= NO_SPEECH and VAD < WEAK_SPEECH
  too short    clip < MIN_SECONDS                        unsure       logprob < MIN_LOGPROB and VAD < UNSURE_SPEECH
  too quiet    loudest 100 ms < MIN_DBFS                 no words     no letters or digits
  faint        VAD, loudness and length all low         repetitive   compression ratio > MAX_COMPRESSION
  phantom      transcript in PHANTOMS and quiet, short or unsure    prompt echo  only vocabulary words (prompt echo)
large-v3-turbo reports no_speech 0.0 on nearly everything, so "whisper" rarely fires.

JARVIS_SPEECH_GATE: shadow (log only), enforce (default, drop), off. Every clip goes to logs/speech-gate.jsonl;
`python -m jarvis.voice.speech_gate [HOURS]` summarises it. SAVE_AUDIO keeps WAVs of every clip, SAVE_SHORT only short
kept ones (to catch phantoms that slip through); newest AUDIO_KEEP files are kept.
"""

import dataclasses
import json
import logging
import re
import sys
import time
import wave
import zlib
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from types import EllipsisType

import numpy as np

from jarvis.config import (
    SPEECH_GATE_AUDIO_DIR,
    SPEECH_GATE_AUDIO_KEEP,
    SPEECH_GATE_FAINT_DBFS,
    SPEECH_GATE_FAINT_MAX_SECONDS,
    SPEECH_GATE_FAINT_SPEECH_SECONDS,
    SPEECH_GATE_LOG,
    SPEECH_GATE_MAX_COMPRESSION,
    SPEECH_GATE_MIN_DBFS,
    SPEECH_GATE_MIN_LOGPROB,
    SPEECH_GATE_MIN_SECONDS,
    SPEECH_GATE_MIN_SPEECH_SECONDS,
    SPEECH_GATE_NO_SPEECH,
    SPEECH_GATE_PHANTOM_DBFS,
    SPEECH_GATE_PHANTOM_SPEECH_SECONDS,
    SPEECH_GATE_PHANTOMS,
    SPEECH_GATE_SAVE_SHORT,
    SPEECH_GATE_SHORT_SECONDS,
    SPEECH_GATE_SHORT_SPEECH_SECONDS,
    SPEECH_GATE_UNSURE_SPEECH_SECONDS,
    SPEECH_GATE_WEAK_SPEECH_SECONDS,
)
from jarvis.state import preferences

log = logging.getLogger("jarvis.voice.speech_gate")

RATE = 16000  # Whisper and Silero input rate
WINDOW = 512  # Silero window at 16 kHz (32 ms)
SPEECH_PROB = 0.5  # Silero's activation threshold
LOUDNESS_WINDOW = RATE // 10  # 100 ms

# Whisper's output on silence/noise: training-video sign-offs and fillers. Matched against the normalised whole
# transcript. Never add "yes", "no", "okay", "sure" or "go ahead": real replies must not need a louder voice.
PHANTOMS = (
    "thank you", "thank you very much", "thank you so much", "thanks", "thanks a lot", "thank you for watching",
    "thanks for watching", "thank you for listening", "thanks for listening", "please subscribe", "subscribe",
    "like and subscribe", "you", "bye", "bye bye", "goodbye", "so", "like", "oh", "um", "uh", "hmm", "mm", "mm hmm",
    "uh huh", "please", "and", "but", "the", "i", "music", "applause", "silence",
)


def normalise(text: str) -> str:
    """Lowercase words only: "Mm-hmm." -> "mm hmm"."""
    return " ".join(re.findall(r"[^\W_]+(?:'[^\W_]+)*", text.lower()))


def compression_ratio(text: str) -> float:
    """UTF-8 length / zlib length (Whisper's loop detector)."""
    data = text.encode()
    return round(len(data) / len(zlib.compress(data)), 2) if data else 0.0


@dataclasses.dataclass(frozen=True)
class Thresholds:
    min_seconds: float = SPEECH_GATE_MIN_SECONDS
    min_speech_seconds: float = SPEECH_GATE_MIN_SPEECH_SECONDS
    min_dbfs: float = SPEECH_GATE_MIN_DBFS
    no_speech: float = SPEECH_GATE_NO_SPEECH
    weak_speech_seconds: float = SPEECH_GATE_WEAK_SPEECH_SECONDS
    faint_speech_seconds: float = SPEECH_GATE_FAINT_SPEECH_SECONDS
    faint_dbfs: float = SPEECH_GATE_FAINT_DBFS
    faint_max_seconds: float = SPEECH_GATE_FAINT_MAX_SECONDS
    phantom_dbfs: float = SPEECH_GATE_PHANTOM_DBFS
    phantom_speech_seconds: float = SPEECH_GATE_PHANTOM_SPEECH_SECONDS
    phantoms: frozenset[str] = frozenset(normalise(p) for p in SPEECH_GATE_PHANTOMS or PHANTOMS)
    min_logprob: float = SPEECH_GATE_MIN_LOGPROB
    unsure_speech_seconds: float = SPEECH_GATE_UNSURE_SPEECH_SECONDS
    max_compression: float = SPEECH_GATE_MAX_COMPRESSION


@dataclasses.dataclass
class Measure:
    """Gate measurements for one clip."""
    seconds: float
    rms_dbfs: float  # whole clip, VAD padding included
    loudest_dbfs: float  # loudest 100 ms
    speech_seconds: float  # Silero speech seconds
    speech_ratio: float
    no_speech: float | None = None  # Whisper's, if given
    avg_logprob: float | None = None  # Whisper's, if given
    compression_ratio: float | None = None  # of the transcript


def _dbfs(x: np.ndarray) -> float:
    return float(20 * np.log10(max(float(np.sqrt(np.mean(np.square(x, dtype=np.float64)))), 1e-10)))


def measure(audio: np.ndarray, speech_probs: Callable[[np.ndarray], np.ndarray]) -> Measure:
    """Measure 16 kHz float audio; `speech_probs` returns per-32 ms-window VAD probabilities."""
    seconds = len(audio) / RATE
    if not len(audio):
        return Measure(0.0, -200.0, -200.0, 0.0, 0.0)
    n = len(audio) - len(audio) % LOUDNESS_WINDOW
    windows = audio[:n].reshape(-1, LOUDNESS_WINDOW) if n else audio[None, :]
    loudest = max(_dbfs(w) for w in windows)
    probs = np.asarray(speech_probs(audio))
    speech = float(np.count_nonzero(probs >= SPEECH_PROB)) * WINDOW / RATE
    return Measure(round(seconds, 3), round(_dbfs(audio), 1), round(loudest, 1), round(min(speech, seconds), 3),
                   round(min(speech / seconds, 1.0), 3))


def reasons(m: Measure, t: Thresholds, text: str | None = None, echo: bool = False) -> list[str]:
    """Reasons to drop the clip; empty to keep. Without `text`, audio only. `echo`: transcript is a prompt echo."""
    why = []
    unsure = m.avg_logprob is not None and m.avg_logprob < t.min_logprob
    if m.speech_seconds < t.min_speech_seconds:
        why.append("no speech")
    if m.seconds < t.min_seconds:
        why.append("too short")
    if m.loudest_dbfs < t.min_dbfs:
        why.append("too quiet")
    if m.no_speech is not None and m.no_speech >= t.no_speech and m.speech_seconds < t.weak_speech_seconds:
        why.append("whisper hears no speech")
    if (m.speech_seconds < t.faint_speech_seconds and m.loudest_dbfs < t.faint_dbfs
            and m.seconds < t.faint_max_seconds):
        why.append("faint")
    if unsure and m.speech_seconds < t.unsure_speech_seconds:
        why.append("whisper unsure")
    if text is None:
        return why
    words = normalise(text)
    if not words:
        why.append("no words")
    elif words in t.phantoms and (m.loudest_dbfs < t.phantom_dbfs or m.speech_seconds < t.phantom_speech_seconds
                                  or unsure):
        why.append("phantom phrase")
    if compression_ratio(text) > t.max_compression:
        why.append("repetitive")
    if echo:
        why.append("prompt echo")
    return why


class SileroProbs:
    """Silero over a whole clip from fresh state, on its own lazily-loaded onnxruntime session."""

    def __init__(self) -> None:
        self._model = None

    def __call__(self, audio: np.ndarray) -> np.ndarray:
        from livekit.plugins.silero import onnx_model

        if self._model is None:
            self._model = onnx_model.OnnxModel(onnx_session=onnx_model.new_inference_session(True), sample_rate=RATE)
        self._model.reset()
        n = len(audio) - len(audio) % WINDOW
        return np.array([self._model(audio[i:i + WINDOW]) for i in range(0, n, WINDOW)], np.float32)


def save_clip(audio: np.ndarray, folder: Path, at: float, keep: int) -> Path:
    """Write a 16-bit WAV named by local time `at`, then prune `folder` to the newest `keep` WAVs."""
    folder.mkdir(parents=True, exist_ok=True)
    stem = time.strftime("%Y%m%d-%H%M%S", time.localtime(at)) + f"-{int(at * 1000) % 1000:03d}"
    path, n = folder / f"{stem}.wav", 1
    while path.exists():  # same millisecond ("_" sorts after ".", keeping order)
        path, n = folder / f"{stem}_{n}.wav", n + 1
    with wave.open(str(path), "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(RATE)
        f.writeframes((np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes())
    for old in sorted(folder.glob("*.wav"))[:-keep]:
        old.unlink(missing_ok=True)
    return path


class SpeechGate:
    def __init__(self, mode: str | None = None, thresholds: Thresholds | None = None,
                 speech_probs: Callable[[np.ndarray], np.ndarray] | None = None,
                 record: Path | None = SPEECH_GATE_LOG,
                 audio_dir: Path | None | EllipsisType = ...,
                 keep_audio: int = SPEECH_GATE_AUDIO_KEEP, save_all: bool | None = None,
                 short: tuple[float, float] = (SPEECH_GATE_SHORT_SECONDS, SPEECH_GATE_SHORT_SPEECH_SECONDS)) -> None:
        # Defaults: the speech-gate preference, and saving per its mode (audio_dir ... = the usual folder if saving)
        self.mode = mode or preferences.speech_gate()
        if save_all is None:
            save_all = preferences.speech_gate_save_audio()
        if audio_dir is ...:
            saving = preferences.speech_gate_save_audio() or SPEECH_GATE_SAVE_SHORT
            audio_dir = SPEECH_GATE_AUDIO_DIR if saving else None
        self.thresholds = thresholds or Thresholds()
        self._probs = speech_probs or SileroProbs()
        self._record = record
        self._audio_dir, self._keep_audio = audio_dir, keep_audio  # None: never save
        # save_all: every clip; else kept clips under short[0] s long or short[1] s of speech
        self._save_all, self._short = save_all, short

    def use(self, mode: str, save_all: bool) -> None:
        """Apply per-conversation mode and save_all."""
        self.mode, self._save_all = mode, save_all
        if save_all and self._audio_dir is None:
            self._audio_dir = SPEECH_GATE_AUDIO_DIR

    @property
    def on(self) -> bool:
        return self.mode != "off"

    def describe(self) -> str:
        """Start-up log line."""
        if not self.on:
            return "speech gate: off"
        t = self.thresholds
        which = "each clip" if self._save_all else (f"the short clips it lets through (under {self._short[0]:g} s, or "
                                                     f"under {self._short[1]:g} s of speech)")
        saving = f"; saving {which} to {self._audio_dir}, the newest {self._keep_audio}" if self._audio_dir else ""
        return (f"speech gate: {self.mode} mode ({'logs, drops nothing' if self.mode == 'shadow' else 'drops'} clips "
                f"with under {t.min_speech_seconds:g} s of speech, under {t.min_seconds:g} s long, quieter than "
                f"{t.min_dbfs:g} dBFS, no_speech >= {t.no_speech:g} with under {t.weak_speech_seconds:g} s of "
                f"speech, or under {t.faint_speech_seconds:g} s of speech, quieter than {t.faint_dbfs:g} dBFS and "
                f"under {t.faint_max_seconds:g} s long, or logprob under {t.min_logprob:g} with under "
                f"{t.unsure_speech_seconds:g} s of speech; and {len(t.phantoms)} phantom phrases such as \"thank you\" "
                f"quieter than {t.phantom_dbfs:g} dBFS, with under {t.phantom_speech_seconds:g} s of speech or that "
                f"Whisper is unsure of, transcripts without words, or compressing over {t.max_compression:g}{saving})")

    def measure(self, audio: np.ndarray) -> Measure | None:
        """Measure a clip, or None if off or failed. Runs in the transcription thread."""
        if not self.on:
            return None
        try:
            return measure(audio, self._probs)
        except Exception:
            log.exception("speech gate couldn't measure a clip; it goes through")
            return None

    def passes(self, m: Measure | None, text: str, no_speech: float | None, audio: np.ndarray | None = None, *,
               avg_logprob: float | None = None, echo: bool = False) -> bool:
        """True if the transcript may reach the brain. Logs every clip and saves `audio` when configured."""
        if m is None or not text:
            return True
        m.no_speech = None if no_speech is None else round(no_speech, 3)
        m.avg_logprob = None if avg_logprob is None else round(avg_logprob, 3)
        m.compression_ratio = compression_ratio(text)
        why = reasons(m, self.thresholds, text, echo)
        verdict = "kept" if not why else "dropped" if self.mode == "enforce" else "would drop"
        now = time.time()
        saved = self._save(audio, now) if self._saves(m, verdict) else None
        if why:
            log.info("speech gate %s%s a %.2f s clip (%s): rms %.1f dBFS, loudest %.1f dBFS, speech %.2f s (%.0f%%), "
                     "no_speech %s, logprob %s: %r%s", verdict, " (shadow)" if verdict == "would drop" else "",
                     m.seconds, ", ".join(why), m.rms_dbfs, m.loudest_dbfs, m.speech_seconds, m.speech_ratio * 100,
                     "n/a" if m.no_speech is None else f"{m.no_speech:.2f}",
                     "n/a" if m.avg_logprob is None else f"{m.avg_logprob:.2f}", text, f" ({saved})" if saved else "")
        self._write({"time": round(now, 1), "verdict": verdict, "reasons": why, "text": text,
                     **dataclasses.asdict(m), "audio": saved and str(saved)})
        return verdict != "dropped"

    def _saves(self, m: Measure, verdict: str) -> bool:
        """Save every clip with save_all, else only short kept ones."""
        return self._save_all or (verdict != "dropped" and (m.seconds < self._short[0]
                                                             or m.speech_seconds < self._short[1]))

    def _save(self, audio: np.ndarray | None, at: float) -> Path | None:
        if self._audio_dir is None or audio is None:
            return None
        try:
            return save_clip(audio, self._audio_dir, at, self._keep_audio)
        except OSError as e:
            log.warning("speech gate couldn't save the clip in %s: %s", self._audio_dir, e)
            return None

    def _write(self, record: dict) -> None:
        if self._record is None:
            return
        try:
            self._record.parent.mkdir(parents=True, exist_ok=True)
            with open(self._record, "a") as f:
                f.write(json.dumps(record) + "\n")
        except OSError as e:
            log.warning("speech gate couldn't write %s: %s", self._record, e)


def summary(path: Path = SPEECH_GATE_LOG, since: float = 0.0) -> str:
    """Counts since `since`, then each flagged clip."""
    try:
        lines = path.read_text().splitlines()
    except FileNotFoundError:
        return f"No clips yet: {path} doesn't exist."
    records = []
    for line in lines:
        try:
            records.append(json.loads(line))
        except ValueError:  # truncated by a crash
            continue
    records = [r for r in records if r.get("time", 0) >= since]
    flagged = [r for r in records if r["verdict"] != "kept"]
    lines = [f"{len(records)} clips, {len(flagged)} the gate would drop or dropped."]
    if counts := Counter(why for r in flagged for why in r["reasons"]):
        lines.append("By reason: " + ", ".join(f"{why} {n}" for why, n in counts.most_common()) + ".")
    for r in flagged:
        lines.append(f"{time.strftime('%m-%d %H:%M:%S', time.localtime(r['time']))}  {r['verdict']:10}  "
                     f"{r['seconds']:5.2f} s  loudest {r['loudest_dbfs']:6.1f} dBFS  speech {r['speech_seconds']:.2f} s "
                     f"({r['speech_ratio'] * 100:3.0f}%)  no_speech {r['no_speech']}  logprob {r.get('avg_logprob')}  "
                     f"[{', '.join(r['reasons'])}]  "
                     f"{r['text']!r}" + (f"  {r['audio']}" if r.get("audio") else ""))
    return "\n".join(lines)


if __name__ == "__main__":  # [HOURS], default 24
    print(summary(since=time.time() - float(sys.argv[1] if sys.argv[1:] else 24) * 3600))
