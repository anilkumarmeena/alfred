"""Tests for jarvis.voice.denoise: attenuation limit, frame sizes, fallback, VAD-only mode."""

import asyncio
import logging
import time

import numpy as np
import pytest
from livekit import rtc
from livekit.agents import vad

from jarvis.config import DENOISE_MODEL
from jarvis.voice import denoise

model = pytest.mark.skipif(not DENOISE_MODEL.is_file(), reason="needs the DeepFilterNet model (make models)")


class Silent:
    """Fake model that removes everything, as with pure noise and no limit."""

    def run(self, _outputs, feed):
        return np.zeros_like(feed["input_frame"]), feed["states"], np.zeros(1, np.float32)


def frames(x: np.ndarray, n: int):
    return [x[i:i + n] for i in range(0, len(x) - n + 1, n)]


def run(d: denoise.Denoiser, x: np.ndarray, rate: int, n: int) -> np.ndarray:
    return np.concatenate([d.process(f, rate) for f in frames(x, n)])


def noise(seconds: float, rate: int, level: float = 0.05, seed: int = 0) -> np.ndarray:
    return (np.random.default_rng(seed).standard_normal(int(seconds * rate)) * level * 32767).astype(np.int16)


def db(a: np.ndarray, b: np.ndarray) -> float:
    return 20 * np.log10(a.astype(np.float64).std() / b.astype(np.float64).std())


def test_attenuation_limit_mixes_the_original_back_in_step():
    x = noise(0.5, 48000)
    out = run(denoise.Denoiser(Silent(), atten_db=20), x, 48000, 2400)
    delayed = np.concatenate([np.zeros(denoise.MODEL_DELAY), x])[:len(out)]
    assert np.abs(out - np.round(delayed * 0.1)).max() <= 1  # -20 dB of the original, aligned to the output
    assert not run(denoise.Denoiser(Silent(), atten_db=0), x, 48000, 2400).any()  # 0 = no limit


@pytest.mark.parametrize("rate, n", [(48000, 2400), (24000, 1200), (16000, 333), (44100, 441)])
def test_each_frame_comes_back_as_long_as_it_went_in(rate, n):
    d = denoise.Denoiser(Silent(), atten_db=12)
    for f in frames(noise(1, rate), n):
        assert len(d.process(f, rate)) == n
    assert d.delay >= denoise.MODEL_DELAY * rate // denoise.RATE


def test_no_model_means_the_original_audio(tmp_path, caplog):
    junk = tmp_path / "junk.onnx"
    junk.write_bytes(b"not a model")
    with caplog.at_level(logging.WARNING, logger="jarvis.voice.denoise"):
        assert denoise.load(tmp_path / "missing.onnx", "on") is None
        assert denoise.load(junk, "vad") is None
    assert len(caplog.records) == 2 and all("original mic audio" in r.getMessage() for r in caplog.records)
    assert denoise.load(junk, "off") is None


def test_the_modes_put_the_filter_where_they_say():
    silero = Recorder()
    on, vad_only = denoise.room_options(Silent(), "on").audio_input, denoise.room_options(Silent(), "vad").audio_input
    assert isinstance(on.noise_cancellation(None), denoise.DeepFilterNet) and on.auto_gain_control is True
    assert vad_only.noise_cancellation is None and vad_only.auto_gain_control is True
    assert denoise.room_options(None, "on").audio_input.noise_cancellation is None  # the model didn't load
    assert isinstance(denoise.session_vad(silero, Silent(), "vad"), denoise.FilteredVAD)
    assert denoise.session_vad(silero, Silent(), "on") is silero
    assert denoise.session_vad(silero, None, "vad") is silero


def test_a_failing_filter_passes_the_original_through(caplog):
    class Broken:
        def run(self, *_):
            raise RuntimeError("boom")

    processor = denoise.DeepFilterNet(Broken())
    frame = rtc.AudioFrame(noise(0.05, 24000).tobytes(), 24000, 1, 1200)
    with caplog.at_level(logging.ERROR, logger="jarvis.voice.denoise"):
        assert processor._process(frame) is frame
    assert not processor.enabled  # so LiveKit stops calling it
    assert "original mic audio" in caplog.records[0].getMessage()


class Recorder(vad.VAD):
    """Fake Silero: every frame inferred; every third frame since a flush is an utterance."""

    def __init__(self) -> None:
        super().__init__(capabilities=vad.VADCapabilities(update_interval=0.05))
        self.heard: list[np.ndarray] = []

    def stream(self) -> vad.VADStream:
        return RecorderStream(self)


class RecorderStream(vad.VADStream):
    async def _main_task(self) -> None:
        got = []
        async for f in self._input_ch:
            if isinstance(f, self._FlushSentinel):
                got = []
                continue
            pcm = np.frombuffer(f.data, np.int16)
            self._vad.heard.append(pcm)
            got.append(pcm)
            self._event_ch.send_nowait(event(vad.VADEventType.INFERENCE_DONE, pcm, f.sample_rate))
            if len(got) % 3 == 0:
                self._event_ch.send_nowait(event(vad.VADEventType.END_OF_SPEECH, np.concatenate(got), f.sample_rate))


def event(kind, pcm: np.ndarray, rate: int) -> vad.VADEvent:
    return vad.VADEvent(type=kind, samples_index=0, timestamp=0, speech_duration=0, silence_duration=0,
                        frames=[rtc.AudioFrame(pcm.tobytes(), rate, 1, len(pcm))])


