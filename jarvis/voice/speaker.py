"""Speaker verification: is it the user speaking, or someone else (a TV, a video, a colleague across the room)?

A voiceprint is a speaker embedding from WeSpeaker's ResNet34 (ONNX, 25 MB, trained on VoxCeleb), averaged over about
25 seconds of the user's speech when they enroll; jarvis.state.voiceprints keeps it with their data. Each utterance's
speech is embedded the same way and compared with it by cosine similarity, and one that scores under what it needs
never reaches the brain: JARVIS stays silent, as it does for the speech gate's phantoms. With no voiceprint enrolled, or
JARVIS_SPEAKER_CHECK off, nothing is checked and everything goes through as before.

Only the clip's speech is judged: Silero VAD finds it, and the silence around the words (most of a short clip) is left
out, as it says nothing about who spoke. What a clip needs depends on how much speech it has, since a short one says
less about its speaker (config.SPEAKER_*):
  long    LONG_SECONDS of speech or more: the threshold (the speaker_threshold setting)
  short   SHORT_SECONDS or less: SHORT_MARGIN under it; in between, sliding from one to the other
  engaged while the user is in a conversation (they were the last one heard, within RECENT_SECONDS, or JARVIS
          finished speaking under REPLY_SECONDS ago, and nobody else has been heard since), a short clip needs RELAX
          less again, and one under MIN_SECONDS of speech is kept whatever it scores: a "yes" to a question
          shouldn't be lost to a sound the model can't place. Outside a conversation it's judged like any other.
  this call  the user's own clearly verified utterances in this conversation (SPEAKER_ADAPT) move a second
          reference halfway from the voiceprint towards them, for clips past SHORT_SECONDS, so the check follows the
          mic and room in use, not only enrollment's
JARVIS_SPEAKER_CHECK (or the Settings panel) picks what happens to a clip that isn't the user's:
  on      (default) drop it: the brain never hears it, and nothing is said
  shadow  log it and keep it, to see what it would drop before it drops anything
  off     no checks
While JARVIS replies, speech interrupts it only once this check has heard it's the user's (match; jarvis.voice.barge_in).
Every clip it judges goes in logs/speaker-check.jsonl with its score, what it needed and its transcript, and every
barge-in with "kind": "barge-in"; `python -m jarvis.voice.speaker log` summarises it.

Enrolling, in any of three ways, each replacing the voiceprint before it:
  voice   in a conversation, "learn my voice": the voice_enroll tool has the next utterances collected instead of
          answered, about 25 seconds of speech, through the very mic, codec and noise filter later calls use
  panel   Settings, Preferences, Enroll in the web app or the iPhone app records 30 seconds in the page
  cli     `make voiceprint ARGS=enroll` records 30 seconds from the Mac's mic
The model runs on the CPU in the process that needs it, loaded only once a voiceprint is in use: the call's own job
process (beside Whisper, which runs elsewhere, so it adds no wait), the web server for the panel's enrollment.
Nothing leaves the Mac.
"""

import argparse
import asyncio
import dataclasses
import json
import logging
import re
import sys
import threading
import time
from collections import Counter
from collections.abc import Callable
from pathlib import Path

import numpy as np

from jarvis.core import config
from jarvis.core.config import (
    SPEAKER_ADAPT,
    SPEAKER_ADAPT_MARGIN,
    SPEAKER_ENROLL_MIN_SECONDS,
    SPEAKER_ENROLL_SECONDS,
    SPEAKER_ENROLL_TIMEOUT,
    SPEAKER_LOG,
    SPEAKER_LONG_SECONDS,
    SPEAKER_MIN_SECONDS,
    SPEAKER_MODEL,
    SPEAKER_RECENT_SECONDS,
    SPEAKER_RELAX,
    SPEAKER_REPLY_SECONDS,
    SPEAKER_SHORT_MARGIN,
    SPEAKER_SHORT_SECONDS,
)
from jarvis.state import voiceprints
from jarvis.state.voiceprints import OWNER, Voiceprint

log = logging.getLogger("jarvis.voice.speaker")

RATE = 16000
FRAME, HOP, FFT = 400, 160, 512  # 25 ms frames every 10 ms, as Kaldi's fbank, padded to a power of two
MELS = 80
PREEMPHASIS = 0.97
EPSILON = float(np.finfo(np.float32).eps)  # Kaldi's floor before the log

