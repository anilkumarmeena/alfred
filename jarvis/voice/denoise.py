"""Mic noise suppression: streaming DeepFilterNet 3 via onnxruntime on CPU, fully local.

JARVIS_DENOISE modes:
  on    filter the room mic input (VAD, barge-in and Whisper all hear it)
  vad   only the VAD hears filtered audio; Whisper gets the original, to compare recognition
  off   original audio everywhere
Model: single-file streaming export (make models), 10 ms of 48 kHz per call, state passed in and out.
Load failure logs a warning and falls back to original audio.
"""

import asyncio
import dataclasses
import logging
import time
from pathlib import Path

import numpy as np
from livekit import rtc
from livekit.agents import vad
from livekit.agents.voice import room_io

from jarvis.core.config import DENOISE, DENOISE_ATTEN_DB, DENOISE_MODEL

log = logging.getLogger("jarvis.voice.denoise")

RATE = 48000  # model's only sample rate
HOP = 480  # samples per call (10 ms)
STATE_SIZE = 45304  # flattened network state size
MODEL_DELAY = 1440  # output lag in samples: STFT 480 + two lookahead frames
# Export's own attenuation limit disabled: its dry mix is a frame out of step (Denoiser does it instead)
_FULL = np.array(0, np.float32)
HISTORY_SECONDS = 75  # vad mode history: Silero's 60 s buffer + padding + slack


def load(path: Path = DENOISE_MODEL, mode: str = DENOISE):
    """onnxruntime session, or None if off or unloadable (raw audio is used)."""
    if mode == "off":
        return None
    if not path.is_file():
        log.warning("noise filter off, using the original mic audio: no model at %s (make models)", path)
        return None
    try:
        import onnxruntime as ort

        options = ort.SessionOptions()
        options.intra_op_num_threads = options.inter_op_num_threads = 1  # more threads don't help a 10 ms hop
        options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        options.log_severity_level = 3
        session = ort.InferenceSession(str(path), options, providers=["CPUExecutionProvider"])
        if [i.name for i in session.get_inputs()] != ["input_frame", "states", "atten_lim_db"]:
            raise ValueError("not the streaming DeepFilterNet 3 export")
        # Warm up with noise (silent hops are skipped) so the first real frame isn't slow
        sound = np.random.default_rng(0).standard_normal(HOP).astype(np.float32) * 0.1
        _run(session, sound, np.zeros(STATE_SIZE, np.float32))
    except Exception as e:
        log.warning("noise filter off, using the original mic audio: couldn't load %s (%s)", path, e)
        return None
    log.info("noise filter: DeepFilterNet 3, %s mode, %s", mode,
             f"at most {DENOISE_ATTEN_DB:g} dB off" if DENOISE_ATTEN_DB else "no attenuation limit")
    return session