def test_vad_only_mode_hears_the_filter_but_hands_on_the_original():
    rate, n = 48000, 2400
    x = (np.arange(n * 9) % 20000).astype(np.int16)  # every sample different, so any misalignment shows

    async def main():
        inner = Recorder()
        stream = denoise.FilteredVAD(inner, Silent(), atten_db=0).stream()
        for i, f in enumerate(frames(x, n)):
            stream.push_frame(rtc.AudioFrame(f.tobytes(), rate, 1, n))
            if i == 3:
                stream.flush()  # the utterance restarts from the fifth frame
        stream.end_input()
        return inner, [ev async for ev in stream]

    inner, events = asyncio.run(main())
    assert not np.concatenate(inner.heard).any()  # Silero only heard the filter's output
    ends = [ev for ev in events if ev.type == vad.VADEventType.END_OF_SPEECH]
    assert len(ends) == 2
    original = np.concatenate([np.zeros(denoise.MODEL_DELAY, np.int16), x])  # the output lags by MODEL_DELAY
    for ev, (start, stop) in zip(ends, [(0, 3), (4, 7)]):
        assert np.array_equal(np.frombuffer(ev.frames[0].data, np.int16), original[start * n:stop * n])
    inferred = np.concatenate([np.frombuffer(ev.frames[0].data, np.int16) for ev in events
                               if ev.type == vad.VADEventType.INFERENCE_DONE])
    assert np.array_equal(inferred, original[:len(x)])


def test_vad_only_mode_falls_back_to_the_original_if_the_filter_fails(caplog):
    class Broken:
        def run(self, *_):
            raise RuntimeError("boom")

    x = (np.arange(2400 * 3) % 20000 + 1).astype(np.int16)

    async def main():
        inner = Recorder()
        stream = denoise.FilteredVAD(inner, Broken()).stream()
        for f in frames(x, 2400):
            stream.push_frame(rtc.AudioFrame(f.tobytes(), 48000, 1, 2400))
        stream.end_input()
        return inner, [ev async for ev in stream]

    with caplog.at_level(logging.ERROR, logger="jarvis.voice.denoise"):
        inner, events = asyncio.run(main())
    assert np.array_equal(np.concatenate(inner.heard), x)  # the VAD carried on with the original
    end = next(ev for ev in events if ev.type == vad.VADEventType.END_OF_SPEECH)
    assert np.array_equal(np.frombuffer(end.frames[0].data, np.int16), x)
    assert len(caplog.records) == 1


@model
def test_the_model_takes_noise_down_by_at_most_the_limit():
    session = denoise.load(DENOISE_MODEL, "on")
    x = noise(3, 24000)
    for limit in (12, 24):
        out = run(denoise.Denoiser(session, atten_db=limit), x, 24000, 1200)
        assert db(out[24000:], x[24000:]) == pytest.approx(-limit, abs=1.5)
    assert db(run(denoise.Denoiser(session, atten_db=0), x, 24000, 1200)[24000:], x[24000:]) < -30


@model
def test_the_model_keeps_voiced_sound_and_cuts_the_noise_between():
    rate = 24000
    t = np.arange(3 * rate) / rate
    on = np.sin(2 * np.pi * 1.5 * t) > 0.2  # bursts of a voiced sound, with gaps
    voice = sum(np.sin(2 * np.pi * f * t) / (i + 1) for i, f in enumerate([150, 300, 450, 600, 900, 1200])) * on * 0.2
    x = (voice * 32767).astype(np.int16) + noise(3, rate, 0.03)
    d = denoise.Denoiser(denoise.load(DENOISE_MODEL, "on"), atten_db=0)
    out = run(d, x, rate, 1200)[d.delay:]
    x, on = x[:len(out)], on[:len(out)]
    steady = np.arange(len(out)) > rate // 2
    before = db(x[on & steady], x[~on & steady])
    after = db(out[on & steady], out[~on & steady])
    assert after - before > 10  # the gaps got much quieter relative to the voice
    assert db(out[on & steady], x[on & steady]) > -6  # and the voice came through, even with no limit


@model
def test_the_output_lags_by_the_delay_it_reports():
    rate = 24000
    x = noise(2, rate, 0.1)
    d = denoise.Denoiser(denoise.load(DENOISE_MODEL, "on"), atten_db=6)  # half the original mixed back in: easy to find
    out = run(d, x, rate, 1200).astype(np.float64)
    lag = max(range(4000), key=lambda k: np.dot(x[:len(out) - 4000], out[k:k + len(out) - 4000]))
    assert abs(lag - d.delay) <= rate // 1000 * 2  # within 2 ms: enough to cut utterances from the original
    assert d.delay < rate * 0.05  # and at most 50 ms in all


@model
def test_the_frame_processor_keeps_up_in_real_time():
    processor = denoise.DeepFilterNet(denoise.load(DENOISE_MODEL, "on"))
    x = noise(2, 24000)
    took = []
    for f in frames(x, 1200):  # LiveKit's 50 ms frames at 24 kHz
        frame = rtc.AudioFrame(f.tobytes(), 24000, 1, 1200)
        start = time.perf_counter()
        out = processor._process(frame)
        took.append(time.perf_counter() - start)
        assert (out.sample_rate, out.samples_per_channel, out.num_channels) == (24000, 1200, 1)
    assert np.mean(took[5:]) < 0.025  # at most half of each 50 ms frame; about 7 ms on an M-series Mac
