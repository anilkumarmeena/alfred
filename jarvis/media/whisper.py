"""Whisper backends: MLX Whisper on Apple silicon (Apple GPU), faster-whisper (CTranslate2) everywhere else.

  run(audio, chosen, multilingual, **options)  -> Whisper's result, the same shape from both backends:
      {text, language, segments: [{start, end, text, tokens, avg_logprob, no_speech_prob, compression_ratio, ...}]}
  model(chosen, multilingual)                  -> the model the backend runs: `chosen`, else the backend's default

faster-whisper runs on CUDA (float16) when an NVIDIA GPU is usable, else on the CPU (int8, one thread per physical
core). On the CPU every clip pays for Whisper's encoder over a whole 30 s window: several seconds for the large and
distil-large models, about a second for small. So live voice defaults to small.en there and recordings to small
(it knows every language); a GPU runs large-v3-turbo, as Apple silicon does.

  uv run python -m jarvis.media.whisper    download this machine's Whisper models (make models)
"""

import functools
import importlib.util
import logging
import os
import threading
from pathlib import Path
from typing import Any

import numpy as np

from jarvis.config import (
    STT_DEVICE,
    STT_MODEL,
    TRANSCRIBE_MODEL,
    WHISPER_CPU_FILES_MODEL,
    WHISPER_CPU_MODEL,
    WHISPER_GPU_MODEL,
)
from jarvis.core import platform

log = logging.getLogger("jarvis.whisper")

MLX, FASTER = "mlx-whisper", "faster-whisper"
GPU_MODEL = WHISPER_GPU_MODEL
CPU_MODEL = WHISPER_CPU_MODEL  # live voice: English only, ~1 s a clip on 4-8 cores
CPU_FILES_MODEL = WHISPER_CPU_FILES_MODEL  # recordings: any language
COMPUTE = {"cuda": "float16", "cpu": "int8"}

LANGUAGES = {
    "en": "english", "zh": "chinese", "de": "german", "es": "spanish", "ru": "russian", "ko": "korean", "fr": "french",
    "ja": "japanese", "pt": "portuguese", "tr": "turkish", "pl": "polish", "ca": "catalan", "nl": "dutch",
    "ar": "arabic", "sv": "swedish", "it": "italian", "id": "indonesian", "hi": "hindi", "fi": "finnish",
    "vi": "vietnamese", "he": "hebrew", "uk": "ukrainian", "el": "greek", "ms": "malay", "cs": "czech",
    "ro": "romanian", "da": "danish", "hu": "hungarian", "ta": "tamil", "no": "norwegian", "th": "thai", "ur": "urdu",
    "hr": "croatian", "bg": "bulgarian", "lt": "lithuanian", "la": "latin", "mi": "maori", "ml": "malayalam",
    "cy": "welsh", "sk": "slovak", "te": "telugu", "fa": "persian", "lv": "latvian", "bn": "bengali", "sr": "serbian",
    "az": "azerbaijani", "sl": "slovenian", "kn": "kannada", "et": "estonian", "mk": "macedonian", "br": "breton",
    "eu": "basque", "is": "icelandic", "hy": "armenian", "ne": "nepali", "mn": "mongolian", "bs": "bosnian",
    "kk": "kazakh", "sq": "albanian", "sw": "swahili", "gl": "galician", "mr": "marathi", "pa": "punjabi",
    "si": "sinhala", "km": "khmer", "sn": "shona", "yo": "yoruba", "so": "somali", "af": "afrikaans", "oc": "occitan",
    "ka": "georgian", "be": "belarusian", "tg": "tajik", "sd": "sindhi", "gu": "gujarati", "am": "amharic",
    "yi": "yiddish", "lo": "lao", "uz": "uzbek", "fo": "faroese", "ht": "haitian creole", "ps": "pashto",
    "tk": "turkmen", "nn": "nynorsk", "mt": "maltese", "sa": "sanskrit", "lb": "luxembourgish", "my": "myanmar",
    "bo": "tibetan", "tl": "tagalog", "mg": "malagasy", "as": "assamese", "tt": "tatar", "haw": "hawaiian",
    "ln": "lingala", "ha": "hausa", "ba": "bashkir", "jw": "javanese", "su": "sundanese", "yue": "cantonese",
}
# Whisper's language names and aliases -> code
TO_LANGUAGE_CODE = {name: code for code, name in LANGUAGES.items()} | {
    "burmese": "my", "valencian": "ca", "flemish": "nl", "haitian": "ht", "letzeburgesch": "lb", "pushto": "ps",
    "panjabi": "pa", "moldavian": "ro", "moldovan": "ro", "sinhalese": "si", "castilian": "es", "mandarin": "zh"}


