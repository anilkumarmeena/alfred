"""Split text for Kokoro into sentences, cutting long ones at natural pauses.

Kokoro renders a piece whole before playing it, so a 25-word sentence means 1s+ of silence. Sentences over MAX_WORDS
are cut after , ; : and dashes, then pieces are greedily re-joined up to MAX_WORDS (fewest cuts, none under
MIN_WORDS). Never cut inside a number ("1,000", "3:30"), a date ("May 4, 2026") or an abbreviation ("e.g.,", "Jr.").
A trailing piece under MIN_WORDS stays with the previous one, so streamed pieces match what the full sentence gives.

The first piece of a reply goes sooner: any sentence of FIRST_MIN_CHARS+ goes alone, and one over FIRST_MAX_WORDS is
cut at its first pause or, with none within FIRST_MAX_WORDS, before its first joining word ("and", "because",
"which"...) after MIN_WORDS: Kokoro takes ~0.12 s a word before a piece plays, and first pieces ran to 17 words.
"""

import re

from livekit.agents.tokenize import SentenceStream, SentenceTokenizer, token_stream
from livekit.agents.tokenize.blingfire import _split_sentences

from alfred.brain import timing

MAX_WORDS = 12  # cut longer sentences; joined pieces stay within it
MIN_WORDS = 4  # no shorter piece unless the whole sentence is
MIN_SENTENCE_CHARS = 20  # matches LiveKit's tokenizer: shorter sentences merge with the next
MIN_CONTEXT_CHARS = 10
FIRST_MAX_WORDS, FIRST_MIN_CHARS = 6, 8  # reply's first piece

# , ; : before a space; a spaced hyphen; an en/em dash not inside a number
_PAUSE = re.compile(r"[,;:](?=\s)|(?<=\s)-(?=\s)|(?<!\d)[–—]|[–—](?!\d)")
# A space before a word that starts a clause: where a sentence with no pause can still be cut without a seam
_JOINING = re.compile(r"\s(?=(?:and|but|so|because|which|who|that|when|while|if|or|then|before|after|until|since|"
                      r"though|although|unless|where)\b)", re.IGNORECASE)
_BEFORE = re.compile(r"(\S+)$")
_AFTER = re.compile(r"^\s*(\S+)")
_NO_CUT_AFTER = {"e.g.", "i.e.", "viz.", "cf.", "vs.", "eg.", "ie."}  # "e.g., apples": comma binds forward
_NO_CUT_BEFORE = {"jr", "jr.", "sr", "sr.", "inc", "inc.", "ltd", "ltd.", "esq", "esq.", "ph.d.", "md", "m.d."}


def _words(text: str) -> int:
    return sum(any(c.isalnum() for c in word) for word in text.split())  # a spaced dash isn't a word


def _can_cut(text: str, at: re.Match) -> bool:
    if at.group() != ",":
        return True
    before = (_BEFORE.search(text, 0, at.start()) or [""])[0]
    after = (_AFTER.search(text[at.end():]) or ["", ""])[1]
    if before[-1:].isdigit() and after[:1].isdigit():  # "May 4, 2026"; number lists read as one piece
        return False
    return before.lower() not in _NO_CUT_AFTER and after.lower() not in _NO_CUT_BEFORE


def _joining_cut(text: str, max_words: int, min_words: int) -> int | None:
    """Where to cut before the first joining word with min_words to max_words words ahead of it, if any."""
    for m in _JOINING.finditer(text):
        if (n := _words(text[:m.start()])) > max_words:
            return None
        if n >= min_words:
            return m.start()
    return None


def clause_spans(text: str, max_words: int = MAX_WORDS, min_words: int = MIN_WORDS,
                 soon: bool = False) -> list[tuple[int, int]]:
    """(start, end) spans covering all of one sentence; a single span if it's short enough. `soon`: with no pause in
    its first max_words, it may be cut before a joining word instead."""
    if _words(text) <= max_words:
        return [(0, len(text))]
    cuts = [m.end() for m in _PAUSE.finditer(text) if _can_cut(text, m)]
    if soon and not any(_words(text[:cut]) <= max_words for cut in cuts):
        if (joining := _joining_cut(text, max_words, min_words)) is not None:
            cuts.append(joining)
    bounds = sorted({0, len(text), *cuts})
    chunks: list[list[int]] = []  # [start, end, words]
    for start, end in zip(bounds, bounds[1:]):
        n = _words(text[start:end])
        if chunks and (chunks[-1][2] < min_words or chunks[-1][2] + n <= max_words):
            chunks[-1][1], chunks[-1][2] = end, chunks[-1][2] + n
        else:
            chunks.append([start, end, n])
    if len(chunks) > 1 and chunks[-1][2] < min_words:  # short or still-streaming tail: rejoin
        tail = chunks.pop()
        chunks[-1][1], chunks[-1][2] = tail[1], chunks[-1][2] + tail[2]
    return [(start, end) for start, end, _ in chunks]


def split(text: str, max_words: int = MAX_WORDS, min_words: int = MIN_WORDS,
          min_chars: int = MIN_SENTENCE_CHARS, soon: bool = False) -> list[tuple[str, int, int]]:
    """LiveKit sentences with long ones cut: contiguous (text, start, end), whitespace kept. `soon` (a reply's first
    piece) lets the first sentence be cut before a joining word."""
    out = []
    for n, (sentence, start, _) in enumerate(_split_sentences(text, min_chars, retain_format=True)):
        for s, e in clause_spans(sentence, max_words, min_words, soon=soon and n == 0):
            out.append((sentence[s:e], start + s, start + e))
    return out


class ClauseTokenizer(SentenceTokenizer):
    """Sentence tokenizer for LiveKit's TTS StreamAdapter that also cuts long sentences."""

    def __init__(self, max_words: int = MAX_WORDS, min_words: int = MIN_WORDS) -> None:
        self._max_words, self._min_words = max_words, min_words

    def _split(self, text: str) -> list[tuple[str, int, int]]:
        return split(text, self._max_words, self._min_words)

    def tokenize(self, text: str, *, language: str | None = None) -> list[str]:
        return [piece.strip() for piece, _, _ in self._split(text) if piece.strip()]

    def stream(self, *, language: str | None = None) -> SentenceStream:
        return _FirstSoonerStream(self._split)


class _FirstSoonerStream(token_stream.BufferedSentenceStream):
    """Buffered stream that releases a reply's first piece sooner.

    LiveKit releases a piece once another follows it; the first such piece of FIRST_MIN_CHARS+ is the first piece."""

    def __init__(self, rest) -> None:
        super().__init__(tokenizer=self._pieces, min_token_len=FIRST_MIN_CHARS, min_ctx_len=MIN_CONTEXT_CHARS)
        self._rest = rest
        self._first = True

    def _pieces(self, text: str) -> list[tuple[str, int, int]]:
        if not self._first:
            self._min_token_len = MIN_SENTENCE_CHARS  # first piece released at its own threshold
            return self._rest(text)
        pieces = split(text, FIRST_MAX_WORDS, MIN_WORDS, FIRST_MIN_CHARS, soon=True)
        if len(pieces) > 1 and len(pieces[0][0].strip()) >= FIRST_MIN_CHARS:
            self._first = False  # release now; the rest are cut normally
            timing.piece(pieces[0][0])
        return pieces

    def flush(self) -> None:
        super().flush()
        self._first, self._min_token_len = True, FIRST_MIN_CHARS
