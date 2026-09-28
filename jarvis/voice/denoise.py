"""Noise suppression on the mic: DeepFilterNet 3, streaming, through onnxruntime on the CPU. Nothing leaves the Mac.

JARVIS_DENOISE picks where the filtered audio goes (config.DENOISE_MODES):
  on    the room's mic input goes through a LiveKit frame processor, so the VAD, barge-in and Whisper all hear it
  vad   only the VAD hears it (turn-taking, barge-in, where each utterance starts and ends); Whisper gets the
        original audio of the same stretch, to compare recognition with and without the filter
  off   the original audio everywhere
The model is a single-file streaming export of DeepFilterNet 3 (make models): 10 ms of 48 kHz audio per call, with
the network's state passed in and out. If it doesn't load, JARVIS logs a warning and uses the original audio.
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

RATE = 48000  # the model's only sample rate
HOP = 480  # samples per model call: 10 ms
STATE_SIZE = 45304  # the flattened network state the export passes in and out
MODEL_DELAY = 1440  # samples its output lags its input: the STFT's 480 plus two frames of lookahead
# The export's own attenuation limit, left off: its mix-in of the noisy input is a frame out of step (Denoiser has ours)
_FULL = np.array(0, np.float32)
HISTORY_SECONDS = 75  # original audio the VAD-only mode keeps: Silero's 60 s speech buffer plus its padding, and slack


def load(path: Path = DENOISE_MODEL, mode: str = DENOISE):
    """The model's onnxruntime session, or None when the filter is off or the model won't load (then raw audio)."""
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
        # A first run, with sound in it (the model skips silent hops), so the first real frame isn't slow
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
    """DeepFilterNet over one stream of mono int16 audio at any sample rate. Each call returns as many samples as it
    was given, `delay` samples behind: silence while the pipeline fills, then the filtered audio."""

    def __init__(self, session, atten_db: float = DENOISE_ATTEN_DB) -> None:
        self._session = session
        # How much of the original to mix back in, so no sound drops by more than atten_db (0 = no limit)
        self._keep = np.float32(10 ** (-atten_db / 20) if atten_db > 0 else 0)
        self._state = np.zeros(STATE_SIZE, np.float32)
        self._dry = np.zeros(MODEL_DELAY, np.float32)  # the input's last MODEL_DELAY samples, in step with the output
        self._rate = 0
        self.delay = 0  # samples, at the input's rate

    def _start(self, rate: int) -> None:
        """A new sample rate (the first frame, or pre-connect audio then the live track). The network keeps its state."""
        self._rate = rate
        # QUICK: the better qualities hand audio back in bursts of ~36 ms, and every burst late adds to the delay
        quick = rtc.AudioResamplerQuality.QUICK
        self._up = rtc.AudioResampler(rate, RATE, quality=quick) if rate != RATE else None
        self._down = rtc.AudioResampler(RATE, rate, quality=quick) if rate != RATE else None
        self._pending = np.zeros(0, np.float32)  # 48 kHz input short of a whole hop
        self._ready = np.zeros(0, np.int16)  # filtered output waiting to go out
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
        if len(ready) < n:  # still filling: pad, and the output falls that much further behind
            self.delay += n - len(ready)
            ready = np.concatenate([np.zeros(n - len(ready), np.int16), ready])
        out, self._ready = ready[:n], ready[n:]
        return out

    def _filter(self, x: np.ndarray) -> np.ndarray:
        """Whole hops of 48 kHz float audio, filtered, MODEL_DELAY behind, with the attenuation limit applied."""
        out = np.empty_like(x)
        for i in range(0, len(x), HOP):
            out[i:i + HOP], self._state = _run(self._session, x[i:i + HOP], self._state)
        if self._keep and len(x):
            dry = np.concatenate([self._dry, x])
            self._dry = dry[len(x):]
            out += self._keep * (dry[:len(x)] - out)  # in step with the output, unlike the export's own mix-in
        return out


def _frame(pcm: np.ndarray, rate: int) -> rtc.AudioFrame:
    return rtc.AudioFrame(pcm.tobytes(), rate, 1, len(pcm))


def _pcm(frames: list[rtc.AudioFrame]) -> np.ndarray:
    return np.concatenate([np.frombuffer(f.data, np.int16) for f in frames]) if frames else np.zeros(0, np.int16)


class DeepFilterNet(rtc.FrameProcessor[rtc.AudioFrame]):
    """LiveKit's hook on the room's mic input: every frame goes through here before the VAD and Whisper see it.
    It runs on the event loop, so it must keep up: about 8 ms of CPU per 50 ms frame on the Mac."""

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
        if frame.num_channels != 1:  # the room input is mono; anything else passes through
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
    """The session's room options: the mic through DeepFilterNet in "on" mode, as it comes otherwise.
    Automatic gain control stays on as it is without a filter, so the modes differ only by the filter."""
    audio = room_io.AudioInputOptions(auto_gain_control=True)
    if session is not None and mode == "on":
        audio.noise_cancellation = lambda _params: DeepFilterNet(session)  # one per mic track, each with its own state
    return room_io.RoomOptions(audio_input=audio)


def session_vad(silero: vad.VAD, session, mode: str = DENOISE) -> vad.VAD:
    """The session's VAD: in "vad" mode, one that hears the filtered mic and hands on the original."""
    return FilteredVAD(silero, session) if session is not None and mode == "vad" else silero


