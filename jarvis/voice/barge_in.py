"""Barge-in by the user's voice only, so a TV or another person can't cut JARVIS off mid-reply.

LiveKit interrupts after 0.5 s of any VAD speech. BargeInVAD zeroes speech_duration while JARVIS is replying until the
speaker check matches the user's voiceprint; LiveKit then interrupts on the next event (32 ms later).

  when    from SPEAKER_BARGE_IN_MIN_SECONDS (0.5 s, LiveKit's own wait) so a check (~15 ms CPU) adds little; again
          every STEP, on the latest MAX_SECONDS, until a match or speech ends
  needs   the normal short-clip threshold (0.25 at <= 1 s); no reply-to-JARVIS leniency
  else    the reply continues; the full utterance is still checked at its end and can interrupt then

Pass-through when the check is off, no voiceprint is enrolled or JARVIS_SPEAKER_BARGE_IN is off. Shadow mode passes
through but logs what it would have done. Judged barge-ins go to logs/speaker-check.jsonl ("kind": "barge-in").
"""

import asyncio
import collections
import dataclasses
import logging
import time
from collections.abc import Callable

from livekit import rtc
from livekit.agents import vad

from jarvis.core.config import (
    SPEAKER_BARGE_IN,
    SPEAKER_BARGE_IN_MAX_SECONDS,
    SPEAKER_BARGE_IN_MIN_SECONDS,
    SPEAKER_BARGE_IN_STEP,
    VAD_THRESHOLD,
)
from jarvis.voice.speaker import RATE, Interruption, SpeakerCheck
from jarvis.voice.speech import _to_whisper_audio

log = logging.getLogger("jarvis.voice.barge_in")

REPLYING = ("thinking", "speaking")  # agent states where speech interrupts


@dataclasses.dataclass
class _Episode:
    """One stretch of speech, first speech window to VAD end."""
    speech: collections.deque = dataclasses.field(default_factory=collections.deque)  # latest speech windows
    kept: float = 0.0  # seconds of speech in `speech`
    heard: float = 0.0  # total speech seconds
    replying: bool = False  # JARVIS was replying during it
    allowed: bool = False  # may interrupt: user matched, or check errored
    busy: bool = False  # a check is running
    checked: float | None = None  # `heard` at the last check
    since: float | None = None  # when it could first have interrupted
    checks: int = 0
    best: tuple | None = None  # (score, needed, seconds, reference) of the closest check
    why: str = ""
    ended: bool = False


