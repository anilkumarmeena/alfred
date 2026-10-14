"""The last check before speech: JARVIS's private notes never reach the user's ears.

The brain is told what's private (bracketed notes, <private> notes, tool guidance) and not to say it; this catches what
slips through anyway, on everything spoken (brain replies and JARVIS's own lines alike, jarvis.voice.agent):
  - [bracketed notes] and <private>…</private> notes go, as do stray tags; a Markdown link keeps its words
  - "task 230" becomes "the task"; a task branch ("jarvis/230-spoken-lookups") becomes "the branch"
  - a file path is said as its last part ("/Users/x/app/main.py" → "main.py"); URLs, dates and "and/or" are left be
  - snake_case names are said as words ("task_start" → "task start")

`Scrubber` works on a stream: it holds back only the tail a later chunk could still change (an open bracket, a word
still arriving, "the task" before its number), so speech starts as soon as it would without it.
"""

import re
from collections.abc import AsyncIterable, AsyncIterator

_LINK = re.compile(r"\[([^\[\]]+)\]\([^()\s]*\)")
_INNER_BRACKETS = re.compile(r"\[[^\[\]]*\]")
_PRIVATE = re.compile(r"<private\b[^>]*>.*?(?:</private\s*>|\Z)", re.S | re.I)
_TAG = re.compile(r"</?[A-Za-z][\w-]*(?:\s[^<>]*)?/?>")
_DETERMINERS = r"the|this|that|a|your|its|their"
# Not a count: "the task 2 weeks ago", "a task 3 times over"
_COUNTED = r"(?!\s*(?:%|(?:percent|seconds?|minutes?|hours?|days?|weeks?|months?|years?|times?|more|of)\b))"
_TASK = re.compile(rf"\b(?:({_DETERMINERS})\s+)?(task)\s*#?\d+\b{_COUNTED}", re.I)
_BRANCH = re.compile(r"(?:\b(?:(the)\s+)?(branch)(?:\s+(?:called|named))?\s+)?"
                     r"(?<![\w/])[A-Za-z][\w.-]*/\d+-[\w.-]*[\w]", re.I)
# Absolute or home paths with two parts or more, relative ones with three, or two ending in a file extension
_SEGMENT = r"[\w@+-][\w.@+-]*"
_PATH = re.compile(rf"(?<![\w/:.@-])(?:(?:~|\.{{1,2}})?/(?:{_SEGMENT}/)+{_SEGMENT}/?"
                   rf"|(?:{_SEGMENT}/){{2,}}{_SEGMENT}/?"
                   rf"|{_SEGMENT}/{_SEGMENT}\.[A-Za-z]{{1,5}}\b)")
_SITE = re.compile(r"[\w-]+(?:\.[\w-]+)*\.[A-Za-z]{2,}/")
_SNAKE = re.compile(r"\b[A-Za-z][A-Za-z0-9]*(?:_[A-Za-z0-9]+)+\b")
_SPACE_BEFORE = re.compile(r"[ \t]+(?=[.,;:!?])")
_SPACES = re.compile(r"[ \t]{2,}")

# A tail a later chunk could still change: a determiner or "task" still waiting for its number (or a number for the
# word after it: "task 2 weeks ago" is a count), "branch" for its name
_HELD_PHRASE = re.compile(rf"(?:\b(?:{_DETERMINERS})\s+)?\b(?:task\s*#?\d*\s*|branch(?:\s+(?:called|named))?\s*)$"
                          rf"|\b(?:{_DETERMINERS})\s*$", re.I)
_OPEN_TAG = re.compile(r"</?[A-Za-z][\w-]*(?:\s[^<>]*)?$|<$")
_LAST_WORD = re.compile(r"\S+$")


def _capital(word: str, like: str) -> str:
    return word[:1].upper() + word[1:] if like[:1].isupper() else word


def _task(m: re.Match) -> str:
    det, task = m[1], m[2]
    return f"{det} {task.lower()}" if det else _capital("the task", task)


def _branch(m: re.Match) -> str:
    return _capital("the branch", m[1] or m[2] or "")


def _path(m: re.Match) -> str:
    path = m[0]
    if not any(c.isalpha() for c in path) or _SITE.match(path):  # 13/10/2026, example.com/docs/a
        return path
    return path.rstrip("/").rsplit("/", 1)[-1] or path


def scrub(text: str) -> str:
    """`text` without private notes and internal markers, fit to say."""
    text = _PRIVATE.sub(" ", text)
    text = _LINK.sub(r"\1", text)
    while (fewer := _INNER_BRACKETS.sub(" ", text)) != text:  # nested notes, innermost first
        text = fewer
    text = text.split("[", 1)[0].replace("]", "")  # an unclosed note runs to the end
    text = _TAG.sub("", text)
    text = _BRANCH.sub(_branch, text)
    text = _PATH.sub(_path, text)
    text = _TASK.sub(_task, text)
    text = _SNAKE.sub(lambda m: m[0].replace("_", " "), text)
    return _SPACES.sub(" ", _SPACE_BEFORE.sub("", text))


def _spans(text: str) -> list[tuple[int, int]]:
    """(start, end) of each note and tag: outermost brackets, private notes, an unfinished tag at the end."""
    spans, depth, opened = [], 0, 0
    for i, c in enumerate(text):
        if c == "[":
            opened = opened if depth else i
            depth += 1
        elif c == "]" and depth:
            depth -= 1
            if not depth:
                spans.append((opened, i + 1))
    if depth:
        spans.append((opened, len(text)))
    spans += [m.span() for m in _PRIVATE.finditer(text)]
    if tag := _OPEN_TAG.search(text):
        spans.append(tag.span())
    return spans


def _held_from(text: str) -> int:
    """Where the tail that a later chunk could still change begins: len(text) if none."""
    spans = _spans(text)
    # A note still open, or closed at the very end (a link's "(url)" may follow)
    held = min([len(text)] + [start for start, end in spans if end == len(text)])
    # The last word may still be arriving. Holding it costs nothing: the TTS's sentence splitter waits for what
    # follows a word before speaking it anyway.
    if word := _LAST_WORD.search(text, 0, held):
        held = word.start()
    if phrase := _HELD_PHRASE.search(text, 0, held):
        held = phrase.start()
    while inside := [start for start, end in spans if start < held < end]:  # never cut a note in two
        held = min(inside)
    return held


class Scrubber:
    """`scrub` over a stream of chunks: feed each in turn, then take the rest with `end`."""

    def __init__(self) -> None:
        self._held = ""

    def feed(self, chunk: str) -> str:
        text = self._held + chunk
        cut = _held_from(text)
        self._held = text[cut:]
        return scrub(text[:cut])

    def end(self) -> str:
        text, self._held = self._held, ""
        return scrub(text)


async def scrubbed(chunks: AsyncIterable) -> AsyncIterator:
    """`chunks` scrubbed; anything not text (LiveKit's flush markers) passes through, after what's held."""
    scrubber = Scrubber()
    async for chunk in chunks:
        if isinstance(chunk, str):
            if said := scrubber.feed(chunk):
                yield said
        else:
            if rest := scrubber.end():
                yield rest
            yield chunk
    if rest := scrubber.end():
        yield rest