WINDOW = 512  # Silero's window at 16 kHz: 32 ms
SPEECH_PROB = 0.5  # a window counts as speech from here, as for the speech gate
PAD_WINDOWS = 2  # speech is widened by this many windows each side (64 ms), so the edges of words stay
MIN_EMBED_SECONDS = 0.2  # less speech than this has no embedding at all
MAX_EMBED_SECONDS = 6.0  # the most of an utterance embedded: more says little new, and costs time
PIECE_SECONDS = 3.0  # enrollment's speech is embedded in pieces this long, then averaged
OUTLIER = 0.3  # an enrollment piece less alike than this to the rest (a cough, a door, someone else) is left out
MIN_CONSISTENCY = 0.65  # pieces less alike than this on average weren't one voice in a quiet room: enroll again
IDLE_SECONDS = 8.0  # a spoken enrollment with enough speech finishes after this long without more
RECORD_SECONDS = 30  # how long the panel and the command line record for an enrollment
STOP_WORDS = {"stop", "cancel", "stop it", "cancel that", "cancel it", "never mind", "nevermind", "forget it",
              "stop listening", "stop learning", "stop that"}

PASSAGE = ("Read this aloud at your usual pace, as you'd talk to JARVIS. Good morning. Could you check my calendar for "
           "today and tell me when my first meeting starts? After that, remind me to call the bank at four, and add "
           "milk and coffee to my shopping list. I'd also like a short summary of my unread email, the important ones "
           "first. Yesterday I spent most of the afternoon fixing a bug in the build; it turned out to be a missing "
           "file, which was annoying but simple in the end. This weekend I'm hoping to go for a long walk, cook "
           "something new, and catch up with a few friends over dinner. Thank you, that's all for now.")


# ── The model ───────────────────────────────────────────────────


def _mel(hz: np.ndarray | float) -> np.ndarray:
    return 1127.0 * np.log(1.0 + np.asarray(hz, dtype=np.float64) / 700.0)


