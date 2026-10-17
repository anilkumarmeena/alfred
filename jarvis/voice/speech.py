"""On-device speech for LiveKit: Whisper in, Kokoro out. Nothing leaves the computer.

Models load once in LiveKit's shared inference process (runners registered here); job processes send it audio/text
over IPC, so concurrent calls share one Whisper and one Kokoro. Requests are serialized, and each runner locks too.
If that process is down or a model failed to load, STT/TTS raise APIConnectionError instead of hanging.

Whisper runs on MLX (Apple GPU) on Apple silicon and on faster-whisper elsewhere (jarvis.media.whisper). It is
prompted with the user's vocabulary, and known mishearings are fixed after the speech gate. Memory is kept lean: MLX's
buffer cache is capped (JARVIS_MLX_CACHE_MB) and Kokoro's ORT arena is off by default.
"""

import asyncio
import ctypes
import json
import logging
import os
import threading
import time
from pathlib import Path

import numpy as np
from livekit import rtc
from livekit.agents import APIConnectionError, APIConnectOptions, get_job_context, stt, tts, utils
from livekit.agents.inference_runner import _InferenceRunner
from livekit.agents.ipc.inference_executor import InferenceExecutor
from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS, NOT_GIVEN, NotGivenOr

from jarvis.config import (
    MLX_CACHE_MB,
    STT_MODEL,
    TTS_MEMORY_ARENA,
    TTS_MODEL,
    TTS_MODEL_FALLBACKS,
    TTS_THREADS,
    TTS_VOICES_FILE,
)
from jarvis.media import whisper
from jarvis.state import preferences
from jarvis.state.vocabulary import Vocabulary
from jarvis.voice.clauses import ClauseTokenizer
from jarvis.voice.speaker import SpeakerCheck
from jarvis.voice.speech_gate import SpeechGate

log = logging.getLogger("jarvis.voice.speech")

WHISPER_RATE = 16000
KOKORO_RATE = 24000
WHISPER_METHOD = "jarvis_whisper"
KOKORO_METHOD = "jarvis_kokoro"
INFERENCE_TIMEOUT = 30  # seconds; only a stuck process hits it


# ── Shared process: one runner per model ───────────────────────


def transcribe(audio: np.ndarray, model: str = STT_MODEL, prompt: str = "") -> dict:
    """{text, no_speech (min over segments), avg_logprob (token-weighted)}; numbers None without segments.

    condition_on_previous_text is off so one phantom can't seed the next; `prompt` is the only context."""
    result = whisper.run(audio, model, language="en", condition_on_previous_text=False, initial_prompt=prompt or None)
    segments = result.get("segments", [])
    no_speech = [s["no_speech_prob"] for s in segments if "no_speech_prob" in s]
    scored = [(s["avg_logprob"], max(len(s.get("tokens", [])), 1)) for s in segments if "avg_logprob" in s]
    tokens = sum(n for _, n in scored)
    return {"text": result["text"].strip(), "no_speech": min(no_speech, default=None),
            "avg_logprob": sum(lp * n for lp, n in scored) / tokens if scored else None}


def limit_mlx_cache(mb: int = MLX_CACHE_MB) -> None:
    """Cap MLX's freed-buffer cache. Uncapped it stays at the largest clip's size (~0.7 GB for large-v3-turbo).
    -1 leaves MLX's default."""
    if mb < 0:
        return
    try:
        import mlx.core as mx  # pyright: ignore[reportMissingImports]  # Apple silicon only

        mx.set_cache_limit(mb * 2**20)
    except Exception as e:  # Whisper still runs, uncapped
        log.warning("couldn't cap MLX's cache at %d MB: %r", mb, e)


def mlx_memory() -> str:
    """MLX active and cached memory, for logs."""
    import mlx.core as mx  # pyright: ignore[reportMissingImports]

    return f"MLX {mx.get_active_memory() / 2**20:.0f} MB in use, {mx.get_cache_memory() / 2**20:.0f} MB cached"


