"""Tests for jarvis.voice.speech: Whisper/Kokoro load once in the shared process; a down or stuck one fails fast."""

import asyncio
import json
import os
import sys
import types

import numpy as np
import pytest
from livekit import rtc
from livekit.agents import APIConnectionError
from livekit.agents.inference_runner import _InferenceRunner
from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS

from jarvis.media import whisper
from jarvis.state.vocabulary import Vocabulary
from jarvis.voice import speech
from jarvis.voice.speech_gate import Measure, SpeechGate


class FakeExecutor:
    """Fake link from the worker to the shared process."""

    def __init__(self, reply=b"", error: Exception | None = None, hang: bool = False) -> None:
        self.reply, self.error, self.hang = reply, error, hang
        self.calls: list[tuple[str, bytes]] = []

    async def do_inference(self, method: str, data: bytes) -> bytes | None:
        self.calls.append((method, data))
        if self.hang:
            await asyncio.Event().wait()
        if self.error:
            raise self.error
        return self.reply


def off_gate() -> SpeechGate:
    return SpeechGate("off", record=None, audio_dir=None)


def test_whisper_sends_the_clip_and_reads_the_reply():
    executor = FakeExecutor(json.dumps({"text": "Hello there.", "no_speech": 0.1, "avg_logprob": -0.4}).encode())
    stt = speech.WhisperSTT(gate=off_gate(), executor=executor)
    audio = np.linspace(-0.5, 0.5, 1600, dtype=np.float32)
    assert asyncio.run(stt._transcribe(audio)) == {"text": "Hello there.", "no_speech": 0.1, "avg_logprob": -0.4}
    [(method, data)] = executor.calls
    sent, prompt = speech.read_whisper_request(data)
    assert method == speech.WHISPER_METHOD and np.array_equal(sent, audio) and prompt == ""


def test_whisper_is_prompted_with_the_vocabulary_and_mishearings_are_put_right():
    executor = FakeExecutor(json.dumps({"text": "Hey Yavis, open the ai army.", "no_speech": 0.0}).encode())
    stt = speech.WhisperSTT(gate=off_gate(), executor=executor)
    stt.use_vocabulary(Vocabulary(("JARVIS", "AI Armory"), (("Yavis", "JARVIS"), ("ai army", "AI Armory"))))
    pcm = (np.sin(np.arange(4800) / 10) * 8000).astype(np.int16)
    event = asyncio.run(stt._recognize_impl([rtc.AudioFrame(pcm.tobytes(), 48000, 1, len(pcm))],
                                            conn_options=DEFAULT_API_CONNECT_OPTIONS))
    assert event.alternatives[0].text == "Hey JARVIS, open the AI Armory."
    assert speech.read_whisper_request(executor.calls[0][1])[1] == "JARVIS, AI Armory."


def test_with_the_prompt_off_whisper_is_unprompted_but_mishearings_are_still_put_right():
    executor = FakeExecutor(json.dumps({"text": "Yavis, hello.", "no_speech": 0.0}).encode())
    stt = speech.WhisperSTT(gate=off_gate(), executor=executor)
    stt.use_vocabulary(Vocabulary(("JARVIS",), (("Yavis", "JARVIS"),)), prompt=False)
    assert asyncio.run(stt._hear([rtc.AudioFrame(np.zeros(480, np.int16).tobytes(), 48000, 1, 480)])) == "JARVIS, hello."
    assert speech.read_whisper_request(executor.calls[0][1])[1] == ""


def test_a_transcript_that_only_echoes_the_prompt_is_dropped(monkeypatch):
    executor = FakeExecutor(json.dumps({"text": "JARVIS, AI Armory, Hermes Agent.", "no_speech": 0.0}).encode())
    gate = SpeechGate("enforce", record=None, audio_dir=None)
    loud = Measure(seconds=1.5, rms_dbfs=-20, loudest_dbfs=-10, speech_seconds=1.2, speech_ratio=0.8)
    monkeypatch.setattr(gate, "measure", lambda audio: loud)
    stt = speech.WhisperSTT(gate=gate, executor=executor)
    stt.use_vocabulary(Vocabulary(("JARVIS", "AI Armory", "Hermes Agent")))
    clip = [rtc.AudioFrame(np.zeros(480, np.int16).tobytes(), 48000, 1, 480)]
    assert asyncio.run(stt._hear(clip)) == ""
    executor.reply = json.dumps({"text": "JARVIS, open AI Armory.", "no_speech": 0.0}).encode()
    assert asyncio.run(stt._hear(clip)) == "JARVIS, open AI Armory."


