"""The speech gate: drops clips with no real speech in them before their transcript reaches the brain.

Whisper makes up text for near-silent or noise-only clips, most often a lone "Thank you." The gate judges the audio
Whisper heard, and the words only count against a clip when they are one of Whisper's known phantoms, so a real "yes",
"no", "okay" or "go ahead" is held to the audio checks alone:
  no speech     Silero VAD, run again over the clip, found less than MIN_SPEECH_SECONDS of speech in it
  too short     the clip is shorter than MIN_SECONDS
  too quiet     its loudest 100 ms is below MIN_DBFS
  whisper       Whisper's own no-speech probability is at least NO_SPEECH and the VAD found under WEAK_SPEECH_SECONDS
                of speech: Whisper's signal only counts alongside weak VAD evidence, never on its own. (Whisper
                large-v3-turbo, 16- or 4-bit, gives 0.0 on every clip, phantoms included, so this one rarely fires.)
  faint         all three at once: the VAD found under FAINT_SPEECH_SECONDS of speech, the loudest 100 ms is below
                FAINT_DBFS, and the clip is under FAINT_MAX_SECONDS long. Real one-word replies are louder or longer.
  phantom       the whole transcript is one of PHANTOMS ("Thank you.", "You", "Bye."), and the clip is quieter than
                PHANTOM_DBFS, or has under PHANTOM_SPEECH_SECONDS of speech, or Whisper is unsure of it (below)
  unsure        Whisper's average log-probability is under MIN_LOGPROB, so every retry it made failed too and the
                text is a guess, and the VAD found under UNSURE_SPEECH_SECONDS of speech
  no words      the transcript has no letters or digits in it (",", "...", "♪")
  repetitive    the transcript's compression ratio is over MAX_COMPRESSION: a phrase looping
  prompt echo   the transcript is nothing but several of the speech vocabulary's words, which Whisper was prompted
                with: its prompt echoed back (jarvis.state.vocabulary)
JARVIS_SPEECH_GATE picks what happens to such a clip:
  shadow   log it and keep it, so a day of real calls can be reviewed before new thresholds drop anything
  enforce  (default) log it and drop it: the brain never hears the transcript
  off      no checks
Every clip, kept or not, goes in logs/speech-gate.jsonl with its measurements; clips the gate would drop are also
logged in logs/agent.log. `python -m jarvis.voice.speech_gate` summarises the file. With JARVIS_SPEECH_GATE_SAVE_AUDIO (on
by default in shadow mode) each clip is also saved as the WAV Whisper heard, in logs/speech-gate-audio/, and its
jsonl line says which file; only the newest JARVIS_SPEECH_GATE_AUDIO_KEEP files are kept. With it off,
JARVIS_SPEECH_GATE_SAVE_SHORT (on by default) still saves the short clips it lets through, under SHORT_SECONDS long
or with under SHORT_SPEECH_SECONDS of speech, so a phantom that slips past can be heard; clips it drops aren't saved.
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

import numpy as np

from jarvis.core.config import (
    SPEECH_GATE,
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
    SPEECH_GATE_SAVE_AUDIO,
    SPEECH_GATE_SAVE_SHORT,
    SPEECH_GATE_SHORT_SECONDS,
    SPEECH_GATE_SHORT_SPEECH_SECONDS,
    SPEECH_GATE_UNSURE_SPEECH_SECONDS,
    SPEECH_GATE_WEAK_SPEECH_SECONDS,
)

log = logging.getLogger("jarvis.voice.speech_gate")

RATE = 16000  # what Whisper and Silero both take
WINDOW = 512  # Silero's window at 16 kHz: 32 ms
SPEECH_PROB = 0.5  # a window counts as speech from here: Silero's own activation threshold
LOUDNESS_WINDOW = RATE // 10  # 100 ms, for the loudest stretch

# What Whisper says on silence, noise or a click: the sign-offs of the videos it learnt from, and lone fillers. Matched
# against the whole transcript, lowercased and without punctuation ("Mm-hmm." is "mm hmm"). Never "yes", "no", "okay",
# "sure" or "go ahead": a real reply to a question must not need a louder voice.
PHANTOMS = (
    "thank you", "thank you very much", "thank you so much", "thanks", "thanks a lot", "thank you for watching",
    "thanks for watching", "thank you for listening", "thanks for listening", "please subscribe", "subscribe",
    "like and subscribe", "you", "bye", "bye bye", "goodbye", "so", "like", "oh", "um", "uh", "hmm", "mm", "mm hmm",
    "uh huh", "please", "and", "but", "the", "i", "music", "applause", "silence",
)


def normalise(text: str) -> str:
    """Lowercase words only: "Mm-hmm." is "mm hmm", "Thank you!" is "thank you"."""
    return " ".join(re.findall(r"[^\W_]+(?:'[^\W_]+)*", text.lower()))


def compression_ratio(text: str) -> float:
    """Whisper's own measure of a looping transcript: its UTF-8 length over its zlib-compressed length."""
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
    """One clip, as the gate saw it."""
    seconds: float
    rms_dbfs: float  # the whole clip, the VAD's padding included
    loudest_dbfs: float  # its loudest 100 ms
    speech_seconds: float  # how much of it Silero calls speech
    speech_ratio: float
    no_speech: float | None = None  # Whisper's no-speech probability, when it gave one
    avg_logprob: float | None = None  # Whisper's average log-probability of the text, when it gave one
    compression_ratio: float | None = None  # the text's