@functools.cache
def backend() -> str:
    """MLX on Apple silicon (when mlx_whisper is installed), else faster-whisper."""
    if not platform.APPLE_SILICON:
        return FASTER
    try:
        found = importlib.util.find_spec("mlx_whisper") is not None
    except ValueError:  # already imported without a spec (a test's fake)
        found = True
    return MLX if found else FASTER


@functools.cache
def _cuda() -> bool:
    try:
        import ctranslate2  # pyright: ignore[reportMissingImports]  # not on Apple silicon

        return ctranslate2.get_cuda_device_count() > 0
    except Exception:  # no CTranslate2 or no driver: CPU
        return False


_gpu_failed = False  # CUDA was found but couldn't run Whisper


def device() -> str:
    """faster-whisper's device: JARVIS_STT_DEVICE if cuda or cpu, else cuda when usable."""
    if STT_DEVICE in COMPUTE:
        return STT_DEVICE
    return "cuda" if _cuda() and not _gpu_failed else "cpu"


def threads() -> int:
    """CPU threads: one per physical core (hyperthreads don't speed up CTranslate2's matrix work)."""
    import psutil

    return psutil.cpu_count(logical=False) or os.cpu_count() or 4


def model(chosen: str = STT_MODEL, multilingual: bool = False) -> str:
    """The model the backend runs: `chosen`, else its default for this device (multilingual for recordings)."""
    if backend() == MLX:
        return chosen
    if chosen and not chosen.startswith("mlx-community/"):  # MLX weights only load on Apple silicon
        return chosen
    if device() == "cuda":
        return GPU_MODEL
    return CPU_FILES_MODEL if multilingual else CPU_MODEL


def run(audio: np.ndarray, chosen: str = STT_MODEL, multilingual: bool = False, **options) -> dict:
    """Transcribe 16 kHz mono float32 audio. `options` are mlx_whisper.transcribe's (faster-whisper takes language,
    initial_prompt, condition_on_previous_text, word_timestamps and verbose)."""
    if backend() == MLX:
        import mlx_whisper  # pyright: ignore[reportMissingImports]  # Apple silicon only

        return mlx_whisper.transcribe(audio, path_or_hf_repo=chosen, **options)
    return _faster(audio, chosen, multilingual, **options)


# ── faster-whisper ──────────────────────────────────────────────

_models: dict[tuple[str, bool], Any] = {}
_loading = threading.Lock()


def loaded(chosen: str = STT_MODEL, multilingual: bool = False):
    """The faster-whisper model, loaded once per process."""
    with _loading:
        if (chosen, multilingual) not in _models:
            _models[chosen, multilingual] = _load(chosen, multilingual)
        return _models[chosen, multilingual]


