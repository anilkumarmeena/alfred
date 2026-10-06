"""What Kokoro is handed to say, one piece at a time: sentences, and long sentences cut at their commas.

Kokoro renders each piece whole before any of it plays, so a 25-word sentence used to be a second or more of
silence. A sentence of more than MAX_WORDS words is cut after its commas, semicolons, colons and dashes, and the
pieces are joined again from the front for as long as they fit in MAX_WORDS: each cut falls where a speaker pauses
anyway, there are no more cuts than needed (each piece is said with its own intonation), and no piece is left under
MIN_WORDS. Never cut: inside a number ("1,000", "3:30", "10–20"), between a date's day and year ("May 4, 2026"), or
at an abbreviation's comma ("e.g., ...", "Smith, Jr."). A long stretch with no pause in it stays whole.

While the brain is still streaming, a sentence's last piece can still grow: a trailing piece under MIN_WORDS is held
with the one before it, so LiveKit lets a piece go only once the next has MIN_WORDS words. Joining from the front
starts afresh at each piece, so the pieces let go along the way are the ones the whole sentence would have given.

A reply's first piece is what the user waits for, so it goes sooner: a sentence of FIRST_MIN_CHARS or more goes alone
("Half past three." isn't held for the next one), and a sentence over FIRST_MAX_WORDS is cut at its first pause.
After it, pieces are cut as above.
"""

import re

from livekit.agents.tokenize import SentenceStream, SentenceTokenizer, token_stream
from livekit.agents.tokenize.blingfire import _split_sentences

from jarvis.voice import timing

MAX_WORDS = 12  # a longer sentence is cut, and joined pieces stay within it
MIN_WORDS = 4  # no piece shorter, unless the whole sentence is
MIN_SENTENCE_CHARS = 20  # as LiveKit's own sentence tokenizer: shorter sentences go with the next
MIN_CONTEXT_CHARS = 10
FIRST_MAX_WORDS, FIRST_MIN_CHARS = 6, 8  # for a reply's first piece

# After , ; : followed by a space; after a spaced hyphen; after an en or em dash that isn't inside a number
_PAUSE = re.compile(r"[,;:](?=\s)|(?<=\s)-(?=\s)|(?<!\d)[–—]|[–—](?!\d)")
_BEFORE = re.compile(r"(\S+)$")
_AFTER = re.compile(r"^\s*(\S+)")
_NO_CUT_AFTER = {"e.g.", "i.e.", "viz.", "cf.", "vs.", "eg.", "ie."}  # "e.g., apples": the comma belongs with what follows
_NO_CUT_BEFORE = {"jr", "jr.", "sr", "sr.", "inc", "inc.", "ltd", "ltd.", "esq", "esq.", "ph.d.", "md", "m.d."}


def _words(text: str) -> int:
    return sum(any(c.isalnum() for c in word) for word in text.split())  # a spaced dash isn't a word


def _can_cut(text: str, at: re.Match) -> bool:
    if at.group() != ",":
        return True
    before = (_BEFORE.search(text, 0, at.start()) or [""])[0]
    after = (_AFTER.search(text[at.end():]) or ["", ""])[1]
    if before[-1:].isdigit() and after[:1].isdigit():  # "May 4, 2026"; a list of numbers reads as one piece anyway
        return False
    return before.lower() not in _NO_CUT_AFTER and after.lower() not in _NO_CUT_BEFORE


def clause_spans(text: str, max_words: int = MAX_WORDS, min_words: int = MIN_WORDS) -> list[tuple[int, int]]:
    """Where to cut one sentence: (start, end) spans covering all of `text`, a single one when it's short enough."""
    if _words(text) <= max_words:
        return [(0, len(text))]
    cuts = [m.end() for m in _PAUSE.finditer(text) if _can_cut(text, m)]
    bounds = sorted({0, len(text), *cuts})
    chunks: list[list[int]] = []  # [start, end, words]
    for start, end in zip(bounds, bounds[1:]):
        n = _words(text[start:end])
        if chunks and (chunks[-1][2] < min_words or chunks[-1][2] + n <= max_words):
            chunks[-1][1], chunks[-1][2] = end, chunks[-1][2] + n
        else:
            chunks.append([start, end, n])
    if len(chunks) > 1 and chunks[-1][2] < min_words:  # a short tail, or one still being written: join it back
        tail = chunks.pop()
        chunks[-1][1], chunks[-1][2] = tail[1], chunks[-1][2] + tail[2]
    return [(start, end) for start, end, _ in chunks]


def split(text: str, max_words: int = MAX_WORDS, min_words: int = MIN_WORDS,
          min_chars: int = MIN_SENTENCE_CHARS) -> list[tuple[str, int, int]]:
    """LiveKit's sentences, the long ones cut at their pauses: (text, start, end) with whitespace kept, end to end."""
    out = []
    for sentence, start, _ in _split_sentences(text, min_chars, retain_format=True):
        for s, e in clause_spans(sentence, max_words, min_words):
            out.append((sentence[s:e], start + s, start + e))
    return out


class ClauseTokenizer(SentenceTokenizer):
    """A sentence tokenizer for LiveKit's TTS StreamAdapter that also cuts long sentences (see the module's doc)."""

    def __init__(self, max_words: int = MAX_WORDS, min_words: int = MIN_WORDS) -> None:
        self._max_words, self._min_words = max_words, min_words

    def _split(self, text: str) -> list[tuple[str, int, int]]:
        return split(text, self._max_words, self._min_words)

    def tokenize(self, text: str, *, language: str | None = None) -> list[str]:
        return [piece.strip() for piece, _, _ in self._split(text) if piece.strip()]

    def stream(self, *, language: str | None = None) -> SentenceStream:
        return _FirstSoonerStream(self._split)


class _FirstSoonerStream(token_stream.BufferedSentenceStream):
    """LiveKit's buffered stream, letting a reply's first piece go sooner (see the module's doc). LiveKit lets a piece
    go once the tokenizer finds another after it; the first one found that way, long enough, is the first piece."""

    def __init__(self, rest) -> None:
        super().__init__(tokenizer=self._pieces, min_token_len=FIRST_MIN_CHARS, min_ctx_len=MIN_CONTEXT_CHARS)
        self._rest = rest
        self._first = True

    def _pieces(self, text: str) -> list[tuple[str, int, int]]:
        if not self._first:
            self._min_token_len = MIN_SENTENCE_CHARS  # the first piece has gone at its own length
            return self._rest(text)
        pieces = split(text, FIRST_MAX_WORDS, MIN_WORDS, FIRST_MIN_CHARS)
        if len(pieces) > 1 and len(pieces[0][0].strip()) >= FIRST_MIN_CHARS:
            self._first = False  # this one goes now; the rest are cut as usual
            timing.piece(pieces[0][0])
        return pieces

    def flush(self) -> None:
        super().flush()
        self._first, self._min_token_len = True, FIRST_MIN_CHARS
