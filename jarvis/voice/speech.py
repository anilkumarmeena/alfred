"""On-device speech for LiveKit: Whisper (MLX, Apple GPU) in, Kokoro out. Nothing leaves the Mac.

The models load once, in LiveKit's inference process: the worker starts that one process before it takes any call
(its runners are the classes registered here), and every job process, one per call from the Mac, phone or browser,
sends it audio or text over the worker's IPC. So two calls share one Whisper and one Kokoro, and a job process holds
no model of its own (only the small Silero VAD and noise filter, which keep per-call state). The inference process
handles one request at a time, which queues calls' requests behind each other; each runner also holds a lock, so a
model never runs twice at once. If that process is down, or a model failed to load in it, a call's STT or TTS raises
an APIConnectionError saying so, rather than hang.
Whisper is prompted with the user's speech vocabulary (jarvis.state.vocabulary), sent with each clip, and a clip the
speech gate passes has the vocabulary's known mishearings put right before the brain hears it.
It keeps no more memory than it's using: MLX's cache of freed GPU buffers is capped (JARVIS_MLX_CACHE_MB), and
Kokoro runs without ONNX Runtime's memory arena (JARVIS_TTS_MEMORY_ARENA), which grew to fit the longest sentence yet.
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

from jarvis.core.config import (
    MLX_CACHE_MB,
    ROOT,
    STT_MODEL,
    TTS_MEMORY_ARENA,
    TTS_MODEL,
    TTS_MODEL_FALLBACKS,
    TTS_SPEED,
    TTS_THREADS,
    TTS_VOICE,
    service_label,
)
from jarvis.state.vocabulary import Vocabulary
from jarvis.voice.clauses import ClauseTokenizer
from jarvis.voice.speaker import SpeakerCheck
from jarvis.voice.speech_gate import SpeechGate

log = logging.getLogger("jarvis.voice.speech")

WHISPER_RATE = 16000
KOKORO_RATE = 24000
WHISPER_METHOD = "jarvis_whisper"
KOKORO_METHOD = "jarvis_kokoro"
INFERENCE_TIMEOUT = 30  # seconds: far more than a clip or sentence takes, so only a stuck process hits it


# ── The shared process: one runner per model ────────────────────


def transcribe(audio: np.ndarray, model: str = STT_MODEL, prompt: str = "") -> dict:
    """{text, no_speech, avg_logprob}: Whisper's no-speech probability (the lowest of its segments') and its average
    log-probability per token over the whole text, each None without any segments. Each clip is heard on its own
    (condition_on_previous_text off), so one phantom can't seed the next; `prompt`, the words to expect, is the only
    text it's given before the clip."""
    import mlx_whisper

    result: dict = mlx_whisper.transcribe(audio, path_or_hf_repo=model, language="en",
                                          condition_on_previous_text=False, initial_prompt=prompt or None)
    segments = result.get("segments", [])
    no_speech = [s["no_speech_prob"] for s in segments if "no_speech_prob" in s]
    scored = [(s["avg_logprob"], max(len(s.get("tokens", [])), 1)) for s in segments if "avg_logprob" in s]
    tokens = sum(n for _, n in scored)
    return {"text": result["text"].strip(), "no_speech": min(no_speech, default=None),
            "avg_logprob": sum(lp * n for lp, n in scored) / tokens if scored else None}


def limit_mlx_cache(mb: int = MLX_CACHE_MB) -> None:
    """Cap the GPU buffers MLX keeps for reuse after a clip (JARVIS_MLX_CACHE_MB); without a cap they stay at the
    largest clip's needs, about 0.7 GB for Whisper large-v3-turbo. -1 leaves MLX's own default, no cap."""
    if mb < 0:
        return
    try:
        import mlx.core as mx

        mx.set_cache_limit(mb * 2**20)
    except Exception as e:  # Whisper still runs, only without the cap
        log.warning("couldn't cap MLX's cache at %d MB: %r", mb, e)


def mlx_memory() -> str:
    """MLX's memory now, for the logs: what Whisper holds, and what's cached for reuse."""
    import mlx.core as mx

    return f"MLX {mx.get_active_memory() / 2**20:.0f} MB in use, {mx.get_cache_memory() / 2**20:.0f} MB cached"


