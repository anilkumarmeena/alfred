"""Tests for jarvis.voice.barge_in: only the user's verified voice interrupts; other events pass through."""

import asyncio
import json

import numpy as np
import pytest
from livekit import rtc
from livekit.agents import vad

from jarvis import config
from jarvis.state.voiceprints import Voiceprint
from jarvis.voice import speaker
from jarvis.voice.barge_in import BargeIn, BargeInVAD
from jarvis.voice.speaker import RATE, SpeakerCheck, Thresholds

DIM = 8
USER = np.eye(DIM, dtype=np.float32)[0]
WINDOW = 512  # Silero's window: 32 ms at 16 kHz
ME, TV = 0.6, 0.1  # voiceprint similarity per voice (the fake embedder reads it off the audio)


class Embedder:
    """Scores audio by mean level: `ME` frames are the user, `TV` someone else, mixes in between."""
    available = True
    path = config.SPEAKER_MODEL

    def __init__(self, fail: bool = False) -> None:
        self.calls, self.fail = 0, fail

    def __call__(self, speech: np.ndarray) -> np.ndarray:
        self.calls += 1
        if self.fail:
            raise RuntimeError("model broke")
        if len(speech) < speaker.MIN_EMBED_SECONDS * RATE:
            raise ValueError("too little speech to embed")
        alike = float(np.clip(np.mean(speech), -1, 1))
        v = np.zeros(DIM, np.float32)
        v[0], v[2] = alike, np.sqrt(1 - alike ** 2)
        return v


def gate(mode: str = "on", tmp_path=None, prints: bool = True, fail: bool = False, **options) -> BargeIn:
    clock = lambda: 100.0
    check = SpeakerCheck(mode, Thresholds(threshold=0.35, adapt=True), Embedder(fail), lambda a: np.ones(1),
                         record=tmp_path / "speaker.jsonl" if tmp_path else None, clock=clock)
    check.prints = [Voiceprint(USER, 25.0, 0.8)] if prints else []
    g = BargeIn(check, **{"enabled": True, "min_seconds": 0.5, "max_seconds": 1.0, "step": 0.25, "threshold": 0.5,
                          "clock": clock, **options})
    g.recorded = []
    record = check.record_interruption
    check.record_interruption = lambda i: (g.recorded.append(i), record(i))
    return g


def window(level: float, n: int, speaking: bool = True, p: float = 0.9, rate: int = 48000) -> vad.VADEvent:
    """The n-th 32 ms window of an utterance at `level`; speech_duration counts from the first."""
    size = WINDOW * rate // RATE
    pcm = np.full(size, int(level * 32767), np.int16)
    return vad.VADEvent(type=vad.VADEventType.INFERENCE_DONE, samples_index=0, timestamp=0,
                        speech_duration=n * WINDOW / RATE if speaking else 0.0, silence_duration=0.0,
                        frames=[rtc.AudioFrame(pcm.tobytes(), rate, 1, size)], probability=p, speaking=speaking)


def end() -> vad.VADEvent:
    return vad.VADEvent(type=vad.VADEventType.END_OF_SPEECH, samples_index=0, timestamp=0, speech_duration=1.0,
                        silence_duration=0.5)


async def settle(g: BargeIn) -> None:
    while g._tasks:
        await asyncio.gather(*list(g._tasks))


async def speak(g: BargeIn, levels: list[float]) -> list[vad.VADEvent]:
    """Feed windows through the gate, finishing any started check before the next; return what LiveKit hears."""
    out = []
    for n, level in enumerate(levels, 1):
        out.append(g.hear("session", window(level, n)))
        await settle(g)
    return out


def interrupts(events: list[vad.VADEvent]) -> int | None:
    """Window at which LiveKit would interrupt (speech_duration reaches 0.5 s), or None."""
    return next((n for n, ev in enumerate(events, 1) if ev.speech_duration >= 0.5), None)


SECOND = int(RATE / WINDOW)  # 31 windows