def test_a_call_hears_through_the_shared_process():
    executor = FakeExecutor(json.dumps({"text": "What's on today?", "no_speech": None}).encode())
    stt = speech.WhisperSTT(gate=off_gate(), executor=executor)
    pcm = (np.sin(np.arange(4800) / 10) * 8000).astype(np.int16)
    event = asyncio.run(stt._recognize_impl([rtc.AudioFrame(pcm.tobytes(), 48000, 1, len(pcm))],
                                            conn_options=DEFAULT_API_CONNECT_OPTIONS))
    assert event.alternatives[0].text == "What's on today?" and stt.hearing == 0
    assert len(speech.read_whisper_request(executor.calls[0][1])[0]) == 1600  # resampled to 16 kHz before sending


def test_kokoro_sends_the_sentence_with_its_voice():
    executor = FakeExecutor(b"\x01\x00\x02\x00")
    tts = speech.KokoroTTS(voice="bf_emma", speed=1.2, executor=executor)
    assert asyncio.run(tts.render("Good morning.")) == b"\x01\x00\x02\x00"
    [(method, data)] = executor.calls
    assert method == speech.KOKORO_METHOD
    assert json.loads(data) == {"text": "Good morning.", "voice": "bf_emma", "speed": 1.2}


def test_a_failing_shared_process_fails_clearly(caplog):
    tts = speech.KokoroTTS(executor=FakeExecutor(error=RuntimeError("inference of jarvis_kokoro failed: boom")))
    with pytest.raises(APIConnectionError) as raised:
        asyncio.run(tts.render("Hello."))
    assert "shared speech process" in str(raised.value) and not raised.value.retryable
    assert "Restarting the agent service restarts it" in caplog.text


def test_a_stuck_shared_process_times_out(monkeypatch):
    monkeypatch.setattr(speech, "INFERENCE_TIMEOUT", 0.05)
    stt = speech.WhisperSTT(gate=off_gate(), executor=FakeExecutor(hang=True))
    with pytest.raises(APIConnectionError):
        asyncio.run(stt._transcribe(np.zeros(160, np.float32)))


def test_without_a_job_there_is_no_shared_process_to_reach():
    with pytest.raises(APIConnectionError):
        asyncio.run(speech.KokoroTTS().render("Hello."))


def test_the_runners_refuse_work_when_their_model_failed_to_load():
    with pytest.raises(RuntimeError, match="Whisper didn't load"):
        speech.WhisperRunner().run(np.zeros(160, np.float32).tobytes())
    with pytest.raises(RuntimeError, match="Kokoro didn't load"):
        speech.KokoroRunner().run(b'{"text": "Hi.", "voice": "bm_george", "speed": 1.0}')


def test_the_whisper_runner_transcribes_what_it_is_sent(monkeypatch):
    heard = []

    def fake_transcribe(audio, model=speech.STT_MODEL, prompt=""):
        heard.append((audio, prompt))
        if len(audio) == 160:
            return {"text": "Hello.", "no_speech": 0.2, "avg_logprob": -0.3}
        return {"text": "", "no_speech": None, "avg_logprob": None}

    monkeypatch.setattr(speech, "transcribe", fake_transcribe)
    monkeypatch.setattr(speech, "limit_mlx_cache", lambda: None)
    runner = speech.WhisperRunner()
    runner.initialize()  # its warm-up uses the same transcribe
    clip = np.full(160, 0.25, np.float32)
    reply = runner.run(speech.whisper_request(clip, "JARVIS, Zoë."))
    assert json.loads(reply) == {"text": "Hello.", "no_speech": 0.2, "avg_logprob": -0.3}
    assert np.array_equal(heard[-1][0], clip) and heard[-1][1] == "JARVIS, Zoë."


def test_an_older_shared_process_without_logprob_still_works():
    executor = FakeExecutor(json.dumps({"text": "Hi.", "no_speech": 0.0}).encode())
    stt = speech.WhisperSTT(gate=off_gate(), executor=executor)
    assert asyncio.run(stt._transcribe(np.zeros(160, np.float32)))["avg_logprob"] is None


