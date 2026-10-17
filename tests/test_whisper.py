"""Tests for jarvis.media.whisper: MLX on Apple silicon, faster-whisper elsewhere, one result shape from both."""

import sys
import types
from types import SimpleNamespace
from typing import ClassVar

import numpy as np
import pytest

from jarvis.media import transcribe, whisper
from jarvis.voice import speech


def segment(text, start=0.0, end=1.0, logprob=-0.2, no_speech=0.01, tokens=(1, 2), words=None):
    return SimpleNamespace(id=0, seek=0, start=start, end=end, text=text, tokens=list(tokens), avg_logprob=logprob,
                           compression_ratio=1.1, no_speech_prob=no_speech, words=words, temperature=0.0)


class FakeModel:
    """faster_whisper.WhisperModel: records how it was made and called."""

    made: ClassVar[list[dict]] = []
    fail_on: ClassVar[set[str]] = set()  # devices that raise on load
    segments: ClassVar[list] = []
    language = "en"

    def __init__(self, name, device, compute_type, cpu_threads):
        if device in self.fail_on:
            raise RuntimeError("Library libcublas.so.12 is not found")
        self.made.append({"name": name, "device": device, "compute_type": compute_type, "threads": cpu_threads})
        self.calls: list[dict] = []

    def transcribe(self, audio, **options):
        self.calls.append(options)
        return iter(self.segments), SimpleNamespace(language=self.language)


@pytest.fixture
def faster(monkeypatch):
    """faster-whisper backend on the CPU, with a fake model."""
    FakeModel.made, FakeModel.fail_on, FakeModel.segments, FakeModel.language = [], set(), [], "en"
    downloads: list[str] = []
    utils = types.SimpleNamespace(_MODELS={"small.en": "Systran/faster-whisper-small.en",
                                           "small": "Systran/faster-whisper-small"})
    module = types.SimpleNamespace(WhisperModel=FakeModel, download_model=downloads.append, utils=utils)
    monkeypatch.setitem(sys.modules, "faster_whisper", module)
    monkeypatch.setitem(sys.modules, "faster_whisper.utils", utils)
    monkeypatch.setattr(whisper, "backend", lambda: whisper.FASTER)
    monkeypatch.setattr(whisper, "_cuda", lambda: False)
    monkeypatch.setattr(whisper, "STT_DEVICE", "")
    monkeypatch.setattr(whisper, "_models", {})
    monkeypatch.setattr(whisper, "_gpu_failed", False)
    module.downloads = downloads
    return module


# ── Which backend ───────────────────────────────────────────────

@pytest.mark.parametrize("silicon, installed, chosen", [
    (True, True, whisper.MLX), (True, False, whisper.FASTER), (False, True, whisper.FASTER)])
def test_mlx_only_on_apple_silicon_with_mlx_whisper_installed(monkeypatch, silicon, installed, chosen):
    monkeypatch.setattr(whisper.platform, "APPLE_SILICON", silicon)
    monkeypatch.setattr(whisper.importlib.util, "find_spec", lambda name: object() if installed else None)
    whisper.backend.cache_clear()
    try:
        assert whisper.backend() == chosen
    finally:
        whisper.backend.cache_clear()


def test_a_fake_mlx_whisper_counts_as_installed(monkeypatch):
    """A module put in sys.modules has no spec; find_spec raises instead of answering."""
    monkeypatch.setattr(whisper.platform, "APPLE_SILICON", True)
    monkeypatch.setitem(sys.modules, "mlx_whisper", types.SimpleNamespace())
    whisper.backend.cache_clear()
    try:
        assert whisper.backend() == whisper.MLX
    finally:
        whisper.backend.cache_clear()


def test_the_speech_provider_names_the_backend(faster):
    assert speech.WhisperSTT().provider == "faster-whisper"


# ── Device and model ────────────────────────────────────────────

@pytest.mark.parametrize("setting, cuda, failed, device", [
    ("", True, False, "cuda"), ("", False, False, "cpu"), ("", True, True, "cpu"), ("auto", True, False, "cuda"),
    ("cpu", True, False, "cpu"), ("cuda", False, False, "cuda"), ("tpu", False, False, "cpu")])