def _load(chosen: str, multilingual: bool):
    """Load on the GPU if usable, else (or if it fails there) on the CPU."""
    global _gpu_failed
    from faster_whisper import WhisperModel  # pyright: ignore[reportMissingImports]  # not on Apple silicon

    if STT_DEVICE and STT_DEVICE not in COMPUTE and STT_DEVICE != "auto":
        log.warning("JARVIS_STT_DEVICE=%r isn't cuda, cpu or auto; choosing myself", STT_DEVICE)
    if chosen.startswith("mlx-community/"):
        log.warning("%s is an MLX model, which runs only on Apple silicon; using faster-whisper's default", chosen)
    where, name = device(), model(chosen, multilingual)
    try:
        whisper = WhisperModel(name, device=where, compute_type=COMPUTE[where], cpu_threads=threads())
        if where == "cuda":  # cuBLAS/cuDNN load on first use
            list(whisper.transcribe(np.zeros(16000, np.float32), language="en", beam_size=1)[0])
    except Exception as e:
        if where != "cuda" or STT_DEVICE == "cuda":
            raise
        log.warning("faster-whisper couldn't run on the GPU (%r); using the CPU. Set JARVIS_STT_DEVICE=cpu to skip "
                    "trying", e)
        _gpu_failed = True
        return _load(chosen, multilingual)
    log.info("Whisper %s loaded on faster-whisper (%s, %s%s)", name, where, COMPUTE[where],
             f", {threads()} threads" if where == "cpu" else "")
    return whisper


def _faster(audio: np.ndarray, chosen: str, multilingual: bool, language: str | None = None,
            initial_prompt: str | None = None, condition_on_previous_text: bool = True, word_timestamps: bool = False,
            verbose: bool | None = None) -> dict:
    """faster-whisper, decoded greedily as mlx_whisper does, as an mlx_whisper-shaped result."""
    segments, info = loaded(chosen, multilingual).transcribe(
        audio, language=language, beam_size=1, initial_prompt=initial_prompt,
        condition_on_previous_text=condition_on_previous_text, word_timestamps=word_timestamps)
    found = [segment(s) for s in segments]  # a generator: decoding happens here
    return {"text": "".join(s["text"] for s in found), "segments": found, "language": info.language}


def segment(s) -> dict:
    """faster-whisper Segment -> mlx_whisper segment dict."""
    said = {"id": s.id, "seek": s.seek, "start": s.start, "end": s.end, "text": s.text, "tokens": list(s.tokens),
            "temperature": s.temperature, "avg_logprob": s.avg_logprob, "compression_ratio": s.compression_ratio,
            "no_speech_prob": s.no_speech_prob}
    if s.words:
        said["words"] = [{"word": w.word, "start": w.start, "end": w.end, "probability": w.probability}
                         for w in s.words]
    return said


# ── Model files ─────────────────────────────────────────────────

def repo(name: str) -> str:
    """Hugging Face repo of a model name ("small.en" -> Systran/faster-whisper-small.en)."""
    if backend() == MLX or "/" in name:
        return name
    from faster_whisper import utils  # pyright: ignore[reportMissingImports]

    return getattr(utils, "_MODELS", {}).get(name, name)


def cached(chosen: str, multilingual: bool = False) -> bool:
    """True if the model is a folder or in the Hugging Face cache, so loading it needs no network."""
    name = model(chosen, multilingual)
    if Path(name).is_dir():
        return True
    hub = Path(os.environ.get("HF_HUB_CACHE")
               or Path(os.environ.get("HF_HOME") or Path.home() / ".cache/huggingface") / "hub")
    return (hub / f"models--{repo(name).replace('/', '--')}" / "snapshots").is_dir()


def download() -> None:
    """Fetch the live voice and recordings models into the Hugging Face cache."""
    for name in dict.fromkeys([model(STT_MODEL), model(TRANSCRIBE_MODEL, multilingual=True)]):
        if Path(name).is_dir():
            continue
        if backend() == MLX:
            from huggingface_hub import snapshot_download

            snapshot_download(name)
        else:
            from faster_whisper import download_model  # pyright: ignore[reportMissingImports]

            print(f"Whisper {name} for faster-whisper on the {device().upper()}")
            download_model(name)


if __name__ == "__main__":
    download()