@pytest.fixture
def mlx(monkeypatch):
    """The MLX backend, as on Apple silicon (faked where it isn't)."""
    monkeypatch.setattr(whisper, "backend", lambda: whisper.MLX)


def test_transcribe_averages_the_logprob_over_every_token(monkeypatch, mlx):
    """Each clip is heard alone (no conditioning on the last); longer segments weigh more."""
    calls = []

    def fake(audio, **kwargs):
        calls.append(kwargs)
        return {"text": " One two three. Four. ", "segments": [
            {"no_speech_prob": 0.01, "avg_logprob": -0.2, "tokens": [1, 2, 3]},
            {"no_speech_prob": 0.3, "avg_logprob": -1.0, "tokens": [4]}]}

    monkeypatch.setitem(sys.modules, "mlx_whisper", types.SimpleNamespace(transcribe=fake))
    heard = speech.transcribe(np.zeros(160, np.float32))
    assert heard["text"] == "One two three. Four." and heard["no_speech"] == 0.01
    assert heard["avg_logprob"] == pytest.approx(-0.4)
    assert calls[0]["condition_on_previous_text"] is False and calls[0]["language"] == "en"
    assert calls[0]["initial_prompt"] is None  # no vocabulary, no prompt
    speech.transcribe(np.zeros(160, np.float32), prompt="JARVIS, AI Armory.")
    assert calls[1]["initial_prompt"] == "JARVIS, AI Armory."
    monkeypatch.setitem(sys.modules, "mlx_whisper", types.SimpleNamespace(transcribe=lambda a, **k: {"text": ""}))
    assert speech.transcribe(np.zeros(160, np.float32)) == {"text": "", "no_speech": None, "avg_logprob": None}


def test_the_kokoro_runner_renders_int16_pcm():
    class FakeKokoro:
        def create(self, text, voice, speed, lang):
            self.args = (text, voice, speed, lang)
            return np.array([0.0, 0.5, 2.0], np.float32), speech.KOKORO_RATE

    runner = speech.KokoroRunner()
    runner._kokoro = FakeKokoro()
    pcm = runner.run(json.dumps({"text": "Hi.", "voice": "am_adam", "speed": 1.0}).encode())
    assert list(np.frombuffer(pcm, np.int16)) == [0, 16383, 32767]
    assert runner._kokoro.args == ("Hi.", "am_adam", 1.0, "en-us")


@pytest.mark.parametrize("seconds, level, slow", [(0.0, "INFO", False), (3.0, "WARNING", True)])
def test_the_kokoro_runner_logs_each_pieces_render_time_against_its_speech(caplog, monkeypatch, seconds, level, slow):
    """Logged at the agent's normal level so pieces slower than speech (audible pauses) show up."""
    class FakeKokoro:
        def create(self, text, voice, speed, lang):
            clock[0] += seconds
            return np.zeros(speech.KOKORO_RATE * 2, np.float32), speech.KOKORO_RATE

    clock = [100.0]
    monkeypatch.setattr(speech.time, "perf_counter", lambda: clock[0])
    runner = speech.KokoroRunner()
    runner._kokoro = FakeKokoro()
    with caplog.at_level("INFO", logger="jarvis.voice.speech"):
        runner.run(json.dumps({"text": "with highs around eighteen degrees.", "voice": "bm_george",
                               "speed": 1.0}).encode())
    [record] = [r for r in caplog.records if r.name == "jarvis.voice.speech"]
    assert record.levelname == level
    message = record.getMessage()
    assert message.startswith(f"Kokoro rendered 5 words in {seconds * 1000:.0f} ms for 2.0 s of speech "
                              f"({seconds / 2:.2f}x real time)")
    assert ("slower than speaking it" in message) == slow


def test_kokoro_loads_the_chosen_model_or_the_first_fallback_there_is(tmp_path):
    chosen = tmp_path / "custom.onnx"
    full, small = tmp_path / "kokoro-v1.0.onnx", tmp_path / "kokoro-v1.0.int8.onnx"
    assert speech.kokoro_model(chosen, (full, small)) == full  # none present: the loader's error names the full model
    small.touch()
    assert speech.kokoro_model(chosen, (full, small)) == small
    full.touch()
    assert speech.kokoro_model(chosen, (full, small)) == full
    chosen.touch()
    assert speech.kokoro_model(chosen, (full, small)) == chosen