def whisper_request(audio: np.ndarray, prompt: str = "") -> bytes:
    """Encode as: 4-byte LE prompt length, UTF-8 prompt, float32 samples."""
    said = prompt.encode()
    return len(said).to_bytes(4, "little") + said + audio.astype(np.float32).tobytes()


def read_whisper_request(data: bytes) -> tuple[np.ndarray, str]:
    n = int.from_bytes(data[:4], "little")
    return np.frombuffer(data[4 + n:], dtype=np.float32), data[4:4 + n].decode(errors="replace")


class WhisperRunner(_InferenceRunner):
    """Whisper runner. In: whisper_request bytes. Out: JSON {text, no_speech, avg_logprob}."""
    INFERENCE_METHOD = WHISPER_METHOD

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._ready = False

    def initialize(self) -> None:
        mlx = whisper.backend() == whisper.MLX
        if mlx:
            limit_mlx_cache()
        transcribe(np.zeros(WHISPER_RATE, dtype=np.float32))  # load (and on MLX compile kernels, ~3 s)
        self._ready = True
        if mlx:
            log.info("shared speech process: Whisper %s loaded, MLX cache capped at %s", STT_MODEL,
                     f"{MLX_CACHE_MB} MB" if MLX_CACHE_MB >= 0 else "nothing (JARVIS_MLX_CACHE_MB=-1)")
        else:
            log.info("shared speech process: Whisper %s loaded on faster-whisper (%s)", whisper.model(),
                     whisper.device())

    def run(self, data: bytes) -> bytes:
        if not self._ready:  # LiveKit swallows initialize failures
            raise RuntimeError("Whisper didn't load in the shared speech process; see logs/agent.log")
        audio, prompt = read_whisper_request(data)
        with self._lock:
            heard = transcribe(audio, prompt=prompt)
            if log.isEnabledFor(logging.DEBUG):
                log.debug("Whisper heard a %.1f s clip%s", len(audio) / WHISPER_RATE,
                          f"; {mlx_memory()}" if whisper.backend() == whisper.MLX else "")
        return json.dumps(heard).encode()


def kokoro_model(chosen: Path = TTS_MODEL, fallbacks: tuple[Path, ...] = TTS_MODEL_FALLBACKS) -> Path:
    """`chosen` if present, else the first existing fallback (fp32, then int8)."""
    if chosen.exists():
        return chosen
    fallback = next((f for f in fallbacks if f.exists()), fallbacks[0])
    log.warning("Kokoro model %s is missing; using %s", chosen, fallback.name)
    return fallback


def performance_cores() -> int:
    """Performance-core count (physical cores on Intel), else cpu_count."""
    try:
        libc = ctypes.CDLL(None)
        for name in (b"hw.perflevel0.physicalcpu", b"hw.physicalcpu"):
            value, size = ctypes.c_int(0), ctypes.c_size_t(ctypes.sizeof(ctypes.c_int))
            if libc.sysctlbyname(name, ctypes.byref(value), ctypes.byref(size), None, 0) == 0 and value.value > 0:
                return value.value
    except (OSError, AttributeError):  # not macOS
        pass
    return os.cpu_count() or 4


def kokoro_threads(setting: str = TTS_THREADS, cores: int | None = None) -> int:
    """Kokoro intra-op threads: P-cores minus one, clamped 2..4, unless set (0 = ORT default, all cores).
    More threads didn't render faster under load; they only starved the call's process."""
    if setting:
        try:
            return max(0, int(setting))
        except ValueError:
            log.warning("JARVIS_TTS_THREADS=%r isn't a number; choosing the threads myself", setting)
    return min(4, max(2, (cores or performance_cores()) - 1))