def test_the_device_is_cuda_when_usable_unless_set(faster, monkeypatch, setting, cuda, failed, device):
    monkeypatch.setattr(whisper, "STT_DEVICE", setting)
    monkeypatch.setattr(whisper, "_cuda", lambda: cuda)
    monkeypatch.setattr(whisper, "_gpu_failed", failed)
    assert whisper.device() == device


def test_cpu_defaults_to_small_models_and_a_gpu_to_large_v3_turbo(faster, monkeypatch):
    """The large encoder takes seconds a clip on a CPU; live voice is English, recordings any language."""
    assert whisper.model("") == "small.en" and whisper.model("", multilingual=True) == "small"
    monkeypatch.setattr(whisper, "_cuda", lambda: True)
    assert whisper.model("") == whisper.model("", multilingual=True) == "large-v3-turbo"


def test_a_chosen_model_is_kept_unless_it_is_mlx_weights(faster):
    assert whisper.model("base.en") == "base.en" and whisper.model("/models/whisper-ct2") == "/models/whisper-ct2"
    assert whisper.model("mlx-community/whisper-large-v3-turbo-q4") == "small.en"


def test_mlx_runs_the_model_it_is_given(monkeypatch):
    monkeypatch.setattr(whisper, "backend", lambda: whisper.MLX)
    assert whisper.model("mlx-community/whisper-large-v3-turbo-q4", multilingual=True) == \
        "mlx-community/whisper-large-v3-turbo-q4"


def test_threads_are_physical_cores():
    assert whisper.threads() >= 1


# ── Loading ─────────────────────────────────────────────────────

def test_the_model_loads_once_on_the_cpu_as_int8(faster):
    first = whisper.loaded("")
    assert whisper.loaded("") is first
    assert FakeModel.made == [{"name": "small.en", "device": "cpu", "compute_type": "int8",
                               "threads": whisper.threads()}]


def test_a_gpu_runs_float16_and_falls_back_to_the_cpu_when_it_fails(faster, monkeypatch, caplog):
    monkeypatch.setattr(whisper, "_cuda", lambda: True)
    whisper.loaded("", multilingual=True)
    assert FakeModel.made[-1] | {"threads": 0} == {"name": "large-v3-turbo", "device": "cuda",
                                                    "compute_type": "float16", "threads": 0}
    FakeModel.fail_on = {"cuda"}  # CUDA driver but no cuBLAS
    whisper.loaded("")
    assert FakeModel.made[-1]["device"] == "cpu" and FakeModel.made[-1]["name"] == "small.en"
    assert "couldn't run on the GPU" in caplog.text and whisper.device() == "cpu"


def test_a_gpu_that_was_asked_for_fails_loudly(faster, monkeypatch):
    monkeypatch.setattr(whisper, "STT_DEVICE", "cuda")
    FakeModel.fail_on = {"cuda"}
    with pytest.raises(RuntimeError, match="libcublas"):
        whisper.loaded("")


# ── Results ─────────────────────────────────────────────────────

def test_faster_whisper_segments_come_back_shaped_like_mlx_whispers(faster):
    words = [SimpleNamespace(word=" Hi", start=0.0, end=0.4, probability=0.9)]
    FakeModel.segments = [segment(" Hi there.", logprob=-0.3, words=words), segment(" Bye.", 1.0, 1.5)]
    result = whisper.run(np.zeros(160, np.float32), "", language="en", initial_prompt="JARVIS.",
                         condition_on_previous_text=False, word_timestamps=True, verbose=None)
    assert result["text"] == " Hi there. Bye." and result["language"] == "en"
    assert result["segments"][0] == {
        "id": 0, "seek": 0, "start": 0.0, "end": 1.0, "text": " Hi there.", "tokens": [1, 2], "temperature": 0.0,
        "avg_logprob": -0.3, "compression_ratio": 1.1, "no_speech_prob": 0.01,
        "words": [{"word": " Hi", "start": 0.0, "end": 0.4, "probability": 0.9}]}
    assert "words" not in result["segments"][1]
    assert whisper.loaded("").calls[-1] == {"language": "en", "beam_size": 1, "initial_prompt": "JARVIS.",
                                             "condition_on_previous_text": False, "word_timestamps": True}