def test_another_voice_never_interrupts_jarvis_and_is_logged(tmp_path):
    g = gate(tmp_path=tmp_path)
    g.replying("speaking")

    async def main():
        events = await speak(g, [TV] * 2 * SECOND)
        g.hear("session", end())
        await settle(g)
        return events

    events = asyncio.run(main())
    assert interrupts(events) is None  # LiveKit never heard enough speech to interrupt
    assert all(ev.speech_duration == 0 for ev in events)
    [ignored] = g.recorded
    assert ignored.verdict == "ignored" and ignored.score == pytest.approx(TV, abs=0.01) and ignored.needed == 0.25
    assert ignored.checks >= 6  # every 0.25 s of speech from 0.5 s until it ended
    logged = [json.loads(line) for line in (tmp_path / "speaker.jsonl").read_text().splitlines()]
    assert logged[0]["kind"] == "barge-in" and logged[0]["verdict"] == "ignored"


def test_the_users_voice_interrupts_as_soon_as_its_checked():
    g = gate()
    g.replying("speaking")
    events = asyncio.run(speak(g, [ME] * SECOND))
    first = interrupts(events)
    assert first == 17  # the window after 0.5 s of speech (16 windows), once its check passed
    assert all(ev.speech_duration == 0 for ev in events[:first - 1])
    [through] = g.recorded
    assert through.verdict == "interrupted" and through.checks == 1 and through.score == pytest.approx(ME, abs=0.01)
    assert through.speech_seconds == pytest.approx(0.512)


def test_the_check_goes_on_as_speech_continues_over_another_voice():
    g = gate()
    g.replying("speaking")
    events = asyncio.run(speak(g, [TV] * SECOND + [ME] * SECOND))  # the user talks over the TV
    assert SECOND < interrupts(events) < 2 * SECOND  # judged on the latest second, so the user soon outweighs the TV
    [through] = g.recorded
    assert through.verdict == "interrupted" and through.checks > 2


def test_the_judged_speech_is_the_latest_second_of_speech_windows_only():
    g = gate()
    g.replying("speaking")
    heard = []
    g.check.match = lambda audio: (heard.append(len(audio) / RATE), (0.0, 0.25, "owner"))[1]

    async def main():
        for n in range(1, 3 * SECOND):
            g.hear("session", window(ME, n, p=0.9 if n % 2 else 0.2))  # alternate windows are pauses between words
            await settle(g)

    asyncio.run(main())
    assert heard[0] == pytest.approx(0.288)  # only the speech windows within LiveKit's first 0.5 s of speech_duration
    assert max(heard) == pytest.approx(1.0, abs=0.04)


def test_while_jarvis_is_listening_nothing_is_held_or_judged():
    g = gate()
    g.replying("listening")
    sent = [window(TV, n) for n in range(1, SECOND)]
    assert [g.hear("session", ev) for ev in sent] == sent
    assert g.check._embed.calls == 0 and not g.recorded


def test_speech_that_started_before_jarvis_replied_is_judged_once_it_does():
    g = gate()

    async def main():
        out = await speak(g, [TV] * 20)
        g.replying("thinking")  # reply still being worked out: cutting it off now loses it too
        for n in range(21, 40):
            out.append(g.hear("session", window(TV, n)))
            await settle(g)
        return out

    events = asyncio.run(main())
    assert interrupts(events[:20]) == 16  # not replying yet: passed through
    assert interrupts(events[20:]) is None and g.check._embed.calls > 0


@pytest.mark.parametrize("mode, prints, enabled", [("off", True, True), ("on", False, True), ("on", True, False)])
def test_without_the_check_a_voiceprint_or_the_setting_any_voice_interrupts_as_before(mode, prints, enabled):
    g = gate(mode, prints=prints, enabled=enabled)
    g.replying("speaking")
    sent = [window(TV, n) for n in range(1, SECOND)] + [end()]
    assert all(g.hear("session", ev) is ev for ev in sent)  # the very same events
    assert g.check._embed.calls == 0 and not g.recorded