def test_the_full_kokoro_model_is_the_default():
    """The 8-bit model rendered 30-60% slower on an M2 Air, slower than speech under load."""
    from jarvis import config

    if not os.environ.get("JARVIS_TTS_MODEL"):
        assert config.TTS_MODEL.name == "kokoro-v1.0.onnx"
    assert config.TTS_MODEL_FALLBACKS[0].name == "kokoro-v1.0.onnx"


@pytest.mark.parametrize("setting, cores, threads", [
    ("", 4, 3),  # M1/M2: leave a performance core for the call
    ("", 2, 2), ("", 1, 2),  # never fewer than 2
    ("", 12, 4),  # Pro/Max: more didn't help
    ("6", 4, 6), ("0", 4, 0),  # set; 0 = ONNX Runtime's default
    ("lots", 4, 3),  # not a number: treated as empty
])
def test_kokoro_threads(setting, cores, threads):
    assert speech.kokoro_threads(setting, cores) == threads


def test_performance_cores_counts_something():
    assert speech.performance_cores() >= 1


def test_both_runners_register_once(monkeypatch):
    monkeypatch.setattr(_InferenceRunner, "registered_runners", {})
    speech.register_runners()
    speech.register_runners()  # a spawned process re-importing the agent module
    assert _InferenceRunner.registered_runners == {speech.WHISPER_METHOD: speech.WhisperRunner,
                                                   speech.KOKORO_METHOD: speech.KokoroRunner}


def fake_mlx(monkeypatch) -> list[int]:
    limits: list[int] = []
    core = types.SimpleNamespace(set_cache_limit=limits.append)
    monkeypatch.setitem(sys.modules, "mlx", types.SimpleNamespace(core=core))
    monkeypatch.setitem(sys.modules, "mlx.core", core)
    return limits


def test_the_whisper_runner_caps_mlx_cache_before_it_loads(monkeypatch, mlx):
    """MLX keeps freed GPU buffers for reuse; uncapped, Whisper held 0.7 GB after one clip."""
    order = []
    monkeypatch.setattr(speech, "limit_mlx_cache", lambda: order.append("cap"))
    monkeypatch.setattr(speech, "transcribe", lambda audio: order.append("load") or {"text": ""})
    speech.WhisperRunner().initialize()
    assert order == ["cap", "load"]


def test_the_mlx_cache_cap_is_in_mb_and_minus_one_leaves_it(monkeypatch):
    limits = fake_mlx(monkeypatch)
    speech.limit_mlx_cache(256)
    speech.limit_mlx_cache(0)  # freed after every clip
    speech.limit_mlx_cache(-1)  # MLX's own default
    assert limits == [256 * 2**20, 0]


def test_without_mlx_the_cap_only_warns(monkeypatch, caplog):
    monkeypatch.setitem(sys.modules, "mlx.core", None)  # import raises, as without a Metal device
    speech.limit_mlx_cache(64)
    assert "couldn't cap MLX's cache at 64 MB" in caplog.text


@pytest.mark.parametrize("arena", [False, True])
def test_kokoro_session_sets_the_memory_arena(monkeypatch, tmp_path, arena):
    """Without the arena, a long sentence's buffers are freed after it rather than kept."""
    made = fake_session(monkeypatch)
    speech.kokoro_session(tmp_path / "kokoro.onnx", 3, arena=arena)
    assert {k: made[k] for k in ("path", "arena", "providers")} == {
        "path": str(tmp_path / "kokoro.onnx"), "arena": arena, "providers": ["CPUExecutionProvider"]}


def fake_session(monkeypatch) -> dict:
    import onnxruntime as ort

    made = {}
    monkeypatch.setattr(ort, "InferenceSession", lambda path, options, providers: made.update(
        path=path, arena=options.enable_cpu_mem_arena, providers=providers, threads=options.intra_op_num_threads,
        spinning=options.get_session_config_entry("session.intra_op.allow_spinning")))
    return made


@pytest.mark.parametrize("threads", [3, 0])  # 0 is ONNX Runtime's default
def test_kokoro_session_limits_threads_and_stops_them_spinning(monkeypatch, tmp_path, threads):
    """Spinning threads kept cores busy between sentences, starving the call's own process."""
    made = fake_session(monkeypatch)
    speech.kokoro_session(tmp_path / "kokoro.onnx", threads)
    assert made["threads"] == threads and made["spinning"] == "0"