def whisper_request(audio: np.ndarray, prompt: str = "") -> bytes:
    """A clip as the shared process takes it: the prompt's length in 4 bytes and the prompt, in UTF-8, then the
    samples."""
    said = prompt.encode()
    return len(said).to_bytes(4, "little") + said + audio.astype(np.float32).tobytes()


def read_whisper_request(data: bytes) -> tuple[np.ndarray, str]:
    n = int.from_bytes(data[:4], "little")
    return np.frombuffer(data[4 + n:], dtype=np.float32), data[4:4 + n].decode(errors="replace")


class WhisperRunner(_InferenceRunner):
    """Whisper, in the inference process. In: a prompt and 16 kHz mono float32 samples (whisper_request). Out: JSON
    {text, no_speech, avg_logprob}."""
    INFERENCE_METHOD = WHISPER_METHOD

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._ready = False

    def initialize(self) -> None:
        limit_mlx_cache()
        transcribe(np.zeros(WHISPER_RATE, dtype=np.float32))  # loads it, and compiles the MLX kernels (~3 s)
        self._ready = True
        log.info("shared speech process: Whisper %s loaded, MLX cache capped at %s", STT_MODEL,
                 f"{MLX_CACHE_MB} MB" if MLX_CACHE_MB >= 0 else "nothing (JARVIS_MLX_CACHE_MB=-1)")

    def run(self, data: bytes) -> bytes:
        if not self._ready:  # LiveKit logs a runner's failed initialize and carries on without it
            raise RuntimeError("Whisper didn't load in the shared speech process; see logs/agent.log")
        audio, prompt = read_whisper_request(data)
        with self._lock:
            heard = transcribe(audio, prompt=prompt)
            if log.isEnabledFor(logging.DEBUG):
                log.debug("Whisper heard a %.1f s clip; %s", len(audio) / WHISPER_RATE, mlx_memory())
        return json.dumps(heard).encode()


def kokoro_model(chosen: Path = TTS_MODEL, fallbacks: tuple[Path, ...] = TTS_MODEL_FALLBACKS) -> Path:
    """The Kokoro model file to load: the chosen one (JARVIS_TTS_MODEL), or if it's missing the first fallback there
    is (the full 32-bit one, then the 8-bit one)."""
    if chosen.exists():
        return chosen
    fallback = next((f for f in fallbacks if f.exists()), fallbacks[0])
    log.warning("Kokoro model %s is missing; using %s", chosen, fallback.name)
    return fallback


def performance_cores() -> int:
    """This Mac's performance cores (all its physical cores on an Intel Mac), else the CPU count."""
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
    """Kokoro's intra-op threads (JARVIS_TTS_THREADS): one fewer than the performance cores, 2 to 4, unless set; 0 is
    ONNX Runtime's own default (every physical core, efficiency ones too). More threads didn't render faster under
    load, they only took CPU from the call's own process."""
    if setting:
        try:
            return max(0, int(setting))
        except ValueError:
            log.warning("JARVIS_TTS_THREADS=%r isn't a number; choosing the threads myself", setting)
    return min(4, max(2, (cores or performance_cores()) - 1))


def kokoro_session(model: Path, threads: int, arena: bool = TTS_MEMORY_ARENA):
    """Kokoro's ONNX Runtime session, on the providers kokoro-onnx would pick. Without the CPU memory arena
    (JARVIS_TTS_MEMORY_ARENA) a sentence's buffers are freed when it's done, rather than kept at the size of the
    longest sentence yet. Its `threads` (kokoro_threads(); 0 is ONNX Runtime's default) sleep between sentences
    rather than spin, which kept idle cores busy for nothing."""
    import onnxruntime as ort
    from kokoro_onnx.session import resolve_providers

    options = ort.SessionOptions()
    options.enable_cpu_mem_arena = arena
    options.intra_op_num_threads = threads
    options.add_session_config_entry("session.intra_op.allow_spinning", "0")
    return ort.InferenceSession(str(model), options, providers=resolve_providers())


def log_render(words: int, took: float, speech: float) -> None:
    """One piece's render time against its length when spoken. Each piece renders while the one before it plays, so
    rendering slower than speaking (over 1x real time) leaves pauses between pieces: those are warnings."""
    ratio = took / speech if speech else 0.0
    slow = ratio > 1
    log.log(logging.WARNING if slow else logging.INFO,
            "Kokoro rendered %d words in %.0f ms for %.1f s of speech (%.2fx real time)%s", words, took * 1000,
            speech, ratio, ", slower than speaking it, so it can leave a pause" if slow else "")


