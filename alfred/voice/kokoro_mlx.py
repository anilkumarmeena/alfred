"""Kokoro on the Apple GPU: kokoro-onnx's front end, with each window of phonemes run by mlx-audio's Kokoro model.

kokoro-onnx still turns the text into phonemes (espeak), picks the voice's style, cuts long text at its punctuation,
and trims and pauses the joins, so speech sounds the same; only the model runs on MLX, from the same voices file.
On the MacBook Air (M2) a 6-word piece rendered in 0.21 s instead of 0.64 s (0.09x real time vs 0.37x).

The model holds 327 MB of weights. Its working memory grows with the window: about 0.6 GB for 6 words and 1.3 GB for
18 (ONNX Runtime's is about 0.3 GB), so windows stay short. Voice pieces are 12 words at most (alfred.voice.clauses).
Half precision saves only the weights, and bf16 audibly changes the sound, so it stays fp32.

  uv run python -m alfred.voice.kokoro_mlx    download the weights (make models)
"""

from pathlib import Path

import numpy as np
from kokoro_onnx import Kokoro
from kokoro_onnx.chunker import split_phonemes
from kokoro_onnx.tokenizer import Tokenizer

from alfred.core import platform

# fp32 weights despite the name; pinned, as the ONNX files are (Makefile)
REPO, REVISION = "mlx-community/Kokoro-82M-bf16", "a71e4d38b236d968966a2002c4c895dbd12b1c3c"
FILES = ["config.json", "kokoro-v1_0.safetensors"]  # its voices folder isn't needed: they're in voices-v1.0.bin
WINDOW = 128  # phonemes per pass (~22 words): a voice piece fits in one, and the working memory stays under ~2 GB


def weights(download: bool = False) -> Path:
    """The weights' folder in the Hugging Face cache. Without `download` a missing one raises (no network call)."""
    from huggingface_hub import snapshot_download

    return Path(snapshot_download(REPO, revision=REVISION, allow_patterns=FILES, local_files_only=not download))


class MlxKokoro(Kokoro):
    """kokoro_onnx.Kokoro whose model runs on MLX. create() is the base class's; no ONNX session is made."""

    def __init__(self, model, voices_path: str | Path) -> None:
        # What Kokoro._setup sets that create() reads, minus the session; the tokenizer uses the MLX model's own
        # vocabulary, so the ids it makes turn back into the same phonemes for the model.
        self.mlx = model
        self.voices = np.load(voices_path)
        self.tokenizer = Tokenizer(vocab=model.vocab)
        self.has_timings = False
        vocab = self.tokenizer.vocab
        self._stops = frozenset(vocab[mark] for mark in ".!?" if mark in vocab)
        self._spaces = frozenset(vocab[mark] for mark in " " if mark in vocab)
        self._phonemes = {i: p for p, i in vocab.items()}

    @classmethod
    def load(cls, voices_path: str | Path, download: bool = False) -> "MlxKokoro":
        from mlx_audio.tts.utils import load_model  # pyright: ignore[reportMissingImports]  # Apple silicon only

        return cls(load_model(weights(download)), voices_path)

    def _split_phonemes(self, phonemes: str) -> list[str]:
        return split_phonemes(phonemes, WINDOW)

    def _infer(self, tokens: list[int], style: np.ndarray, speed: float) -> tuple[np.ndarray, None]:
        import mlx.core as mx  # pyright: ignore[reportMissingImports]

        audio = self.mlx("".join(self._phonemes[t] for t in tokens), mx.array(style), speed)
        return np.array(audio, dtype=np.float32).ravel(), None


if __name__ == "__main__":
    if platform.APPLE_SILICON:
        weights(download=True)
    else:
        print("Kokoro on MLX needs Apple silicon; ONNX Runtime runs it here")