def _run(session, hop: np.ndarray, state: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    out, state, _lsnr = session.run(None, {"input_frame": hop, "states": state, "atten_lim_db": _FULL})
    return out, state


class Denoiser:
    """DeepFilterNet over one mono int16 stream at any rate. Each call returns len(input) samples, `delay` behind
    (silence while filling)."""

    def __init__(self, session, atten_db: float = DENOISE_ATTEN_DB) -> None:
        self._session = session
        # Dry mix so nothing drops more than atten_db (0 = no limit)
        self._keep = np.float32(10 ** (-atten_db / 20) if atten_db > 0 else 0)
        self._state = np.zeros(STATE_SIZE, np.float32)
        self._dry = np.zeros(MODEL_DELAY, np.float32)  # last MODEL_DELAY input samples, aligned with output
        self._rate = 0
        self.delay = 0  # samples at the input rate

    def _start(self, rate: int) -> None:
        """Reset resamplers for a new input rate (e.g. pre-connect audio then live track). Network state is kept."""
        self._rate = rate
        # QUICK: higher qualities emit ~36 ms bursts, adding latency
        quick = rtc.AudioResamplerQuality.QUICK
        self._up = rtc.AudioResampler(rate, RATE, quality=quick) if rate != RATE else None
        self._down = rtc.AudioResampler(RATE, rate, quality=quick) if rate != RATE else None
        self._pending = np.zeros(0, np.float32)  # 48 kHz remainder short of a hop
        self._ready = np.zeros(0, np.int16)  # filtered output not yet returned
        self.delay = round(MODEL_DELAY * rate / RATE)

    def process(self, pcm: np.ndarray, rate: int) -> np.ndarray:
        if rate != self._rate:
            self._start(rate)
        n = len(pcm)
        x = _pcm(self._up.push(_frame(pcm, rate))) if self._up else pcm
        x = np.concatenate([self._pending, x.astype(np.float32) / 32768])
        whole = len(x) - len(x) % HOP
        self._pending = x[whole:]
        y = (np.clip(self._filter(x[:whole]), -1, 1) * 32767).astype(np.int16)
        if self._down and len(y):
            y = _pcm(self._down.push(_frame(y, RATE)))
        ready = np.concatenate([self._ready, y])
        if len(ready) < n:  # still filling: pad and grow the delay
            self.delay += n - len(ready)
            ready = np.concatenate([np.zeros(n - len(ready), np.int16), ready])
        out, self._ready = ready[:n], ready[n:]
        return out

    def _filter(self, x: np.ndarray) -> np.ndarray:
        """Filter whole 48 kHz float hops (MODEL_DELAY behind), applying the attenuation limit."""
        out = np.empty_like(x)
        for i in range(0, len(x), HOP):
            out[i:i + HOP], self._state = _run(self._session, x[i:i + HOP], self._state)
        if self._keep and len(x):
            dry = np.concatenate([self._dry, x])
            self._dry = dry[len(x):]
            out += self._keep * (dry[:len(x)] - out)  # aligned, unlike the export's mix
        return out


def _frame(pcm: np.ndarray, rate: int) -> rtc.AudioFrame:
    return rtc.AudioFrame(pcm.tobytes(), rate, 1, len(pcm))


def _pcm(frames: list[rtc.AudioFrame]) -> np.ndarray:
    return np.concatenate([np.frombuffer(f.data, np.int16) for f in frames]) if frames else np.zeros(0, np.int16)


class DeepFilterNet(rtc.FrameProcessor[rtc.AudioFrame]):
    """LiveKit frame processor on the room mic input, before VAD and Whisper.

    Runs on the event loop, so must stay real-time: ~8 ms CPU per 50 ms frame on an M-series Mac."""

    def __init__(self, session, atten_db: float = DENOISE_ATTEN_DB) -> None:
        self._denoiser = Denoiser(session, atten_db)
        self._enabled = True
        self._frames, self._busy, self._worst, self._audio = 0, 0.0, 0.0, 0.0
        self._slow_warned = 0.0

    @property
    def enabled(self) -> bool:
        return self._enabled

    @enabled.setter
    def enabled(self, value: bool) -> None:
        self._enabled = value

    def _process(self, frame: rtc.AudioFrame) -> rtc.AudioFrame:
        if frame.num_channels != 1:  # room input is mono; pass anything else through
            return frame
        start = time.perf_counter()
        try:
            pcm = self._denoiser.process(np.frombuffer(frame.data, np.int16), frame.sample_rate)
        except Exception:
            log.exception("noise filter failed; the original mic audio goes through from here on")
            self._enabled = False
            return frame
        took = time.perf_counter() - start
        self._frames += 1
        self._busy += took
        self._audio += frame.duration
        self._worst = max(self._worst, took)
        if took > frame.duration and start - self._slow_warned > 60:
            self._slow_warned = start
            log.warning("noise filter slower than real time: %.1f ms for a %.0f ms frame",
                        took * 1000, frame.duration * 1000)
        return _frame(pcm, frame.sample_rate)

    def _close(self) -> None:
        if self._frames:
            log.info("noise filter: %d frames, %.1f ms each on average (worst %.1f), %.0f%% of real time",
                     self._frames, self._busy / self._frames * 1000, self._worst * 1000, self._busy / self._audio * 100)


def room_options(session, mode: str = DENOISE) -> room_io.RoomOptions:
    """Room options: mic through DeepFilterNet in "on" mode. AGC stays on in all modes so only the filter differs."""
    audio = room_io.AudioInputOptions(auto_gain_control=True)
    if session is not None and mode == "on":
        audio.noise_cancellation = lambda _params: DeepFilterNet(session)  # one per mic track, own state
    return room_io.RoomOptions(audio_input=audio)


def session_vad(silero: vad.VAD, session, mode: str = DENOISE) -> vad.VAD:
    """Session VAD; in "vad" mode, decides on filtered audio but emits the original."""
    return FilteredVAD(silero, session) if session is not None and mode == "vad" else silero


class FilteredVAD(vad.VAD):
    """Silero decides on denoised audio; events carry the original audio (which the STT adapter cuts for Whisper)."""

    def __init__(self, inner: vad.VAD, session, atten_db: float = DENOISE_ATTEN_DB) -> None:
        super().__init__(capabilities=inner.capabilities)
        self._inner, self._session, self._atten_db = inner, session, atten_db

    @property
    def model(self) -> str:
        return self._inner.model

    @property
    def provider(self) -> str:
        return self._inner.provider

    def stream(self) -> "FilteredVADStream":
        return FilteredVADStream(self, self._inner, Denoiser(self._session, self._atten_db))


class _History:
    """Ring buffer of the last `size` original samples, indexed by absolute sample number."""

    def __init__(self, size: int) -> None:
        self._ring = np.zeros(size, np.int16)
        self.end = 0  # total samples added

    def add(self, pcm: np.ndarray) -> None:
        size = len(self._ring)
        at = (self.end + np.arange(max(0, len(pcm) - size), len(pcm))) % size
        self._ring[at] = pcm[-size:]
        self.end += len(pcm)

    def span(self, start: int, end: int) -> np.ndarray:
        """Samples [start, end); zeros where not retained."""
        out = np.zeros(end - start, np.int16)
        lo, hi = max(start, self.end - len(self._ring), 0), min(end, self.end)
        if hi > lo:
            out[lo - start:hi - start] = self._ring[np.arange(lo, hi) % len(self._ring)]
        return out


@dataclasses.dataclass
class _Segment:
    """One inner Silero stream between flushes; `origin` is its first absolute sample."""
    stream: vad.VADStream
    origin: int
    consumed: int = 0  # samples inferred (from inference_done)


class FilteredVADStream(vad.VADStream):
    def __init__(self, vad_: FilteredVAD, inner: vad.VAD, denoiser: Denoiser) -> None:
        self._inner, self._denoiser = inner, denoiser
        self._history: _History | None = None  # sized from the first frame's rate
        self._rate = 0
        self._failed = False
        super().__init__(vad_)  # starts _main_task

    async def _main_task(self) -> None:
        segments: asyncio.Queue[_Segment | None] = asyncio.Queue()
        forward = asyncio.create_task(self._forward(segments))
        segment = _Segment(self._inner.stream(), 0)
        segments.put_nowait(segment)
        opened = [segment]
        try:
            async for item in self._input_ch:
                if isinstance(item, self._FlushSentinel):
                    # Silero drops buffered audio on flush; a fresh stream keeps sample counts exact
                    segment.stream.end_input()
                    segment = _Segment(self._inner.stream(), self._history.end if self._history else 0)
                    segments.put_nowait(segment)
                    opened.append(segment)
                    continue
                pcm = np.frombuffer(item.data, np.int16)
                if self._history is None:
                    self._rate = item.sample_rate
                    self._history = _History(HISTORY_SECONDS * self._rate)
                clean = pcm
                if not self._failed:
                    try:
                        clean = await asyncio.to_thread(self._denoiser.process, pcm, item.sample_rate)
                    except Exception:
                        log.exception("noise filter failed; the VAD hears the original mic audio from here on")
                        self._failed, self._denoiser.delay = True, 0
                self._history.add(pcm)
                segment.stream.push_frame(_frame(clean, item.sample_rate))
            segment.stream.end_input()
            segments.put_nowait(None)
            await forward
        finally:
            forward.cancel()
            for s in opened:
                await s.stream.aclose()

    async def _forward(self, segments: "asyncio.Queue[_Segment | None]") -> None:
        """Forward inner events in order, with frames replaced by original audio."""
        while (segment := await segments.get()) is not None:
            async for ev in segment.stream:
                n = sum(f.samples_per_channel for f in ev.frames)
                if ev.type == vad.VADEventType.INFERENCE_DONE:
                    segment.consumed += n
                # Frames end at Silero's read position; the matching original is `delay` earlier
                end = segment.origin + segment.consumed - self._denoiser.delay
                frames = [_frame(self._history.span(end - n, end), self._rate)] if n else []
                self._event_ch.send_nowait(dataclasses.replace(ev, frames=frames))