class KokoroRunner(_InferenceRunner):
    """Kokoro, in the inference process. In: JSON {text, voice, speed}. Out: 24 kHz mono int16 PCM."""
    INFERENCE_METHOD = KOKORO_METHOD

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._kokoro = None

    def initialize(self) -> None:
        from kokoro_onnx import Kokoro

        model, threads = kokoro_model(), kokoro_threads()
        self._kokoro = Kokoro.from_session(kokoro_session(model, threads), str(ROOT / "models/voices-v1.0.bin"))
        self._render("Ready.", TTS_VOICE, TTS_SPEED, timed=False)  # the first synthesis is the slow one
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
    """Have the worker start the shared process with Whisper and Kokoro. Call on the main thread before the server
    runs; calling it again (a spawned process re-importing the agent module) does nothing."""
    for runner in (WhisperRunner, KokoroRunner):
        if runner.INFERENCE_METHOD not in _InferenceRunner.registered_runners:
            _InferenceRunner.register_runner(runner)


async def _infer(executor: InferenceExecutor | None, method: str, data: bytes) -> bytes:
    """One request to the shared process, from a job process. Raises APIConnectionError, not retried, when it's
    unreachable, failing or stuck."""
    try:
        executor = executor or get_job_context().inference_executor
        async with asyncio.timeout(INFERENCE_TIMEOUT):
            result = await executor.do_inference(method, data)
    except Exception as e:
        log.error("shared speech process failed on %s: %r. Restarting the agent (%s) restarts it",
                  method, e, service_label("agent"))
        raise APIConnectionError(f"shared speech process failed on {method}", retryable=False) from e
    if result is None:
        raise APIConnectionError(f"shared speech process returned nothing for {method}", retryable=False)
    return result