def test_live_voice_scores_faster_whisper_as_it_does_mlx(faster):
    """The speech gate reads the same no_speech and token-weighted logprob from either backend."""
    FakeModel.segments = [segment(" One two three.", no_speech=0.01, logprob=-0.2, tokens=[1, 2, 3]),
                          segment(" Four.", no_speech=0.3, logprob=-1.0, tokens=[4])]
    heard = speech.transcribe(np.zeros(160, np.float32), prompt="JARVIS, AI Armory.")
    assert heard["text"] == "One two three. Four." and heard["no_speech"] == 0.01
    assert heard["avg_logprob"] == pytest.approx(-0.4)
    call = whisper.loaded(speech.STT_MODEL).calls[-1]
    assert call["initial_prompt"] == "JARVIS, AI Armory." and call["condition_on_previous_text"] is False
    FakeModel.segments = []
    assert speech.transcribe(np.zeros(160, np.float32)) == {"text": "", "no_speech": None, "avg_logprob": None}


def test_recordings_use_a_multilingual_model_and_report_the_language(faster, monkeypatch):
    monkeypatch.setattr(transcribe, "MODEL", "")
    FakeModel.segments, FakeModel.language = [segment(" Namaste.", 2.0, 3.0)], "hi"
    assert transcribe._whisper(np.zeros(160, np.float32), "") == ([(2.0, " Namaste.")], "hi")
    assert FakeModel.made[-1]["name"] == "small" and whisper.loaded("", True).calls[-1]["language"] is None


def test_the_shared_process_loads_faster_whisper_without_mlx(faster, monkeypatch, caplog):
    capped = []
    monkeypatch.setattr(speech, "limit_mlx_cache", lambda: capped.append(1))
    caplog.set_level("INFO")
    runner = speech.WhisperRunner()
    runner.initialize()
    assert not capped and "loaded on faster-whisper (cpu)" in caplog.text
    FakeModel.segments = [segment(" Hello.")]
    assert b'"text": "Hello."' in runner.run(speech.whisper_request(np.zeros(160, np.float32)))


# ── Languages and model files ───────────────────────────────────

def test_the_language_table_matches_whispers():
    tokenizer = pytest.importorskip("mlx_whisper.tokenizer")
    assert whisper.LANGUAGES == tokenizer.LANGUAGES and whisper.TO_LANGUAGE_CODE == tokenizer.TO_LANGUAGE_CODE


def test_recording_languages_resolve_without_mlx():
    assert transcribe._language("hindi") == transcribe._language("hi") == "hi" and transcribe._language("") == ""
    assert transcribe._language("mandarin") == "zh" and transcribe._language_name("yue") == "Cantonese"
    with pytest.raises(Exception, match="doesn't know the language 'klingon'"):
        transcribe._language("klingon")


def test_a_cached_model_needs_no_network(faster, monkeypatch, tmp_path):
    monkeypatch.setenv("HF_HUB_CACHE", str(tmp_path))
    assert not whisper.cached("")
    (tmp_path / "models--Systran--faster-whisper-small.en" / "snapshots").mkdir(parents=True)
    assert whisper.cached("") and not whisper.cached("", multilingual=True)
    assert whisper.cached(str(tmp_path))  # a local model folder
    monkeypatch.setattr(transcribe, "MODEL", "small.en")
    assert transcribe._environment()["HF_HUB_OFFLINE"] == "1"


def test_make_models_downloads_the_live_and_recordings_models(faster, monkeypatch):
    monkeypatch.setattr(whisper, "STT_MODEL", "")
    monkeypatch.setattr(whisper, "TRANSCRIBE_MODEL", "")
    whisper.download()
    assert faster.downloads == ["small.en", "small"]
    faster.downloads.clear()
    monkeypatch.setattr(whisper, "TRANSCRIBE_MODEL", "small.en")  # one model for both: fetched once
    whisper.download()
    assert faster.downloads == ["small.en"]


def test_make_models_on_apple_silicon_fetches_the_mlx_repo_once(monkeypatch):
    fetched = []
    monkeypatch.setattr(whisper, "backend", lambda: whisper.MLX)
    monkeypatch.setitem(sys.modules, "huggingface_hub", types.SimpleNamespace(snapshot_download=fetched.append))
    monkeypatch.setattr(whisper, "STT_MODEL", "mlx-community/whisper-large-v3-turbo-q4")
    monkeypatch.setattr(whisper, "TRANSCRIBE_MODEL", "mlx-community/whisper-large-v3-turbo-q4")
    whisper.download()
    assert fetched == ["mlx-community/whisper-large-v3-turbo-q4"]