def _dbfs(x: np.ndarray) -> float:
    return float(20 * np.log10(max(float(np.sqrt(np.mean(np.square(x, dtype=np.float64)))), 1e-10)))


def measure(audio: np.ndarray, speech_probs: Callable[[np.ndarray], np.ndarray]) -> Measure:
    """Measure 16 kHz float audio; `speech_probs` gives the VAD's speech probability for each 32 ms window."""
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
    """Why the clip holds no real speech, or its transcript no real words; empty if it may. Without `text`, only the
    audio is judged. `echo`: the transcript is Whisper's prompt echoed back."""
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
    """Silero VAD over a whole clip, from a fresh state, on its own onnxruntime session (the session's VAD has its
    own). Loaded on first use."""

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
    """Write 16 kHz float audio to `folder` as a 16-bit WAV named by the local time `at`, then delete all but the
    newest `keep` WAVs there (the names sort by time)."""
    folder.mkdir(parents=True, exist_ok=True)
    stem = time.strftime("%Y%m%d-%H%M%S", time.localtime(at)) + f"-{int(at * 1000) % 1000:03d}"
    path, n = folder / f"{stem}.wav", 1
    while path.exists():  # two clips in the same millisecond ("_" sorts after ".", so the names stay in order)
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
    def __init__(self, mode: str = SPEECH_GATE, thresholds: Thresholds | None = None,
                 speech_probs: Callable[[np.ndarray], np.ndarray] | None = None,
                 record: Path | None = SPEECH_GATE_LOG,
                 audio_dir: Path | None = (SPEECH_GATE_AUDIO_DIR if SPEECH_GATE_SAVE_AUDIO or SPEECH_GATE_SAVE_SHORT
                                           else None),
                 keep_audio: int = SPEECH_GATE_AUDIO_KEEP, save_all: bool = SPEECH_GATE_SAVE_AUDIO,
                 short: tuple[float, float] = (SPEECH_GATE_SHORT_SECONDS, SPEECH_GATE_SHORT_SPEECH_SECONDS)) -> None:
        self.mode = mode
        self.thresholds = thresholds or Thresholds()
        self._probs = speech_probs or SileroProbs()
        self._record = record
        self._audio_dir, self._keep_audio = audio_dir, keep_audio  # no audio_dir: no clips saved
        # save_all: every clip; otherwise the short ones it lets through: under short[0] s long or short[1] s of speech
        self._save_all, self._short = save_all, short

    def use(self, mode: str, save_all: bool) -> None:
        """Switch mode, and whether every clip is saved, for the conversation starting (config.speech_gate())."""
        self.mode, self._save_all = mode, save_all
        if save_all and self._audio_dir is None:
            self._audio_dir = SPEECH_GATE_AUDIO_DIR

    @property
    def on(self) -> bool:
        return self.mode != "off"

    def describe(self) -> str:
        """For the agent log at start-up."""
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
        """The clip's measurements, or None with the gate off. Runs in the transcription thread."""
        if not self.on:
            return None
        try:
            return measure(audio, self._probs)
        except Exception:
            log.exception("speech gate couldn't measure a clip; it goes through")
            return None

    def passes(self, m: Measure | None, text: str, no_speech: float | None, audio: np.ndarray | None = None, *,
               avg_logprob: float | None = None, echo: bool = False) -> bool:
        """Whether the transcript goes on to the brain. Logs every clip it would drop, in either mode, and saves
        `audio`, the clip, when saving is on for it. `echo`: the transcript is Whisper's prompt echoed back."""
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
        """Whether this clip's audio is saved: every clip with save_all, else a short one that reaches the brain."""
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
    """What the gate made of the clips in its file since `since`: counts, then each clip it would drop or dropped."""
    try:
        lines = path.read_text().splitlines()
    except FileNotFoundError:
        return f"No clips yet: {path} doesn't exist."
    records = []
    for line in lines:
        try:
            records.append(json.loads(line))
        except ValueError:  # a line cut short by a crash
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


if __name__ == "__main__":  # python -m jarvis.voice.speech_gate [HOURS]: the last day's clips by default
    print(summary(since=time.time() - float(sys.argv[1] if sys.argv[1:] else 24) * 3600))
