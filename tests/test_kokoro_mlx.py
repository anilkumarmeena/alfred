"""Tests for alfred.voice.kokoro_mlx and the Kokoro runner's choice of engine: the same front end, the model on MLX."""

import json
import sys
import types

import numpy as np
import pytest
from kokoro_onnx.tokenizer import DEFAULT_VOCAB

from alfred.voice import kokoro_mlx, speech


class FakeModel:
    """Stands in for mlx-audio's Kokoro model: half a second of audio per phoneme, the calls kept."""
    vocab = DEFAULT_VOCAB

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple, float]] = []

    def __call__(self, phonemes, style, speed):
        self.calls.append((phonemes, style.shape, speed))
        return np.full(len(phonemes) * 12000, 0.1, np.float32)


@pytest.fixture
def fake_mlx(monkeypatch):
    core = types.SimpleNamespace(array=np.asarray, set_cache_limit=lambda n: None)
    monkeypatch.setitem(sys.modules, "mlx", types.SimpleNamespace(core=core))
    monkeypatch.setitem(sys.modules, "mlx.core", core)


@pytest.fixture
def voices(tmp_path):
    path = tmp_path / "voices.npz"
    np.savez(path, bm_george=np.zeros((510, 1, 256), np.float32))
    return path


def test_the_model_gets_the_phonemes_kokoro_onnx_made_and_the_voices_style(fake_mlx, voices):
    model = FakeModel()
    kokoro = kokoro_mlx.MlxKokoro(model, voices)
    audio, rate = kokoro.create("həlˈoʊ", voice="bm_george", speed=1.05, is_phonemes=True, trim=False)
    assert model.calls == [("həlˈoʊ", (1, 256), 1.05)]  # the ids turned back into the same phonemes
    assert rate == 24000 and len(audio) == 6 * 12000


def test_long_text_is_cut_into_short_windows(fake_mlx, voices):
    """The working memory grows with the window (about 1.3 GB at 18 words), so no pass is longer than WINDOW."""
    model = FakeModel()
    sentence = "ðə bˈɪld fˈɪnɪʃt, ənd ˈɔːl ðə tˈɛsts pˈæst. "
    kokoro_mlx.MlxKokoro(model, voices).create(sentence * 12, voice="bm_george", is_phonemes=True)
    assert len(model.calls) > 1 and all(len(p) <= kokoro_mlx.WINDOW for p, _, _ in model.calls)


def test_weights_are_pinned_and_not_fetched_while_speaking(monkeypatch):
    calls = []
    hub = types.SimpleNamespace(snapshot_download=lambda *a, **k: calls.append((a, k)) or "/cache/kokoro")
    monkeypatch.setitem(sys.modules, "huggingface_hub", hub)
    assert str(kokoro_mlx.weights()) == "/cache/kokoro"
    kokoro_mlx.weights(download=True)
    assert [k["local_files_only"] for _, k in calls] == [True, False]
    assert all(a == (kokoro_mlx.REPO,) and k["revision"] == kokoro_mlx.REVISION for a, k in calls)


@pytest.fixture
def runner(monkeypatch):
    monkeypatch.setattr(speech.preferences, "tts_voice", lambda: "bm_george")
    monkeypatch.setattr(speech.preferences, "tts_speed", lambda: 1.0)
    monkeypatch.setattr(speech, "limit_mlx_cache", lambda: None)
    monkeypatch.setattr(speech.platform, "APPLE_SILICON", True)
    onnx = types.SimpleNamespace(create=lambda *a, **k: (np.zeros(10, np.float32), 24000), engine="onnx")
    monkeypatch.setattr(speech.KokoroRunner, "_on_onnx", lambda self: (onnx, "onnx"))
    return speech.KokoroRunner()


def test_kokoro_runs_on_mlx_and_shares_whispers_gpu_lock(monkeypatch, runner):
    gpu = types.SimpleNamespace(create=lambda *a, **k: (np.zeros(10, np.float32), 24000), engine="mlx")
    monkeypatch.setattr(speech, "TTS_ENGINE", "mlx")
    monkeypatch.setattr(kokoro_mlx.MlxKokoro, "load", classmethod(lambda cls, voices: gpu))
    runner.initialize()
    assert runner._kokoro.engine == "mlx" and runner._lock is speech.MLX_LOCK
    pcm = runner.run(json.dumps({"text": "Hi.", "voice": "bm_george", "speed": 1.0}).encode())
    assert len(pcm) == 20


def test_kokoro_falls_back_to_onnx_when_mlx_cant_load(monkeypatch, runner, caplog):
    def missing(cls, voices):
        raise FileNotFoundError("weights not downloaded")

    monkeypatch.setattr(speech, "TTS_ENGINE", "mlx")
    monkeypatch.setattr(kokoro_mlx.MlxKokoro, "load", classmethod(missing))
    runner.initialize()
    assert runner._kokoro.engine == "onnx" and runner._lock is not speech.MLX_LOCK
    assert "couldn't load on MLX" in caplog.text


@pytest.mark.parametrize("engine, apple", [("onnx", True), ("mlx", False)])
def test_kokoro_stays_on_onnx_when_set_or_off_apple_silicon(monkeypatch, runner, engine, apple):
    monkeypatch.setattr(speech, "TTS_ENGINE", engine)
    monkeypatch.setattr(speech.platform, "APPLE_SILICON", apple)
    monkeypatch.setattr(kokoro_mlx.MlxKokoro, "load", classmethod(lambda cls, voices: pytest.fail("loaded MLX")))
    runner.initialize()
    assert runner._kokoro.engine == "onnx"


def test_whisper_on_mlx_takes_the_shared_gpu_lock(monkeypatch):
    from alfred.media import whisper

    monkeypatch.setattr(whisper, "backend", lambda: whisper.MLX)
    monkeypatch.setattr(speech, "limit_mlx_cache", lambda: None)
    monkeypatch.setattr(speech, "transcribe", lambda audio: {"text": ""})
    runner = speech.WhisperRunner()
    runner.initialize()
    assert runner._lock is speech.MLX_LOCK