def kokoro_session(model: Path, threads: int, arena: bool = TTS_MEMORY_ARENA):
    """Kokoro ORT session. Arena off frees buffers per sentence instead of growing to the longest one.
    Spinning is disabled so idle threads sleep between sentences."""
    import onnxruntime as ort
    from kokoro_onnx.session import resolve_providers

    options = ort.SessionOptions()
    options.enable_cpu_mem_arena = arena
    options.intra_op_num_threads = threads
    options.add_session_config_entry("session.intra_op.allow_spinning", "0")
    return ort.InferenceSession(str(model), options, providers=resolve_providers())


def log_render(words: int, took: float, speech: float) -> None:
    """Log render time vs. audio length. Over 1x real time leaves audible gaps, so it's a warning."""
    ratio = took / speech if speech else 0.0
    slow = ratio > 1
    log.log(logging.WARNING if slow else logging.INFO,
            "Kokoro rendered %d words in %.0f ms for %.1f s of speech (%.2fx real time)%s", words, took * 1000,
            speech, ratio, ", slower than speaking it, so it can leave a pause" if slow else "")


class KokoroRunner(_InferenceRunner):
    """Kokoro runner. In: JSON {text, voice, speed}. Out: 24 kHz mono int16 PCM."""
    INFERENCE_METHOD = KOKORO_METHOD

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._kokoro = None

    def initialize(self) -> None:
        from kokoro_onnx import Kokoro

        model, threads = kokoro_model(), kokoro_threads()
        self._kokoro = Kokoro.from_session(kokoro_session(model, threads), str(TTS_VOICES_FILE))
        self._render("Ready.", preferences.tts_voice(), preferences.tts_speed(), timed=False)  # first synthesis is slow
        log.info("shared speech process: Kokoro loaded (%s, %s, memory arena %s)", model.name,
                 f"{threads} threads" if threads else "ONNX Runtime's default threads",
                 "on" if TTS_MEMORY_ARENA else "off")

    def _render(self, text: str, voice: str, speed: float, timed: bool = True) -> bytes:
        lang = "en-gb" if voice.startswith("b") else "en-us"
        with self._lock:
            started = time.perf_counter()
            samples, _ = self._kokoro.create(text, voice=voice, speed=speed, lang=lang)
            took = time.perf_counter() - started
        if timed:
            log_render(len(text.split()), took, len(samples) / KOKORO_RATE)
        return (np.clip(samples, -1, 1) * 32767).astype(np.int16).tobytes()

    def run(self, data: bytes) -> bytes:
        if self._kokoro is None:
            raise RuntimeError("Kokoro didn't load in the shared speech process; see logs/agent.log")
        req = json.loads(data)
        return self._render(req["text"], req["voice"], req["speed"])


def register_runners() -> None:
    """Register the Whisper and Kokoro runners. Call on the main thread before the server runs; idempotent."""
    for runner in (WhisperRunner, KokoroRunner):
        if runner.INFERENCE_METHOD not in _InferenceRunner.registered_runners:
            _InferenceRunner.register_runner(runner)


async def _infer(executor: InferenceExecutor | None, method: str, data: bytes) -> bytes:
    """Call the shared process. Raises non-retryable APIConnectionError if unreachable, failing or stuck."""
    try:
        executor = executor or get_job_context().inference_executor
        async with asyncio.timeout(INFERENCE_TIMEOUT):
            result = await executor.do_inference(method, data)
    except Exception as e:
        log.error("shared speech process failed on %s: %r. Restarting the agent service restarts it", method, e)
        raise APIConnectionError(f"shared speech process failed on {method}", retryable=False) from e
    if result is None:
        raise APIConnectionError(f"shared speech process returned nothing for {method}", retryable=False)
    return result


# ── Job side: LiveKit STT and TTS clients ──────────────────────