def _mel_banks(low: float = 20.0, high: float = RATE / 2) -> np.ndarray:
    """Kaldi's triangular mel filters, as a (FFT // 2 + 1, MELS) matrix for the power spectrum."""
    lo, hi = _mel(low), _mel(high)
    delta = (hi - lo) / (MELS + 1)
    left = lo + np.arange(MELS)[:, None] * delta
    centre, right = left + delta, left + 2 * delta
    mel = _mel(RATE / FFT * np.arange(FFT // 2))[None, :]
    banks = np.maximum(0.0, np.minimum((mel - left) / (centre - left), (right - mel) / (right - centre)))
    return np.pad(banks, ((0, 0), (0, 1))).T.astype(np.float32)  # Kaldi leaves out the Nyquist bin


_BANKS = _mel_banks()
_HAMMING = (0.54 - 0.46 * np.cos(2 * np.pi * np.arange(FRAME) / (FRAME - 1))).astype(np.float32)


def fbank(audio: np.ndarray) -> np.ndarray:
    """80 log mel filterbank energies every 10 ms of 16 kHz float audio, as Kaldi computes them (torchaudio's
    compliance.kaldi.fbank with a Hamming window and no dither, which is what WeSpeaker trained on): (frames, 80).
    Audio shorter than one 25 ms frame has no frames."""
    samples = np.asarray(audio, dtype=np.float32) * 32768.0  # WeSpeaker scales to 16-bit sample values first
    if len(samples) < FRAME:
        return np.zeros((0, MELS), np.float32)
    count = 1 + (len(samples) - FRAME) // HOP
    frames = np.lib.stride_tricks.sliding_window_view(samples, FRAME)[::HOP][:count].astype(np.float64)
    frames = frames - frames.mean(axis=1, keepdims=True)  # each frame's DC offset
    frames = frames - PREEMPHASIS * np.concatenate([frames[:, :1], frames[:, :-1]], axis=1)
    spectrum = np.abs(np.fft.rfft(frames * _HAMMING, FFT)) ** 2
    return np.log(np.maximum(spectrum.astype(np.float32) @ _BANKS, EPSILON)).astype(np.float32)


def unit(v: np.ndarray) -> np.ndarray:
    v = np.asarray(v, dtype=np.float32)
    norm = float(np.linalg.norm(v))
    return v / norm if norm > 0 else v


def cosine(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.dot(unit(a), unit(b)))


class Embedder:
    """The speaker model, through onnxruntime on the CPU, loaded on first use: about 80 MB while in use."""

    def __init__(self, path: Path = SPEAKER_MODEL, threads: int = 2) -> None:
        self.path, self._threads = path, threads
        self._session = None
        self._lock = threading.Lock()

    @property
    def available(self) -> bool:
        return self._session is not None or self.path.is_file()

    def _load(self):
        with self._lock:
            if self._session is None:
                import onnxruntime as ort

                options = ort.SessionOptions()
                options.intra_op_num_threads = self._threads
                options.enable_cpu_mem_arena = False  # its buffers are freed after each clip, as Kokoro's are
                options.add_session_config_entry("session.intra_op.allow_spinning", "0")
                self._session = ort.InferenceSession(str(self.path), options, providers=["CPUExecutionProvider"])
                log.info("speaker model loaded: %s", self.path.name)
        return self._session

    def __call__(self, speech: np.ndarray) -> np.ndarray:
        """The unit-length embedding of 16 kHz speech (its first MAX_EMBED_SECONDS). Raises ValueError for less than
        MIN_EMBED_SECONDS of it."""
        if len(speech) < MIN_EMBED_SECONDS * RATE:
            raise ValueError("too little speech to embed")
        feats = fbank(speech[:int(MAX_EMBED_SECONDS * RATE)])
        feats = feats - feats.mean(axis=0)  # WeSpeaker's mean normalisation, over the utterance
        session = self._session or self._load()
        return unit(session.run(None, {session.get_inputs()[0].name: feats[None]})[0][0])  # pyright: ignore[reportIndexIssue]


def speech_only(audio: np.ndarray, speech_probs: Callable[[np.ndarray], np.ndarray]) -> np.ndarray:
    """The clip's speech windows, each widened by PAD_WINDOWS, joined; empty if the VAD found none."""
    probs = np.asarray(speech_probs(audio)) >= SPEECH_PROB
    keep = probs.copy()
    for shift in range(1, PAD_WINDOWS + 1):
        keep[shift:] |= probs[:-shift]
        keep[:-shift] |= probs[shift:]
    windows = np.flatnonzero(keep)
    if not len(windows):
        return np.zeros(0, np.float32)
    return np.concatenate([audio[w * WINDOW:(w + 1) * WINDOW] for w in windows]).astype(np.float32)


def silero() -> Callable[[np.ndarray], np.ndarray]:
    """Silero VAD over a whole clip, on its own session (jarvis.voice.speech_gate.SileroProbs)."""
    from jarvis.voice.speech_gate import SileroProbs

    return SileroProbs()


# ── Enrolling ───────────────────────────────────────────────────


def pieces(speech: np.ndarray, seconds: float = PIECE_SECONDS) -> list[np.ndarray]:
    """Speech cut into pieces about `seconds` long; a short end is joined to the piece before it."""
    size = int(seconds * RATE)
    cut = [speech[i:i + size] for i in range(0, len(speech), size)]
    if len(cut) > 1 and len(cut[-1]) < size / 2:
        cut[-2:] = [np.concatenate(cut[-2:])]
    return cut


def voiceprint(speech: np.ndarray, embed: Callable[[np.ndarray], np.ndarray], *, name: str = OWNER,
               via: str = "voice", minimum: float = SPEAKER_ENROLL_MIN_SECONDS) -> Voiceprint:
    """A voiceprint from `speech` (the VAD's speech only, 16 kHz): its pieces' embeddings averaged, any piece unlike
    the rest left out. Raises ValueError, saying what to do, for too little speech or pieces too unlike each other."""
    seconds = len(speech) / RATE
    if seconds < minimum:
        raise ValueError(f"I heard only {seconds:.0f} seconds of speech, and need at least {minimum:.0f}. Try again, "
                         f"talking for about half a minute.")
    found = np.array([embed(p) for p in pieces(speech)])
    centre = unit(found.mean(axis=0))
    alike = found @ centre
    if (alike >= OUTLIER).sum() >= max(2, len(found) * 2 // 3):  # a few odd pieces out, the rest averaged again
        found = found[alike >= OUTLIER]
        centre = unit(found.mean(axis=0))
        alike = found @ centre
    consistency = float(alike.mean())
    if consistency < MIN_CONSISTENCY:
        raise ValueError("That didn't sound like one voice throughout: more than one person, or a lot of noise. Try "
                         "again somewhere quieter, with only you talking.")
    return Voiceprint(centre, seconds, consistency, name=name, via=via)


def enroll(audio: np.ndarray, *, name: str = OWNER, via: str = "panel", embed: Embedder | None = None,
           speech_probs: Callable[[np.ndarray], np.ndarray] | None = None, save: bool = True) -> Voiceprint:
    """Enroll from a recording, 16 kHz float audio (the panel's, the command line's): the voiceprint, saved unless
    `save` is False. Raises ValueError, saying what to do, if it can't make one."""
    embed = embed or Embedder()
    if not embed.available:
        raise ValueError("The speaker model isn't on this Mac: run make models, then try again.")
    speech = speech_only(np.asarray(audio, dtype=np.float32), speech_probs or silero())
    made = voiceprint(speech, embed, name=name, via=via)
    return voiceprints.save(made) if save else made


def to_float(pcm16: bytes) -> np.ndarray:
    """16-bit little-endian PCM as float audio."""
    return np.frombuffer(pcm16[:len(pcm16) // 2 * 2], dtype="<i2").astype(np.float32) / 32768.0


# ── Checking each utterance, in a conversation ──────────────────


@dataclasses.dataclass(frozen=True)
class Thresholds:
    threshold: float = 0.35
    short_margin: float = SPEAKER_SHORT_MARGIN
    relax: float = SPEAKER_RELAX
    short_seconds: float = SPEAKER_SHORT_SECONDS
    long_seconds: float = SPEAKER_LONG_SECONDS
    min_seconds: float = SPEAKER_MIN_SECONDS
    recent_seconds: float = SPEAKER_RECENT_SECONDS
    reply_seconds: float = SPEAKER_REPLY_SECONDS
    adapt: bool = SPEAKER_ADAPT
    adapt_margin: float = SPEAKER_ADAPT_MARGIN

    def needed(self, speech_seconds: float, engaged: bool = False) -> float:
        """The score a clip with this much speech needs: the threshold from long_seconds up, short_margin less at
        short_seconds and under, sliding in between; and relax less again, as much shorter, while engaged."""
        span = self.long_seconds - self.short_seconds
        shortness = 1 - min(1.0, max(0.0, (speech_seconds - self.short_seconds) / span)) if span > 0 else \
            float(speech_seconds < self.long_seconds)
        return round(self.threshold - (self.short_margin + (self.relax if engaged else 0.0)) * shortness, 3)


@dataclasses.dataclass
class Judgement:
    """One clip, as the check saw it."""
    seconds: float  # the whole clip
    speech_seconds: float  # the VAD's speech in it, what was judged
    score: float | None  # cosine similarity to the closest reference; None with too little speech to embed
    needed: float
    engaged: bool  # the user was in a conversation
    verdict: str  # kept, dropped, or "would drop" in shadow mode
    why: str = ""  # why it was kept without its score counting, or why not
    reference: str = ""  # which reference it was closest to: a voiceprint's name, or "this call"

    @property
    def drop(self) -> bool:
        return self.verdict == "dropped"

    @property
    def other(self) -> bool:
        """Judged someone else, whether or not it was dropped for it."""
        return self.verdict != "kept"


@dataclasses.dataclass
class Interruption:
    """Speech heard while JARVIS replied, as the barge-in check saw it (jarvis.voice.barge_in)."""
    speech_seconds: float  # the speech judged in its best check
    score: float | None  # that check's score; None with too little speech to embed
    needed: float
    verdict: str  # interrupted or ignored; "would interrupt" or "would ignore" in shadow mode
    checks: int  # how often it was judged, as more speech came
    held: float  # seconds JARVIS's reply was held from being cut off, from when LiveKit would have cut it
    why: str = ""
    reference: str = ""


class SpeakerCheck:
    """The speaker check for one job process's conversations: set for each by use(), told when JARVIS speaks
    (jarvis_speaking), asked about each clip by WhisperSTT (judge, in a thread), and collecting a spoken enrollment
    when asked to (start_enrollment)."""

    def __init__(self, mode: str = "off", thresholds: Thresholds | None = None, embedder: Embedder | None = None,
                 speech_probs: Callable[[np.ndarray], np.ndarray] | None = None, record: Path | None = SPEAKER_LOG,
                 clock: Callable[[], float] = time.monotonic,
                 keep: Callable[[Voiceprint], Voiceprint] = voiceprints.save) -> None:
        self.mode = mode
        self.thresholds = thresholds or Thresholds()
        self.prints: list[Voiceprint] = []
        self._embed = embedder or Embedder()
        self._probs = speech_probs
        self._record = record
        self._clock = clock
        self._keep = keep  # where a spoken enrollment's voiceprint goes
        self.announce: Callable[[str], object] = lambda text: None  # says something unprompted, set by the agent
        self._reset()

    def _reset(self) -> None:
        self._user_at = self._other_at = self._jarvis_at = None  # when each was last heard (monotonic)
        self._speaking = False  # JARVIS is speaking now
        self._session: list[np.ndarray] = []  # this call's clearly verified utterances
        self._warned = False
        self._enrolling: dict | None = None
        self._timer: asyncio.TimerHandle | None = None

    def use(self, mode: str, prints: list[Voiceprint], threshold: float | None = None) -> None:
        """The mode, voiceprints and threshold for the conversation starting (config.speaker_check(),
        voiceprints.load(), config.speaker_threshold()); what the last one learned is forgotten."""
        self.cancel_enrollment()
        self.mode, self.prints = mode, list(prints)
        if threshold is not None:
            self.thresholds = dataclasses.replace(self.thresholds, threshold=threshold)
        self._reset()
        if self.prints and self.mode != "off" and not self._embed.available:
            log.warning("speaker check off: the speaker model %s is missing; run make models", self._embed.path)
        log.info(self.describe())

    @property
    def on(self) -> bool:
        return self.mode != "off" and bool(self.prints) and self._embed.available

    def warm_up(self) -> None:
        """Load the model and VAD now, if the check is on, rather than on the conversation's first words."""
        if self.on:
            try:
                self._embed(np.zeros(RATE, np.float32))
                self._speech_probs()(np.zeros(WINDOW, np.float32))
            except Exception:
                log.exception("speaker check couldn't load its model")

    def describe(self) -> str:
        """For the agent log."""
        if self.mode == "off":
            return "speaker check: off"
        if not self.prints:
            return "speaker check: no voiceprint enrolled, so every voice goes through (enroll: \"learn my voice\")"
        t = self.thresholds
        return (f"speaker check: {self.mode} mode ({'logs, drops nothing' if self.mode == 'shadow' else 'drops'} "
                f"speech unlike {', '.join(p.name for p in self.prints)}: a score of {t.threshold:g} needed from "
                f"{t.long_seconds:g} s of speech, {t.threshold - t.short_margin:g} at {t.short_seconds:g} s and under, "
                f"{t.relax:g} less in a conversation, where under {t.min_seconds:g} s goes through"
                f"{'; adapting to this call' if t.adapt else ''})")

    # The conversation around each clip

    def jarvis_speaking(self, speaking: bool) -> None:
        """JARVIS started or stopped speaking (the session's agent state). A clip soon after it stops is likely a
        reply."""
        if self._speaking and not speaking:
            self._jarvis_at = self._clock()
        self._speaking = speaking

    def engaged(self) -> bool:
        """Whether the user is in a conversation now: they were the last one heard, lately, or JARVIS just spoke,
        and nobody else has been heard since."""
        now, t, other = self._clock(), self.thresholds, self._other_at or float("-inf")
        user = self._user_at is not None and now - self._user_at < t.recent_seconds and self._user_at >= other
        replying = self._speaking or (self._jarvis_at is not None and now - self._jarvis_at < t.reply_seconds
                                      and self._jarvis_at >= other)
        return user or replying

    def _references(self, speech_seconds: float) -> list[tuple[str, np.ndarray]]:
        """The voiceprints, and once the user has been clearly heard in this call, the first voiceprint moved halfway
        towards how they sound in it: anchored to the voiceprint, so it can't drift to whatever else this mic hears."""
        found = [(p.name, p.embedding) for p in self.prints]
        if self.thresholds.adapt and self._session and speech_seconds > self.thresholds.short_seconds:
            found.append(("this call", unit(unit(self.prints[0].embedding) + unit(np.mean(self._session, axis=0)))))
        return found

    def judge(self, audio: np.ndarray) -> Judgement | None:
        """Whether a clip, 16 kHz float audio, is the user's; None with the check off, or when it fails (the clip
        then goes through, as without a voiceprint). Runs in a thread."""
        if not self.on:
            return None
        try:
            return self._judge(audio)
        except Exception:
            if not self._warned:
                log.exception("speaker check couldn't judge a clip; letting clips through")
                self._warned = True
            return None

    def _judge(self, audio: np.ndarray) -> Judgement:
        t, engaged = self.thresholds, self.engaged()
        speech = speech_only(audio, self._speech_probs())
        speech_seconds = round(len(speech) / RATE, 3)
        needed = t.needed(speech_seconds, engaged)
        score, reference, why = None, "", ""
        try:
            embedding = self._embed(speech)
        except ValueError:  # too little speech to tell
            embedding = None
        if embedding is not None:
            reference, score = max(((name, cosine(embedding, ref)) for name, ref in self._references(speech_seconds)),
                                   key=lambda found: found[1])
            score = round(score, 3)
        if engaged and speech_seconds < t.min_seconds:
            ok, why = True, "too short to tell, in a conversation"
        elif score is None:
            ok, why = False, "too little speech to tell"
        else:
            ok = score >= needed
            why = "" if ok else "not the user's voice"
        now = self._clock()
        if ok and not why and speech_seconds >= t.min_seconds:  # heard clearly, not let off as too short
            self._user_at = now
            if embedding is not None and speech_seconds >= t.long_seconds and score >= t.threshold + t.adapt_margin:
                self._session = [*self._session[-9:], embedding]
        elif not ok and speech_seconds >= t.short_seconds:
            self._other_at = now
        verdict = "kept" if ok else "dropped" if self.mode == "on" else "would drop"
        return Judgement(round(len(audio) / RATE, 3), speech_seconds, score, needed, engaged, verdict, why, reference)

    def _speech_probs(self) -> Callable[[np.ndarray], np.ndarray]:
        if self._probs is None:
            self._probs = silero()
        return self._probs

    def record(self, j: Judgement, text: str | None) -> None:
        """Log a judged clip with its transcript: every one in SPEAKER_LOG, the ones not the user's in the agent log."""
        if j.other:
            log.info("speaker check %s%s a %.2f s clip (%.2f s of speech): score %s, needed %.2f%s: %r", j.verdict,
                     " (shadow)" if j.verdict == "would drop" else "", j.seconds, j.speech_seconds,
                     "n/a" if j.score is None else f"{j.score:.2f}", j.needed, ", in a conversation" if j.engaged
                     else "", text)
        self._write({**dataclasses.asdict(j), "text": text})

    def _write(self, entry: dict) -> None:
        if self._record is None:
            return
        try:
            self._record.parent.mkdir(parents=True, exist_ok=True)
            with open(self._record, "a") as f:
                f.write(json.dumps({"time": round(time.time(), 1), **entry}) + "\n")
        except OSError as e:
            log.warning("speaker check couldn't write %s: %s", self._record, e)

    # Barge-in: speech heard while JARVIS replies (jarvis.voice.barge_in)

    def match(self, speech: np.ndarray) -> tuple[float | None, float, str]:
        """How alike speech heard while JARVIS replies (the VAD's speech only, 16 kHz) is to the user: (score, needed,
        reference), the score None with too little to embed. It needs what a clip as long needs outside a conversation,
        with nothing let off as too short, and the call's own reference counts at any length. The conversation's state
        is left alone: the whole utterance is judged again when it ends. Runs in a thread."""
        needed = self.thresholds.needed(len(speech) / RATE)
        try:
            embedding = self._embed(speech)
        except ValueError:  # too little speech to tell
            return None, needed, ""
        reference, score = max(((name, cosine(embedding, ref)) for name, ref in self._references(float("inf"))),
                               key=lambda found: found[1])
        return round(score, 3), needed, reference

    def record_interruption(self, i: Interruption) -> None:
        """Log a barge-in decision: in SPEAKER_LOG, with "kind": "barge-in", and in the agent log."""
        log.info("barge-in %s after %d check%s, held %.0f ms: %.2f s of speech, score %s, needed %.2f%s", i.verdict,
                 i.checks, "" if i.checks == 1 else "s", i.held * 1000, i.speech_seconds,
                 "n/a" if i.score is None else f"{i.score:.2f}", i.needed, f" ({i.why})" if i.why else "")
        self._write({"kind": "barge-in", **dataclasses.asdict(i)})

    # Enrolling by voice, in this conversation

    @property
    def enrolling(self) -> bool:
        return self._enrolling is not None

    def start_enrollment(self, name: str = OWNER) -> str:
        """Collect the user's next utterances for a voiceprint instead of passing them on; returns what to tell
        them. Finishes by itself (announce) once it has SPEAKER_ENROLL_SECONDS of speech, or enough and a pause."""
        if not self._embed.available:
            return "The speaker model isn't installed on the Mac: run make models there, then restart JARVIS."
        self.cancel_enrollment()
        self._enrolling = {"name": name, "speech": [], "seconds": 0.0, "started": self._clock()}
        self.rearm()
        log.info("enrolling %s's voice: collecting %.0f s of speech", name, SPEAKER_ENROLL_SECONDS)
        return ("Listening for their voice now. Tell them to talk naturally for about thirty seconds, about anything "
                "(their day, or something they read aloud), somewhere quiet, and that saying stop or cancel stops it. "
                "Their words won't reach you meanwhile, and JARVIS tells them by itself when it has enough.")

    def cancel_enrollment(self) -> bool:
        """Stop an enrollment under way; False if none was. Its timer, if left, finds it gone and does nothing."""
        self._disarm()
        was, self._enrolling = self._enrolling is not None, None
        return was

    def _disarm(self) -> None:
        """Cancel the enrollment's timer, from the event loop's thread only: from the transcription thread it's left
        to run out, and finds its enrollment over."""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    def rearm(self) -> None:
        """(Re)start an enrollment's timer, on the event loop: IDLE_SECONDS once it has enough, else what's left of
        the timeout. Nothing without one under way, or off the loop (the transcription thread's caller re-arms it)."""
        if (e := self._enrolling) is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._disarm()
        wait = IDLE_SECONDS if e["seconds"] >= SPEAKER_ENROLL_MIN_SECONDS else \
            max(0.0, SPEAKER_ENROLL_TIMEOUT - (self._clock() - e["started"]))
        self._timer = loop.call_later(wait, lambda: asyncio.ensure_future(self._lapse(e)))

    async def _lapse(self, e: dict) -> None:
        """The timer of enrollment `e` ran out: finish with what it has, if that's enough, unless it's over."""
        if self._enrolling is not e:
            return
        self._timer = None
        if e["seconds"] >= SPEAKER_ENROLL_MIN_SECONDS:
            self.announce(await asyncio.to_thread(self._finish))
        else:
            self.cancel_enrollment()
            self.announce(f"I stopped listening for your voice: I heard only {e['seconds']:.0f} seconds of speech. "
                          f"Ask me to learn your voice again when you're ready.")

    def collect(self, audio: np.ndarray, text: str = "") -> str | None:
        """An utterance while enrolling, which isn't passed on: its speech kept, unless JARVIS was speaking (its own
        voice could be in it). Returns what to say when that finishes the enrollment, or it was told to stop; None
        to stay silent. Runs in a thread; the caller re-arms the timer (rearm)."""
        if (e := self._enrolling) is None:
            return None
        if re.sub(r"[^\w ]", "", text.lower()).strip() in STOP_WORDS:
            self.cancel_enrollment()
            return "Okay, I've stopped. Your voiceprint is as it was."
        if self._speaking:
            return None
        speech = speech_only(audio, self._speech_probs())
        e["speech"].append(speech)
        e["seconds"] += len(speech) / RATE
        log.info("enrolling: %.1f s of speech so far", e["seconds"])
        return self._finish() if e["seconds"] >= SPEAKER_ENROLL_SECONDS else None

    def _finish(self) -> str:
        """Make and keep the voiceprint from what was collected; what to tell the user."""
        e, self._enrolling = self._enrolling, None
        self._disarm()
        if e is None:
            return ""
        try:
            made = self._keep(voiceprint(np.concatenate(e["speech"]), self._embed, name=e["name"], via="voice",
                                         minimum=min(SPEAKER_ENROLL_MIN_SECONDS, SPEAKER_ENROLL_SECONDS)))
        except ValueError as problem:
            return f"I couldn't learn your voice. {problem}"
        except Exception:
            log.exception("enrolling a voice failed")
            return "Something went wrong learning your voice; it's in the agent log. Your voiceprint is as it was."
        self.prints = sorted([p for p in self.prints if p.name != made.name] + [made], key=lambda p: p.name != OWNER)
        self._session, self._user_at = [], self._clock()
        return "Got it: I know your voice now" + {
            "on": ", and I'll ignore other voices.", "shadow": ". The speaker check is only logging for now.",
            "off": ". The speaker check is off, though: switch it on in Settings."}.get(self.mode, ".")


# ── The command line ────────────────────────────────────────────


def summary(path: Path = SPEAKER_LOG, since: float = 0.0) -> str:
    """What the check made of the clips in its file since `since`: counts, scores, then each one not the user's; then
    the barge-ins it judged, and each one it didn't let interrupt."""
    try:
        lines = path.read_text().splitlines()
    except FileNotFoundError:
        return f"No clips yet: {path} doesn't exist."
    records = []
    for line in lines:
        try:
            records.append(json.loads(line))
        except ValueError:
            continue
    records = [r for r in records if r.get("time", 0) >= since]
    barge_ins = [r for r in records if r.get("kind") == "barge-in"]
    records = [r for r in records if r.get("kind") != "barge-in"]
    other = [r for r in records if r["verdict"] != "kept"]
    scored = [r["score"] for r in records if r.get("score") is not None]
    out = [f"{len(records)} clips, {len(other)} judged not the user's."]
    if scored:
        out.append("Scores: lowest {:.2f}, median {:.2f}, highest {:.2f}.".format(*np.quantile(scored, [0, 0.5, 1])))
    if counts := Counter(r["verdict"] for r in records):
        out.append(", ".join(f"{v} {n}" for v, n in counts.most_common()) + ".")
    for r in other:
        score = "n/a" if r.get("score") is None else f"{r['score']:.2f}"
        out.append(f"{time.strftime('%m-%d %H:%M:%S', time.localtime(r['time']))}  {r['verdict']:10}  "
                   f"speech {r['speech_seconds']:5.2f} s  score {score:>5} / {r['needed']:.2f}"
                   f"{'  in conversation' if r.get('engaged') else ''}  {r.get('text')!r}")
    if barge_ins:
        counts = Counter(r["verdict"] for r in barge_ins)
        through = [r["held"] for r in barge_ins if "interrupt" in r["verdict"]]
        out.append(f"{len(barge_ins)} barge-ins while JARVIS replied: "
                   + ", ".join(f"{v} {n}" for v, n in counts.most_common())
                   + (f"; the user's held {np.median(through) * 1000:.0f} ms (median)." if through else "."))
    for r in barge_ins:
        if "ignore" in r["verdict"]:
            score = "n/a" if r.get("score") is None else f"{r['score']:.2f}"
            out.append(f"{time.strftime('%m-%d %H:%M:%S', time.localtime(r['time']))}  barge-in {r['verdict']:12}  "
                       f"speech {r['speech_seconds']:5.2f} s  best score {score:>5} / {r['needed']:.2f}  "
                       f"{r['checks']} checks")
    return "\n".join(out)


def record_mic(seconds: float) -> np.ndarray:
    """Record from the Mac's default mic, 16 kHz mono, showing the time left."""
    import sounddevice

    frames = sounddevice.rec(int(seconds * RATE), samplerate=RATE, channels=1, dtype="float32")
    for left in range(int(seconds), 0, -1):
        print(f"\r  recording: {left:2d} s left ", end="", flush=True)
        time.sleep(1)
    sounddevice.wait()
    print("\r  recorded.            ")
    return frames[:, 0]


def _status() -> None:
    print(f"Speaker check: {config.speaker_check()}, threshold {config.speaker_threshold():g}. Model: "
          f"{SPEAKER_MODEL.name}{'' if SPEAKER_MODEL.is_file() else ' (missing: run make models)'}.")
    found = voiceprints.every()
    for p in found:
        print(f"  {p.name}: {p.seconds:.0f} s of speech, consistency {p.consistency:.2f}, via {p.via}, "
              f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(p.created))}"
              + ("" if p.model == config.SPEAKER_MODEL_ID else f" (from {p.model}: enroll again)"))
    if not found:
        print("  No voiceprint: JARVIS answers any voice. Enroll with: make voiceprint ARGS=enroll")


def _enroll(name: str, seconds: float, record: Callable[[float], np.ndarray]) -> None:
    print(PASSAGE, "\n\nStarting in 3 seconds...")
    time.sleep(3)
    try:
        made = enroll(record(seconds), name=name, via="cli")
    except ValueError as e:
        sys.exit(str(e))
    print(f"Saved {made.name}'s voiceprint: {made.seconds:.0f} s of speech, consistency {made.consistency:.2f}. "
          f"JARVIS uses it from the next conversation.")


def _test(seconds: float, record: Callable[[float], np.ndarray]) -> None:
    if not (prints := voiceprints.load()):
        sys.exit("No voiceprint to compare with: make voiceprint ARGS=enroll")
    print("Say something...")
    speech = speech_only(record(seconds), silero())
    embedding, said = Embedder()(speech), len(speech) / RATE
    needed = Thresholds(threshold=config.speaker_threshold()).needed(said)
    for p in prints:
        score = cosine(embedding, p.embedding)
        print(f"  {p.name}: {score:.2f} (needs {needed:.2f} for {said:.1f} s of speech; "
              f"{'yes' if score >= needed else 'no'})")


def main(argv: list[str] | None = None, record: Callable[[float], np.ndarray] = record_mic) -> int:
    parser = argparse.ArgumentParser(prog="python -m jarvis.voice.speaker",
                                     description="JARVIS's speaker check: the voiceprint and what it did with it.")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status", help="the voiceprints, and the check's settings")
    enroll_ = commands.add_parser("enroll", help="record about 30 seconds from the Mac's mic and keep the voiceprint")
    enroll_.add_argument("--name", default=OWNER, help=f"whose voice ({OWNER}, the default, is the user JARVIS works for)")
    enroll_.add_argument("--seconds", type=float, default=RECORD_SECONDS)
    remove_ = commands.add_parser("remove", help="forget a voiceprint")
    remove_.add_argument("--name", default=OWNER)
    test = commands.add_parser("test", help="record a few seconds and say how alike they are to the voiceprints")
    test.add_argument("--seconds", type=float, default=5.0)
    history = commands.add_parser("log", help="what the check made of the last day's clips")
    history.add_argument("hours", nargs="?", type=float, default=24.0)
    args = parser.parse_args(argv)

    if args.command == "log":
        print(summary(since=time.time() - args.hours * 3600))
    elif args.command == "status":
        _status()
    elif args.command == "remove":
        print("Forgotten." if voiceprints.remove(args.name) else f"There's no voiceprint for {args.name}.")
    elif not SPEAKER_MODEL.is_file():
        sys.exit("The speaker model is missing: run make models first.")
    elif args.command == "enroll":
        _enroll(args.name, args.seconds, record)
    else:
        _test(args.seconds, record)
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    sys.exit(main())