class BargeIn:
    """Per-conversation gate on whether speech may interrupt a reply. Fed agent state (`replying`) and VAD events
    (`hear`)."""

    def __init__(self, check: SpeakerCheck, enabled: bool = SPEAKER_BARGE_IN,
                 min_seconds: float = SPEAKER_BARGE_IN_MIN_SECONDS, max_seconds: float = SPEAKER_BARGE_IN_MAX_SECONDS,
                 step: float = SPEAKER_BARGE_IN_STEP, threshold: float = VAD_THRESHOLD,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.check, self.enabled = check, enabled
        self.min_seconds, self.max_seconds, self.step = min_seconds, max_seconds, step
        self.threshold = threshold  # speech probability cutoff, as the session VAD
        self._clock = clock
        self._replying = False
        self._owner: object | None = None  # stream whose speech is judged; others follow its verdict
        self._episode = _Episode()
        self._warned = False
        self._tasks: set[asyncio.Future] = set()

    def replying(self, state: str) -> None:
        """Handle agent_state_changed."""
        self._replying = state in REPLYING

    @property
    def judging(self) -> bool:
        """True when the speaker check is on or in shadow mode (with a voiceprint)."""
        return self.enabled and self.check.on

    def hear(self, stream: object, ev: vad.VADEvent) -> vad.VADEvent:
        """Return the event to pass on. All streams hear the same audio, so only the first one is judged."""
        try:
            if not self.judging:
                if self._episode.heard:
                    self._episode = _Episode()
                return ev
            if self._owner is None:
                self._owner = stream
            if stream is self._owner:
                self._observe(ev)
            # Hide speech from LiveKit while replying until it's known to be the user's
            holding = self._replying and not self._episode.allowed and self.check.mode == "on"
            if holding and ev.type == vad.VADEventType.INFERENCE_DONE and ev.speech_duration > 0:
                return dataclasses.replace(ev, speech_duration=0.0)
        except Exception:
            if not self._warned:
                log.exception("barge-in check failed; any voice interrupts JARVIS until the next conversation")
                self._warned, self.enabled = True, False
        return ev

    def release(self, stream: object) -> None:
        """`stream` closed; another becomes the judged one."""
        if stream is self._owner:
            self._owner = None
            self._end()

    def _observe(self, ev: vad.VADEvent) -> None:
        e = self._episode
        if ev.type == vad.VADEventType.END_OF_SPEECH:
            self._end()
            return
        if ev.type != vad.VADEventType.INFERENCE_DONE:
            return
        if ev.probability < self.threshold:
            if not ev.speaking and e.heard:  # too short to start speech, or speech over
                self._end()
            return
        for frame in ev.frames:
            e.speech.append(frame)
            e.kept += _seconds(frame)
            e.heard += _seconds(frame)
        while len(e.speech) > 1 and e.kept - _seconds(e.speech[0]) >= self.max_seconds:
            e.kept -= _seconds(e.speech.popleft())
        e.replying |= self._replying
        due = (e.replying and not e.allowed and not e.busy and (self._replying or self.check.mode != "on")
               and ev.speech_duration >= self.min_seconds and (e.checked is None or e.heard - e.checked >= self.step))
        if due:
            e.since = self._clock() if e.since is None else e.since
            e.busy, e.checked = True, e.heard
            self._spawn(self._judge(e, list(e.speech)))

    async def _judge(self, e: _Episode, frames: list[rtc.AudioFrame]) -> None:
        try:
            score, needed, seconds, reference = await asyncio.to_thread(self._match, frames)
        except Exception:
            if not self._warned:
                log.exception("barge-in check couldn't judge speech; letting it interrupt")
                self._warned = True
            score, needed, seconds, reference, e.why = None, 0.0, 0.0, "", "check failed"
        finally:
            e.busy = False
        e.checks += 1
        if e.best is None or _margin(score, needed) > _margin(*e.best[:2]):
            e.best = (score, needed, seconds, reference)
        if e.ended:
            self._settle(e, "speech ended before the check did" if score is not None and score >= needed else "")
        elif e.why or (score is not None and score >= needed):
            e.allowed = True
            self._log(e, True)

    def _match(self, frames: list[rtc.AudioFrame]) -> tuple[float | None, float, float, str]:
        """(score, needed, seconds, reference) for the latest MAX_SECONDS of speech. Runs in a thread."""
        audio = _to_whisper_audio(frames)[-int(self.max_seconds * RATE):]
        score, needed, reference = self.check.match(audio)
        return score, needed, round(len(audio) / RATE, 3), reference

    def _end(self) -> None:
        e, self._episode = self._episode, _Episode()
        e.ended = True
        if not e.busy:
            self._settle(e)

    def _settle(self, e: _Episode, why: str = "") -> None:
        """Log an ended, judged, never-allowed episode as ignored."""
        if e.checks and not e.allowed:
            e.why = why or e.why
            self._log(e, False)

    def _log(self, e: _Episode, interrupted: bool) -> None:
        verdict = "interrupted" if interrupted else "ignored"
        if self.check.mode != "on":
            verdict = "would interrupt" if interrupted else "would ignore"
        score, needed, seconds, reference = e.best or (None, 0.0, 0.0, "")
        found = Interruption(seconds, score, needed, verdict, e.checks,
                             round(self._clock() - (e.since or self._clock()), 3), e.why, reference)
        self._spawn(asyncio.to_thread(self.check.record_interruption, found))

    def _spawn(self, work) -> None:
        task = asyncio.ensure_future(work)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)


def _seconds(frame: rtc.AudioFrame) -> float:
    return frame.samples_per_channel / frame.sample_rate


def _margin(score: float | None, needed: float) -> float:
    """Score minus threshold; -inf without a score."""
    return float("-inf") if score is None else score - needed


class BargeInVAD(vad.VAD):
    """Wraps the session VAD, passing each event through `gate`."""

    def __init__(self, inner: vad.VAD, gate: BargeIn) -> None:
        super().__init__(capabilities=inner.capabilities)
        self._inner, self._gate = inner, gate

    @property
    def model(self) -> str:
        return self._inner.model

    @property
    def provider(self) -> str:
        return self._inner.provider

    def stream(self) -> "BargeInStream":
        return BargeInStream(self, self._inner.stream(), self._gate)


class BargeInStream(vad.VADStream):
    def __init__(self, vad_: BargeInVAD, inner: vad.VADStream, gate: BargeIn) -> None:
        self._inner, self._gate = inner, gate
        super().__init__(vad_)  # starts _main_task

    async def _main_task(self) -> None:
        forward = asyncio.create_task(self._forward())
        try:
            async for item in self._input_ch:
                if isinstance(item, self._FlushSentinel):
                    self._inner.flush()
                else:
                    self._inner.push_frame(item)
            self._inner.end_input()
            await forward
        finally:
            forward.cancel()
            self._gate.release(self)
            await self._inner.aclose()

    async def _forward(self) -> None:
        async for ev in self._inner:
            self._event_ch.send_nowait(self._gate.hear(self, ev))