def _to_whisper_audio(buffer: utils.AudioBuffer) -> np.ndarray:
    frame = rtc.combine_audio_frames(buffer)
    audio = np.frombuffer(frame.data, dtype=np.int16).astype(np.float32) / 32768.0
    if frame.num_channels > 1:
        audio = audio.reshape(-1, frame.num_channels).mean(axis=1)
    if frame.sample_rate != WHISPER_RATE:
        n = int(len(audio) * WHISPER_RATE / frame.sample_rate)
        audio = np.interp(np.linspace(0, len(audio) - 1, n), np.arange(len(audio)), audio)
    return audio.astype(np.float32)


class WhisperSTT(stt.STT):
    """Batch STT on VAD-cut utterances, filtered by the speech gate and speaker check.

    The speaker check runs alongside Whisper; other voices are dropped. During voice enrollment clips feed the
    voiceprint instead. Whisper runs in the shared process; `executor` replaces it in tests."""

    def __init__(self, model: str = STT_MODEL, gate: SpeechGate | None = None,
                 executor: InferenceExecutor | None = None, speaker: SpeakerCheck | None = None) -> None:
        super().__init__(capabilities=stt.STTCapabilities(streaming=False, interim_results=False))
        self._model = model
        self._gate = gate or SpeechGate()
        self._speaker = speaker or SpeakerCheck()
        self._executor = executor
        self._vocabulary, self._prompt = Vocabulary(), ""
        self._lock = asyncio.Lock()  # one clip at a time, in order
        self.hearing = 0  # in-flight clips; holds announcements and the turn's end
        self.heard_at = 0.0  # monotonic time the last clip finished
        self.clips: list[dict] = []  # passed clips this turn (take_clips)

    @property
    def gate(self) -> SpeechGate:
        return self._gate

    @property
    def speaker(self) -> SpeakerCheck:
        return self._speaker

    @property
    def enrolling(self) -> bool:
        """True during voice enrollment; announcements wait."""
        return self._speaker.enrolling

    def use_vocabulary(self, vocabulary: Vocabulary, prompt: bool = True) -> None:
        """Set the conversation's vocabulary. `prompt` False only fixes mishearings, leaving Whisper unprompted."""
        self._vocabulary, self._prompt = vocabulary, vocabulary.prompt() if prompt else ""
        log.info("speech vocabulary: %d words, %d known mishearings%s", len(vocabulary.terms), len(vocabulary.fixes),
                 f"; Whisper's prompt: {self._prompt!r}" if self._prompt else "; Whisper unprompted")

    @property
    def model(self) -> str:
        return whisper.model(self._model)

    @property
    def provider(self) -> str:
        return whisper.backend()

    async def _transcribe(self, audio: np.ndarray) -> dict:
        """{text, no_speech, avg_logprob} from the shared process."""
        heard = json.loads(await _infer(self._executor, WHISPER_METHOD, whisper_request(audio, self._prompt)))
        return {"text": heard["text"], "no_speech": heard.get("no_speech"), "avg_logprob": heard.get("avg_logprob")}

    def warm_up(self) -> None:
        self._gate.measure(np.zeros(WHISPER_RATE, dtype=np.float32))  # loads its VAD
        log.info(self._gate.describe())

    def _measure(self, buffer: utils.AudioBuffer):
        audio = _to_whisper_audio(buffer)
        return audio, self._gate.measure(audio)

    async def _hear(self, buffer: utils.AudioBuffer) -> str:
        # off the event loop, which carries live audio
        audio, measured = await asyncio.to_thread(self._measure, buffer)
        if self._speaker.enrolling:
            return await self._enroll(audio)
        # speaker check on this process's CPU while Whisper runs in the shared one
        judging = asyncio.ensure_future(asyncio.to_thread(self._speaker.judge, audio)) if self._speaker.on else None
        try:
            heard = await self._transcribe(audio)
        finally:
            judged = await judging if judging is not None else None
        text = heard["text"]
        passes = judged is None or not judged.drop  # another voice: stay silent
        if passes:
            echo = bool(self._prompt) and self._vocabulary.echo(text)
            passes = await asyncio.to_thread(self._gate.passes, measured, text, heard["no_speech"], audio,
                                             avg_logprob=heard["avg_logprob"], echo=echo)
        if judged is not None:
            await asyncio.to_thread(self._speaker.record, judged, text)
        if not passes:
            return ""  # session ignores empty text
        if (fixed := self._vocabulary.correct(text)) != text:
            log.info("speech vocabulary put a mishearing right: %r as %r", text, fixed)
        self.clips.append({"words": len(fixed.split()), "logprob": heard["avg_logprob"], "speaker": self._who(judged)})
        return fixed

    def _who(self, judged) -> str | None:
        """Matched voiceprint name, if any."""
        if judged is None or not judged.reference:
            return None
        return self._speaker.prints[0].name if judged.reference == "this call" else judged.reference

    def take_clips(self) -> list[dict]:
        """Return and clear clips since the last call: [{words, logprob, speaker}]."""
        clips, self.clips = self.clips, []
        return clips

    async def _enroll(self, audio: np.ndarray) -> str:
        """Enrollment clip: feeds the voiceprint, not the brain. Transcribed only to catch "stop"."""
        heard = await self._transcribe(audio)
        said = await asyncio.to_thread(self._speaker.collect, audio, heard["text"])
        self._speaker.rearm()
        if said:
            self._speaker.announce(said)
        return ""

    async def _recognize_impl(self, buffer: utils.AudioBuffer, *, language: NotGivenOr[str] = NOT_GIVEN,
                              conn_options: APIConnectOptions) -> stt.SpeechEvent:
        self.hearing += 1
        try:
            async with self._lock:
                text = await self._hear(buffer)
        finally:
            self.hearing -= 1
            self.heard_at = time.monotonic()
        return stt.SpeechEvent(
            type=stt.SpeechEventType.FINAL_TRANSCRIPT,
            alternatives=[stt.SpeechData(language="en", text=text)],
        )