class FilteredVAD(vad.VAD):
    """Silero decides on denoised audio, but the speech in its events is the original audio. The session's own VAD
    stream drives turn-taking and barge-in; the STT adapter's cuts utterances for Whisper out of those events."""

    def __init__(self, inner: vad.VAD, session, atten_db: float = DENOISE_ATTEN_DB) -> None:
        super().__init__(capabilities=inner.capabilities)
        self._inner, self._session, self._atten_db = inner, session, atten_db

    @property
    def model(self) -> str:
        return self._inner.model

    @property
    def provider(self) -> str:
        return self._inner.provider

    @property
    def min_silence_duration(self) -> float | None:  # the session checks it against the turn detector
        return getattr(self._inner, "min_silence_duration", None)

    def stream(self) -> "FilteredVADStream":
        return FilteredVADStream(self, self._inner, Denoiser(self._session, self._atten_db))


class _History:
    """The last `size` samples of original audio, looked up by absolute sample index (a ring buffer)."""

    def __init__(self, size: int) -> None:
        self._ring = np.zeros(size, np.int16)
        self.end = 0  # samples added so far

    def add(self, pcm: np.ndarray) -> None:
        size = len(self._ring)
        at = (self.end + np.arange(max(0, len(pcm) - size), len(pcm))) % size
        self._ring[at] = pcm[-size:]
        self.end += len(pcm)

    def span(self, start: int, end: int) -> np.ndarray:
        """Samples start..end; silence for any part not kept (before the stream began, or too long ago)."""
        out = np.zeros(end - start, np.int16)
        lo, hi = max(start, self.end - len(self._ring), 0), min(end, self.end)
        if hi > lo:
            out[lo - start:hi - start] = self._ring[np.arange(lo, hi) % len(self._ring)]
        return out


@dataclasses.dataclass
class _Segment:
    """One inner Silero stream, from the stream's start or a flush to the next; `origin` is where it began."""
    stream: vad.VADStream
    origin: int
    consumed: int = 0  # samples Silero has run inference over, reported by its inference_done events


class FilteredVADStream(vad.VADStream):
    def __init__(self, vad_: FilteredVAD, inner: vad.VAD, denoiser: Denoiser) -> None:
        self._inner, self._denoiser = inner, denoiser
        self._history: _History | None = None  # sized by the first frame's rate (the room input's never changes)
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
                    # Silero drops what it holds on a flush; a fresh stream per stretch keeps the sample count exact
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
        """Pass each inner stream's events on in order, with their audio swapped for the original."""
        while (segment := await segments.get()) is not None:
            async for ev in segment.stream:
                n = sum(f.samples_per_channel for f in ev.frames)
                if ev.type == vad.VADEventType.INFERENCE_DONE:
                    segment.consumed += n
                # The frames end where Silero has read up to; the original for that lies `delay` further back
                end = segment.origin + segment.consumed - self._denoiser.delay
                frames = [_frame(self._history.span(end - n, end), self._rate)] if n else []
                self._event_ch.send_nowait(dataclasses.replace(ev, frames=frames))