def test_in_shadow_mode_any_voice_interrupts_but_each_is_judged_and_logged(tmp_path):
    g = gate("shadow", tmp_path)
    g.replying("speaking")

    async def main():
        events = await speak(g, [TV] * SECOND)
        g.replying("listening")  # LiveKit interrupted JARVIS as usual
        events += await speak(g, [TV] * 10)
        g.hear("session", end())
        await settle(g)
        events += await speak(g, [ME] * SECOND)
        return events

    events = asyncio.run(main())
    assert interrupts(events) == 16  # untouched
    assert [i.verdict for i in g.recorded] == ["would ignore"]  # the user spoke while JARVIS wasn't replying
    g.replying("speaking")
    asyncio.run(speak(g, [ME] * SECOND))
    assert g.recorded[-1].verdict == "would interrupt"
    assert "barge-ins while JARVIS replied: would ignore 1, would interrupt 1" in speaker.summary(
        tmp_path / "speaker.jsonl")


def test_a_check_that_fails_lets_the_speech_interrupt(caplog):
    g = gate(fail=True)
    g.replying("speaking")
    events = asyncio.run(speak(g, [TV] * SECOND))
    assert interrupts(events) == 17
    assert g.recorded[0].verdict == "interrupted" and g.recorded[0].why == "check failed"
    assert "couldn't judge speech" in caplog.text


def test_a_sound_too_short_to_be_speech_is_forgotten():
    g = gate()
    g.replying("speaking")
    for n in range(1, 6):  # a click: speech windows that never started speech
        g.hear("session", window(TV, n, speaking=False))
    g.hear("session", window(0.0, 0, speaking=False, p=0.1))
    assert g._episode.heard == 0


def test_every_stream_follows_the_verdict_but_only_one_is_judged():
    g = gate()
    g.replying("speaking")

    async def main():
        a, b = [], []
        for n in range(1, SECOND):
            b.append(g.hear("stt adapter", window(ME, n)))  # LiveKit's STT adapter has its own VAD stream
            a.append(g.hear("session", window(ME, n)))
            await settle(g)
        return a, b

    a, b = asyncio.run(main())
    assert g.check._embed.calls == 1 and len(g.recorded) == 1
    assert interrupts(a) == interrupts(b) == 17  # judged once, on the first stream


def test_the_barge_in_check_needs_what_a_short_clip_needs_outside_a_conversation():
    g = gate()
    check = g.check
    check.jarvis_speaking(True)
    assert check.engaged()  # a reply would be let off lightly now; a barge-in isn't
    score, needed, reference = check.match(np.full(int(0.5 * RATE), 0.2, np.float32))
    assert (score, needed, reference) == (pytest.approx(0.2, abs=0.01), 0.25, speaker.OWNER)
    assert check.match(np.full(int(0.1 * RATE), 0.9, np.float32))[0] is None  # too short to embed: not let through
    assert check._user_at is None and check._other_at is None  # the whole utterance is judged later


class Inner(vad.VAD):
    """Fake Silero: one INFERENCE_DONE per pushed frame, speech from the first."""

    def __init__(self) -> None:
        super().__init__(capabilities=vad.VADCapabilities(update_interval=0.032))

    def stream(self) -> vad.VADStream:
        return InnerStream(self)


class InnerStream(vad.VADStream):
    async def _main_task(self) -> None:
        n = 0
        async for f in self._input_ch:
            if isinstance(f, self._FlushSentinel):
                continue
            n += 1
            level = np.frombuffer(f.data, np.int16)[0] / 32767
            self._event_ch.send_nowait(window(level, n, rate=f.sample_rate))


def test_the_sessions_vad_holds_back_another_voice_and_lets_the_user_through():
    async def main():
        g = gate()
        g.replying("speaking")
        stream = BargeInVAD(Inner(), g).stream()
        heard = []
        for level in [TV] * SECOND + [ME] * SECOND:
            stream.push_frame(rtc.AudioFrame(np.full(1536, int(level * 32767), np.int16).tobytes(), 48000, 1, 1536))
            heard.append(await stream.__anext__())
            await settle(g)
        stream.end_input()
        await stream.aclose()
        return g, heard

    g, heard = asyncio.run(main())
    assert SECOND < interrupts(heard) < 2 * SECOND
    assert g._owner is None  # stream released on close