class KokoroTTS(tts.TTS):
    """Whole-piece TTS, fed one sentence or clause at a time via streaming(). `executor` replaces it in tests."""

    def __init__(self, voice: str | None = None, speed: float | None = None,
                 executor: InferenceExecutor | None = None) -> None:
        super().__init__(capabilities=tts.TTSCapabilities(streaming=False), sample_rate=KOKORO_RATE, num_channels=1)
        self._voice = voice or preferences.tts_voice()
        self._speed = preferences.tts_speed() if speed is None else speed
        self._executor = executor
        self._lock = asyncio.Lock()

    def use(self, voice: str, speed: float) -> None:
        """Set the conversation's voice and speed."""
        self._voice, self._speed = voice, speed

    @property
    def model(self) -> str:
        return "kokoro-v1.0"

    @property
    def provider(self) -> str:
        return "kokoro-onnx"

    async def render(self, text: str) -> bytes:
        req = {"text": text, "voice": self._voice, "speed": self._speed}
        return await _infer(self._executor, KOKORO_METHOD, json.dumps(req).encode())

    def synthesize(self, text: str, *, conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS) -> tts.ChunkedStream:
        return _KokoroStream(tts=self, input_text=text, conn_options=conn_options)


def streaming(engine: KokoroTTS) -> tts.StreamAdapter:
    """Session TTS that also cuts long sentences at pauses, so speech starts sooner. One per process."""
    return tts.StreamAdapter(tts=engine, sentence_tokenizer=ClauseTokenizer())


class _KokoroStream(tts.ChunkedStream):
    async def _run(self, output_emitter: tts.AudioEmitter) -> None:
        output_emitter.initialize(request_id=utils.shortuuid(), sample_rate=KOKORO_RATE, num_channels=1,
                                  mime_type="audio/pcm")
        engine: KokoroTTS = self._tts
        if self._input_text.strip():
            async with engine._lock:
                pcm = await engine.render(self._input_text)
            output_emitter.push(pcm)
        output_emitter.flush()