# ── The job side: LiveKit STT and TTS that call it ──────────────


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
    """Batch recognition; the session's VAD cuts speech into utterances first. The speech gate then judges each
    clip's audio and Whisper's confidence, so Whisper's made-up text for near-silence never reaches the brain
    (jarvis.voice.speech_gate). Once the user has a voiceprint, the speaker check judges whose voice it is, while
    Whisper transcribes, and a clip that isn't theirs goes no further (jarvis.voice.speaker); while they enroll by
    voice, their clips go to the voiceprint instead. The vocabulary (use_vocabulary) is Whisper's prompt, and puts
    right what a clip that passes has misheard.
    Whisper itself runs in the shared process; `executor` stands in for it in tests."""

    def __init__(self, model: str = STT_MODEL, gate: SpeechGate | None = None,
                 executor: InferenceExecutor | None = None, speaker: SpeakerCheck | None = None) -> None:
        super().__init__(capabilities=stt.STTCapabilities(streaming=False, interim_results=False))
        self._model = model
        self._gate = gate or SpeechGate()
        self._speaker = speaker or SpeakerCheck()
        self._executor = executor
        self._vocabulary, self._prompt = Vocabulary(), ""
        self._lock = asyncio.Lock()  # one clip at a time, in the order heard
        self.hearing = 0  # clips being transcribed now, which hold announcements back (jarvis.voice.floor)
        self.clips: list[dict] = []  # those passed on since the turn under way began (take_clips)

    @property
    def gate(self) -> SpeechGate:
        return self._gate

    @property
    def speaker(self) -> SpeakerCheck:
        return self._speaker

    @property
    def enrolling(self) -> bool:
        """The user is enrolling their voice by talking: announcements wait (jarvis.voice.floor)."""
        return self._speaker.enrolling

    def use_vocabulary(self, vocabulary: Vocabulary, prompt: bool = True) -> None:
        """The words to expect and mishearings to put right, for the conversation starting (vocabulary.load());
        `prompt` False (JARVIS_STT_PROMPT=off) only puts mishearings right, leaving Whisper unprompted."""
        self._vocabulary, self._prompt = vocabulary, vocabulary.prompt() if prompt else ""
        log.info("speech vocabulary: %d words, %d known mishearings%s", len(vocabulary.terms), len(vocabulary.fixes),
                 f"; Whisper's prompt: {self._prompt!r}" if self._prompt else "; Whisper unprompted")

    @property
    def model(self) -> str:
        return self._model

    @property
    def provider(self) -> str:
        return "mlx-whisper"

    async def _transcribe(self, audio: np.ndarray) -> dict:
        """{text, no_speech, avg_logprob}, as transcribe gave them."""
        heard = json.loads(await _infer(self._executor, WHISPER_METHOD, whisper_request(audio, self._prompt)))
        return {"text": heard["text"], "no_speech": heard.get("no_speech"), "avg_logprob": heard.get("avg_logprob")}

    def warm_up(self) -> None:
        self._gate.measure(np.zeros(WHISPER_RATE, dtype=np.float32))  # loads its VAD
        log.info(self._gate.describe())

    def _measure(self, buffer: utils.AudioBuffer):
        audio = _to_whisper_audio(buffer)
        return audio, self._gate.measure(audio)

    async def _hear(self, buffer: utils.AudioBuffer) -> str:
        # converting and gating in a thread keeps the event loop free for live audio
        audio, measured = await asyncio.to_thread(self._measure, buffer)
        if self._speaker.enrolling:
            return await self._enroll(audio)
        # whose voice it is, judged on this process's CPU while Whisper runs in the shared one
        judging = asyncio.ensure_future(asyncio.to_thread(self._speaker.judge, audio)) if self._speaker.on else None
        try:
            heard = await self._transcribe(audio)
        finally:
            judged = await judging if judging is not None else None
        text = heard["text"]
        passes = judged is None or not judged.drop  # someone else's words go no further: JARVIS stays silent
        if passes:
            echo = bool(self._prompt) and self._vocabulary.echo(text)
            passes = await asyncio.to_thread(self._gate.passes, measured, text, heard["no_speech"], audio,
                                             avg_logprob=heard["avg_logprob"], echo=echo)
        if judged is not None:
            await asyncio.to_thread(self._speaker.record, judged, text)
        if not passes:
            return ""  # empty: the session ignores it
        if (fixed := self._vocabulary.correct(text)) != text:
            log.info("speech vocabulary put a mishearing right: %r as %r", text, fixed)
        self.clips.append({"words": len(fixed.split()), "logprob": heard["avg_logprob"], "speaker": self._who(judged)})
        return fixed

    def _who(self, judged) -> str | None:
        """The voiceprint a clip matched, if the speaker check judged it."""
        if judged is None or not judged.reference:
            return None
        return self._speaker.prints[0].name if judged.reference == "this call" else judged.reference

    def take_clips(self) -> list[dict]:
        """The clips heard since the last call, {words, logprob, speaker}: a turn's, once it's over
        (jarvis.voice.turn.clips_heard)."""
        clips, self.clips = self.clips, []
        return clips

    async def _enroll(self, audio: np.ndarray) -> str:
        """A clip while the user enrolls by voice: its speech goes to the voiceprint, not the brain. Transcribed only
        to hear a "stop"."""
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
        return stt.SpeechEvent(
            type=stt.SpeechEventType.FINAL_TRANSCRIPT,
            alternatives=[stt.SpeechData(language="en", text=text)],
        )


class KokoroTTS(tts.TTS):
    """Whole-piece synthesis; the session feeds it one sentence, or part of a long one, at a time as the brain streams
    (streaming()). Kokoro itself runs in the shared process; `executor` stands in for it in tests."""

    def __init__(self, voice: str = TTS_VOICE, speed: float = TTS_SPEED,
                 executor: InferenceExecutor | None = None) -> None:
        super().__init__(capabilities=tts.TTSCapabilities(streaming=False), sample_rate=KOKORO_RATE, num_channels=1)
        self._voice, self._speed = voice, speed
        self._executor = executor
        self._lock = asyncio.Lock()

    def use(self, voice: str, speed: float) -> None:
        """The voice and speed for the conversation starting (config.tts_voice(), config.tts_speed())."""
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
    """Kokoro as the session's TTS: the brain's streamed text cut into sentences, and long sentences at their pauses
    (jarvis.voice.clauses), so speaking starts before a long sentence is all rendered. LiveKit would otherwise wrap
    it in an adapter of its own that cuts at sentence ends only. Make one per process: it listens to `engine`."""
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
