"""On-device speech for LiveKit: Whisper in, Kokoro out. Nothing leaves the computer.

Models load once in LiveKit's shared inference process (runners registered here); job processes send it audio/text
over IPC, so concurrent calls share one Whisper and one Kokoro. That process runs requests on a thread pool, so each
runner locks, and the two share one lock when both run on MLX (one GPU job at a time).
If that process is down or a model failed to load, STT/TTS raise APIConnectionError instead of hanging.

Whisper runs on MLX (Apple GPU) on Apple silicon and on faster-whisper elsewhere (alfred.media.whisper). It is
prompted with the user's vocabulary, and known mishearings are fixed after the speech gate. Kokoro runs on MLX too on
Apple silicon (alfred.voice.kokoro_mlx, ALFRED_TTS_ENGINE), else on ONNX Runtime. Memory is kept lean: MLX's buffer
cache is capped (ALFRED_MLX_CACHE_MB) and Kokoro's ORT arena is off by default.
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

from alfred.config import (
    MLX_CACHE_MB,
    STT_MODEL,
    TTS_ENGINE,
    TTS_MEMORY_ARENA,
    TTS_MODEL,
    TTS_MODEL_FALLBACKS,
    TTS_THREADS,
    TTS_VOICES_FILE,
)
from alfred.core import platform
from alfred.media import whisper
from alfred.state import preferences
from alfred.state.vocabulary import Vocabulary
from alfred.voice import speech_gate
from alfred.voice.clauses import ClauseTokenizer
from alfred.voice.speaker import SpeakerCheck
from alfred.voice.speech_gate import SileroProbs, SpeechGate, compression_ratio

log = logging.getLogger("alfred.voice.speech")

WHISPER_RATE = 16000
KOKORO_RATE = 24000
WHISPER_METHOD = "alfred_whisper"
KOKORO_METHOD = "alfred_kokoro"
INFERENCE_TIMEOUT = 30  # seconds; only a stuck process hits it
MLX_LOCK = threading.Lock()  # Whisper and Kokoro on the Apple GPU: one at a time, as MLX runs one job per stream


# ── Shared process: one runner per model ───────────────────────


# Tokens a clip may decode: a margin plus a generous rate of speech (timestamps included), up to Whisper's own cap.
# A hallucinated loop ("I was a man, I was a man…") stops there, not at the window's end.
TOKENS_BASE, TOKENS_PER_SECOND, TOKENS_MOST = 20, 8, 224
# A dropped clip scoring within this of the speaker check's needed score is still transcribed, so the speaker log shows
# what was said by voices close to the user's (tuning); the rest skip Whisper.
NEAR_MISS = 0.05
EARLY_KEPT = 4  # utterances heard early awaiting their clip; older ones are given up


def transcribe(audio: np.ndarray, model: str = STT_MODEL, prompt: str = "") -> dict:
    """{text, no_speech (min over segments), avg_logprob (token-weighted)}; numbers None without segments.

    condition_on_previous_text is off so one phantom can't seed the next; `prompt` is the only context. One greedy
    pass: Whisper's default retries a doubtful window at rising temperatures, running the 30 s encoder up to six
    times for one clip and sampling foreign-language gibberish; the speech gate judges doubtful text itself."""
    tokens = min(TOKENS_MOST, TOKENS_BASE + round(TOKENS_PER_SECOND * len(audio) / WHISPER_RATE))
    result = whisper.run(audio, model, language="en", condition_on_previous_text=False, initial_prompt=prompt or None,
                         temperature=0.0, sample_len=tokens)
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
            self._lock = MLX_LOCK
        transcribe(np.zeros(WHISPER_RATE, dtype=np.float32))  # load (and on MLX compile kernels, ~3 s)
        self._ready = True
        if mlx:
            log.info("shared speech process: Whisper %s loaded, MLX cache capped at %s", STT_MODEL,
                     f"{MLX_CACHE_MB} MB" if MLX_CACHE_MB >= 0 else "nothing (ALFRED_MLX_CACHE_MB=-1)")
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
            log.warning("ALFRED_TTS_THREADS=%r isn't a number; choosing the threads myself", setting)
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
        on_gpu = self._on_mlx() if TTS_ENGINE == "mlx" and platform.APPLE_SILICON else None
        self._kokoro, how = on_gpu or self._on_onnx()
        self._render("Ready.", preferences.tts_voice(), preferences.tts_speed(), timed=False)  # first synthesis is slow
        log.info("shared speech process: Kokoro loaded (%s)", how)

    def _on_mlx(self):
        """(Kokoro on the Apple GPU, how) or None if it can't load; it then shares Whisper's GPU lock."""
        try:
            from alfred.voice import kokoro_mlx

            limit_mlx_cache()
            kokoro = kokoro_mlx.MlxKokoro.load(TTS_VOICES_FILE)
        except (ImportError, OSError, RuntimeError, ValueError) as e:  # not installed or downloaded (make models)
            log.warning("Kokoro couldn't load on MLX (%r); running it on ONNX Runtime", e)
            return None
        self._lock = MLX_LOCK
        return kokoro, f"on MLX, {kokoro_mlx.REPO}"

    def _on_onnx(self):
        from kokoro_onnx import Kokoro

        model, threads = kokoro_model(), kokoro_threads()
        kokoro = Kokoro.from_session(kokoro_session(model, threads), str(TTS_VOICES_FILE))
        cores = f"{threads} threads" if threads else "ONNX Runtime's default threads"
        return kokoro, f"{model.name}, {cores}, memory arena {'on' if TTS_MEMORY_ARENA else 'off'}"

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
    """16 kHz mono float32. LiveKit's resampler filters out what 16 kHz can't hold (the room's 8-12 kHz); plain
    interpolation folded it back into the band Whisper, the gate and the voiceprints listen to."""
    frame = rtc.combine_audio_frames(buffer)
    if frame.sample_rate != WHISPER_RATE:
        resampler = rtc.AudioResampler(frame.sample_rate, WHISPER_RATE, num_channels=frame.num_channels,
                                       quality=rtc.AudioResamplerQuality.HIGH)
        frames = [*resampler.push(frame), *resampler.flush()]
        if not frames:
            return np.zeros(0, dtype=np.float32)
        frame = rtc.combine_audio_frames(frames)
    audio = np.frombuffer(frame.data, dtype=np.int16).astype(np.float32) / 32768.0
    if frame.num_channels > 1:
        audio = audio.reshape(-1, frame.num_channels).mean(axis=1)
    return audio.astype(np.float32)


class WhisperSTT(stt.STT):
    """Batch STT on VAD-cut utterances, filtered by the speech gate and speaker check.

    The speaker check runs first and other voices are dropped without Whisper. During voice enrollment clips feed the
    voiceprint instead. Whisper runs in the shared process; `executor` replaces it in tests."""

    def __init__(self, model: str = STT_MODEL, gate: SpeechGate | None = None,
                 executor: InferenceExecutor | None = None, speaker: SpeakerCheck | None = None) -> None:
        super().__init__(capabilities=stt.STTCapabilities(streaming=False, interim_results=False))
        self._model = model
        measured = SileroProbs()  # the gate measures each clip first; the speaker check reuses that, same clip
        self._gate = gate or SpeechGate(speech_probs=measured)
        self._speaker = speaker or SpeakerCheck(speech_probs=speech_gate.reusing(measured))
        self._executor = executor
        self._vocabulary, self._prompt = Vocabulary(), ""
        self._lock = asyncio.Lock()  # one clip at a time, in order
        self.hearing = 0  # in-flight clips; holds announcements and the turn's end
        self.heard_at = 0.0  # monotonic time the last clip finished
        self.clips: list[dict] = []  # passed clips this turn (take_clips)
        # Utterances heard early (alfred.voice.early), by where their speech starts: (offset, bytes) of the clip's audio
        self._early: dict[tuple[int, bytes], asyncio.Future] = {}
        self._early_probs = SileroProbs()  # the early speaker check's own Silero: a clip may be measured alongside

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

    async def _transcribe(self, audio: np.ndarray, prompt: str | None = None) -> dict:
        """{text, no_speech, avg_logprob} from the shared process; prompted with the vocabulary unless `prompt`."""
        said = self._prompt if prompt is None else prompt
        heard = json.loads(await _infer(self._executor, WHISPER_METHOD, whisper_request(audio, said)))
        return {"text": heard["text"], "no_speech": heard.get("no_speech"), "avg_logprob": heard.get("avg_logprob")}

    async def _heard(self, audio: np.ndarray) -> dict:
        """Whisper's words for a clip. A short clip can lead prompted Whisper into a loop of the prompt's words
        ("JARVIS, JARVIS, JARVIS, …") or a run of one sound, which the gate would drop: then it hears it once more,
        unprompted. On the MacBook Air that recovered "Hi Jarvis", "Yeah, sure." and "Yeah, it is done."."""
        heard = await self._transcribe(audio)
        text = heard["text"]
        if self._prompt and (compression_ratio(text) > self._gate.thresholds.max_compression
                             or self._vocabulary.echo(text)):
            heard = await self._transcribe(audio, prompt="")
            log.info("Whisper looped on the prompt (%r); unprompted it heard %r", text[:60], heard["text"])
        return heard

    def hear_early(self, onset: tuple[int, bytes], buffer: utils.AudioBuffer) -> None:
        """Start hearing an utterance so far, before the VAD ends its clip (alfred.voice.early). The clip whose audio
        has `onset` (offset, bytes: where its speech starts) uses these words, unless drop_early(onset) came first."""
        self.drop_early(onset)
        while len(self._early) >= EARLY_KEPT:
            self._early.pop(next(iter(self._early))).cancel()
        self._early[onset] = asyncio.ensure_future(self._guess(buffer))

    def drop_early(self, onset: tuple[int, bytes]) -> None:
        """Give up the early hearing of `onset`'s utterance: speech went on, so its clip will be longer."""
        if (guess := self._early.pop(onset, None)) is not None:
            guess.cancel()

    async def _guess(self, buffer: utils.AudioBuffer) -> dict | None:
        """Whisper's words for an utterance so far; None if it sounds like another voice (its clip skips Whisper)."""
        audio = await asyncio.to_thread(_to_whisper_audio, buffer)
        if self._speaker.on and not await asyncio.to_thread(self._speaker.likely, audio, self._early_probs):
            return None
        return await self._heard(audio)

    def _heard_early(self, buffer: utils.AudioBuffer) -> asyncio.Future | None:
        """The early hearing of this clip's utterance, taken (each is used once), or None."""
        if not self._early:
            return None
        data = rtc.combine_audio_frames(buffer).data.tobytes()
        for onset in list(self._early):
            offset, sound = onset
            if data[offset:offset + len(sound)] == sound:
                return self._early.pop(onset)
        return None

    @staticmethod
    async def _words(guess: asyncio.Future | None) -> dict | None:
        """Words heard early, if any came (not another voice, nor failed)."""
        if guess is None or guess.cancelled():
            return None
        try:
            return await guess
        except APIConnectionError:
            return None

    def warm_up(self) -> None:
        self._gate.measure(np.zeros(WHISPER_RATE, dtype=np.float32))  # loads its VAD
        log.info(self._gate.describe())

    def _measure(self, buffer: utils.AudioBuffer):
        audio = _to_whisper_audio(buffer)
        return audio, self._gate.measure(audio)

    async def _hear(self, buffer: utils.AudioBuffer, early: asyncio.Future | None = None) -> str:
        """The clip's words for the brain, or "" to ignore it. `early`: Whisper started on it before it ended."""
        # off the event loop, which carries live audio
        audio, measured = await asyncio.to_thread(self._measure, buffer)
        if self._speaker.enrolling:
            if early is not None:
                early.cancel()
            return await self._enroll(audio)
        # The speaker check first (~25 ms): another voice's clip, two in three on the MacBook Air, skips Whisper, which
        # would hold this call's next clip and Kokoro's GPU for ~1 s. Near misses are still heard, for the log.
        judged = await asyncio.to_thread(self._speaker.judge, audio) if self._speaker.on else None
        if judged is not None and judged.drop:
            if early is not None:
                early.cancel()
            near = judged.score is not None and judged.score >= judged.needed - NEAR_MISS
            await asyncio.to_thread(self._speaker.record, judged, (await self._heard(audio))["text"] if near else None)
            return ""  # another voice: stay silent
        heard = await self._words(early)
        heard_early = heard is not None
        started = time.monotonic()
        heard = heard or await self._heard(audio)
        whisper = None if heard_early else time.monotonic() - started  # its wait for Whisper (the GPU queue included)
        text = heard["text"]
        echo = bool(self._prompt) and self._vocabulary.echo(text)
        passes = await asyncio.to_thread(self._gate.passes, measured, text, heard["no_speech"], audio,
                                         avg_logprob=heard["avg_logprob"], echo=echo, whisper=whisper)
        if judged is not None:
            await asyncio.to_thread(self._speaker.record, judged, text)
        if not passes:
            return ""  # session ignores empty text
        if (fixed := self._vocabulary.correct(text)) != text:
            log.info("speech vocabulary put a mishearing right: %r as %r", text, fixed)
        # Silero's speech seconds: the gate's, else the speaker check's, else unknown (both off)
        speech = measured.speech_seconds if measured is not None else judged.speech_seconds if judged else None
        self.clips.append({"words": len(fixed.split()), "logprob": heard["avg_logprob"], "speaker": self._who(judged),
                           "speech": speech, "early": heard_early,
                           "whisper": None if whisper is None else round(whisper, 3)})
        return fixed

    def _who(self, judged) -> str | None:
        """Matched voiceprint name, if any."""
        if judged is None or not judged.reference:
            return None
        return self._speaker.prints[0].name if judged.reference == "this call" else judged.reference

    def take_clips(self) -> list[dict]:
        """Return and clear clips since the last call: [{words, logprob, speaker, speech (Silero seconds), early,
        whisper (seconds waited on Whisper; None if heard early)}]."""
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
        early = self._heard_early(buffer)
        try:
            async with self._lock:
                text = await self._hear(buffer, early)
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
