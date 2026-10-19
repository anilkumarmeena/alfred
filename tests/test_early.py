"""Tests for alfred.voice.early: Whisper starts on an utterance at a short pause and its clip uses those words."""

import asyncio
import json

import numpy as np
from livekit import rtc
from livekit.agents import stt as lk_stt, vad
from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS

from alfred.voice import early, speech
from alfred.voice.speech_gate import SpeechGate

RATE = 48000
WINDOW = 1536  # Silero's 32 ms window at 48 kHz


def sound(n: int, seed: int) -> np.ndarray:
    return np.random.default_rng(seed).integers(-8000, 8000, n).astype(np.int16)


def frame(samples: np.ndarray) -> rtc.AudioFrame:
    return rtc.AudioFrame(samples.tobytes(), RATE, 1, len(samples))


def event(kind: vad.VADEventType, samples: np.ndarray, silence: float = 0.0, speaking: bool = True) -> vad.VADEvent:
    return vad.VADEvent(type=kind, samples_index=0, timestamp=0.0, speech_duration=0.0, silence_duration=silence,
                        frames=[frame(samples)], speaking=speaking, raw_accumulated_silence=silence)


class FakeWhisper:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def hear_early(self, onset, buffer) -> None:
        self.calls.append(("hear", onset, np.concatenate([np.frombuffer(f.data, np.int16) for f in buffer])))

    def drop_early(self, onset) -> None:
        self.calls.append(("drop", onset))


def stream(whisper, after: float = 0.25) -> early.EarlyStream:
    """An EarlyStream fed by hand (see), its own task stopped."""
    s = early.EarlyStream.__new__(early.EarlyStream)
    s._inner, s._whisper, s._after = None, whisper, after
    s._frames, s._onset, s._silence, s._early = None, (0, b""), 0.0, False
    return s


def utterance(silences: list[float]) -> tuple[np.ndarray, list[vad.VADEvent]]:
    """A start of speech, then one window per silence reading; returns all its audio and the events."""
    start = sound(RATE // 2, 1)
    windows = [sound(WINDOW, 10 + i) for i in range(len(silences))]
    events = [event(vad.VADEventType.START_OF_SPEECH, start)]
    events += [event(vad.VADEventType.INFERENCE_DONE, w, s) for w, s in zip(windows, silences)]
    return np.concatenate([start, *windows]), events


def test_whisper_starts_at_the_pause_on_everything_said_so_far():
    whisper = FakeWhisper()
    s = stream(whisper)
    audio, events = utterance([0.0, 0.064, 0.16, 0.256, 0.288])
    for ev in events:
        s.see(ev)
    [(kind, onset, heard)] = whisper.calls
    assert kind == "hear" and np.array_equal(heard, audio[:RATE // 2 + 4 * WINDOW])  # through the window at 0.256
    offset, onset_bytes = onset
    assert audio.tobytes()[offset:offset + len(onset_bytes)] == onset_bytes  # where the speech started


def test_speech_going_on_drops_it_and_the_next_pause_starts_again():
    whisper = FakeWhisper()
    s = stream(whisper)
    _, events = utterance([0.0, 0.256, 0.0, 0.1, 0.3])
    for ev in events:
        s.see(ev)
    assert [c[0] for c in whisper.calls] == ["hear", "drop", "hear"]
    assert len(whisper.calls[2][2]) > len(whisper.calls[0][2])


def test_nothing_starts_outside_speech_or_before_the_pause():
    whisper = FakeWhisper()
    s = stream(whisper)
    s.see(event(vad.VADEventType.INFERENCE_DONE, sound(WINDOW, 2), 1.0, speaking=False))  # silence, no utterance
    _, events = utterance([0.0, 0.1, 0.2])
    for ev in events:
        s.see(ev)
    s.see(event(vad.VADEventType.END_OF_SPEECH, sound(WINDOW, 3), 0.55, speaking=False))
    s.see(event(vad.VADEventType.INFERENCE_DONE, sound(WINDOW, 4), 0.6, speaking=False))
    assert whisper.calls == []


def test_off_or_not_before_the_vads_own_end_it_is_whisper_alone():
    whisper, inner = FakeWhisper(), object()
    assert early.adapter(whisper, inner, 0.0) is whisper
    assert early.adapter(whisper, inner, early.VAD_MIN_SILENCE) is whisper
    adapted = early.adapter(speech.WhisperSTT(gate=SpeechGate("off", record=None, audio_dir=None)), NoVAD(), 0.25)
    assert isinstance(adapted, lk_stt.StreamAdapter)


class NoVAD(vad.VAD):
    def __init__(self) -> None:
        super().__init__(capabilities=vad.VADCapabilities(update_interval=0.032))

    def stream(self):
        raise NotImplementedError


# ── WhisperSTT takes the early words ────────────────────────────


class CountingExecutor:
    def __init__(self) -> None:
        self.calls = 0

    async def do_inference(self, method: str, data: bytes) -> bytes:
        self.calls += 1
        audio, _ = speech.read_whisper_request(data)
        return json.dumps({"text": f"{len(audio)} samples.", "no_speech": 0.0, "avg_logprob": -0.2}).encode()


def whisper_stt() -> tuple[speech.WhisperSTT, CountingExecutor]:
    executor = CountingExecutor()
    return speech.WhisperSTT(gate=SpeechGate("off", record=None, audio_dir=None), executor=executor), executor


def early_then_clip(clip_audio: np.ndarray, early_audio: np.ndarray, *, dropped: bool = False):
    stt, executor = whisper_stt()
    cut = RATE - 9600  # in bytes: 0.1 s of 16-bit audio ending half a second in, where the speech started
    onset = (cut, early_audio.tobytes()[cut:cut + 9600])
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        async def go():
            stt.hear_early(onset, [frame(early_audio)])
            await asyncio.sleep(0.05)
            if dropped:
                stt.drop_early(onset)
            return await stt._recognize_impl([frame(clip_audio)], conn_options=DEFAULT_API_CONNECT_OPTIONS)
        text = loop.run_until_complete(go()).alternatives[0].text
    finally:
        loop.close()
    return stt, executor, text


def test_a_clip_uses_the_words_heard_early_on_its_utterance():
    clip_audio = np.concatenate([sound(RATE, 5), np.zeros(RATE // 4, np.int16)])  # then the rest of the silence
    early_audio = clip_audio[:RATE + RATE // 10]
    stt, executor, text = early_then_clip(clip_audio, early_audio)
    assert executor.calls == 1 and text == f"{len(early_audio) // 3} samples."  # 48 -> 16 kHz
    assert stt.take_clips()[0]["early"] is True


def test_speech_that_went_on_is_heard_fresh():
    clip_audio = np.concatenate([sound(RATE, 5), sound(RATE // 2, 6)])
    stt, executor, text = early_then_clip(clip_audio, clip_audio[:RATE], dropped=True)
    assert executor.calls == 2 and text == f"{len(clip_audio) // 3} samples."
    assert stt.take_clips()[0]["early"] is False


def test_another_utterances_early_words_are_never_used():
    _, executor, text = early_then_clip(sound(RATE * 2, 7), sound(RATE, 8))
    assert executor.calls == 2 and text == f"{RATE * 2 // 3} samples."
