"""Where a talked-over reply was cut off, and what to do about the part the user didn't hear.

LiveKit reports how much of a reply had played when it was interrupted (its synchronised transcript). `Reply` lines
that up with the text sent to speech, so the next turn knows exactly what was heard and what wasn't. The words that
cut it are then read by `obvious`, with no model call, so nothing waits:
  remark  thanks, agreement, a backchannel ("okay", "hmm", "yes yes"): JARVIS answers it in a word or two, then gives
          the part not heard as a one-line recap
  point   a question: JARVIS answers it and drops the rest unless asked, or offers it in a few words if it mattered
Anything else (a request, a correction, a "yes" right after JARVIS asked something) is left to the brain, shown both
ways.
"""

import re
import time
import unicodedata
from dataclasses import dataclass, field

from jarvis.voice import unspoken

REMARK, POINT = "remark", "point"
LONGEST_WORD = 20  # a cut partway through a longer run of letters (unspaced scripts) isn't moved to its start
HEARD_WORDS = 25  # end of what was heard, quoted to the brain
UNHEARD_CHARS = 1200  # start of what wasn't, quoted to the brain

# Utterances made only of these (repeated or combined: "yes yes", "okay thanks", "thank you so much JARVIS") are
# passing remarks. Other languages and anything less plain go to the brain.
ACKS = {tuple(p.split()) for p in (
    "thank you", "thanks", "thanks a lot", "cheers", "ok", "okay", "right", "alright", "all right", "yes", "yeah", "yep",
    "yup", "sure", "got it", "i see", "cool", "nice", "great", "good", "perfect", "wow", "interesting", "makes sense",
    "fair enough", "true", "exactly", "indeed", "of course", "uh huh", "mhm", "mm", "hmm", "aha", "haha", "lovely",
    "brilliant", "awesome", "fine", "understood", "noted", "sounds good", "go on", "thats great", "thats good",
    "thats nice", "thats right", "thats true", "thats interesting", "that makes sense")}
FILLERS = {"jarvis", "so", "much", "very", "really", "oh", "ah", "um", "uh", "well", "and"}
_LONGEST_ACK = max(map(len, ACKS))
_CLOSING = {"Po", "Pe", "Pf", "Pd"}  # what ends the words before a cut: . , ! ? 。 ) ” —


@dataclass(frozen=True)
class Cut:
    """A reply the user spoke over: what of it they heard, what they didn't, and whether it was stopped before the
    brain had finished it (then there was more to come than `unheard`)."""

    heard: str
    unheard: str
    unfinished: bool


@dataclass
class Reply:
    """One reply to a user turn: the text sent to speech, and what LiveKit says was heard of it."""

    sent: list[str] = field(default_factory=list)
    finished: bool = False  # streamed to its end, not stopped partway
    heard: str | None = None  # None until LiveKit reports it; it doesn't when nothing played
    interrupted: bool = False
    began: float = field(default_factory=time.time)
    over: bool = False  # the user's next turn has ended: later reports are of other speech

    def spoke(self, text: str, interrupted: bool, started: float | None = None) -> None:
        """LiveKit's report of what played, from `started` (epoch seconds): the first since this reply began, and
        before the user's next turn ended, is its."""
        if self.heard is None and not self.over and (started is None or started >= self.began):
            self.heard, self.interrupted = text, interrupted

    def cut(self) -> Cut | None:
        """Where the user cut it off, or None if they heard it all. With no report from LiveKit, only a reply stopped
        partway counts as cut off (nothing of it played); one that ran to its end is taken as heard."""
        full = unspoken.scrub("".join(self.sent))  # as spoken: LiveKit reports the scrubbed words
        if not _has_words(full) or (self.heard is None and self.finished) or (
                self.heard is not None and not self.interrupted):
            return None
        heard = self.heard or ""
        rest = unheard(full, heard)
        if self.finished and not _has_words(rest):
            return None  # cut off on its last word
        return Cut(heard.strip(), rest, not self.finished)


def unheard(full: str, heard: str) -> str:
    """What of `full` follows `heard`, its spoken start. Matched on letters and digits alone, as LiveKit's transcript
    may space and punctuate differently; a word cut partway counts as unheard."""
    want = [c.lower() for c in heard if c.isalnum()]
    matched = end = 0
    for i, c in enumerate(full):
        if matched == len(want):
            break
        if c.isalnum():
            if c.lower() != want[matched]:
                break  # it went its own way from here: keep what matched
            matched += 1
            end = i + 1
    if end < len(full) and full[end].isalnum():
        start = end
        while start > 0 and full[start - 1].isalnum():
            start -= 1
        if end - start <= LONGEST_WORD:
            end = start
    while end < len(full) and (full[end].isspace() or unicodedata.category(full[end]) in _CLOSING):
        end += 1
    return full[end:].strip()


def _has_words(text: str) -> bool:
    return any(c.isalnum() for c in text)


def _tokens(text: str) -> list[str]:
    return re.sub(r"[^\w\s]", " ", re.sub(r"['’]", "", text.lower())).split()


def obvious(cut: Cut, words: str) -> str | None:
    """REMARK or POINT when plain from the words alone, else None. A question is a point. A bare "yes" or "okay"
    is a remark only if JARVIS hadn't just asked something, which it may answer."""
    if "?" in words:
        return POINT
    tokens = _tokens(words)
    if not tokens or "?" in cut.heard:
        return None
    i, acks = 0, 0
    while i < len(tokens):
        size = next((n for n in range(_LONGEST_ACK, 0, -1) if tuple(tokens[i:i + n]) in ACKS), 0)
        if size:
            i, acks = i + size, acks + 1
        elif tokens[i] in FILLERS:
            i += 1
        else:
            return None
    return REMARK if acks else None


def framing(cut: Cut, kind: str | None) -> list[str]:
    """Bracketed lines for the brain's next turn: where its reply was cut off, and what to do about the rest."""
    heard = cut.heard.split()
    where = (f'up to "{"…" if len(heard) > HEARD_WORDS else ""}{" ".join(heard[-HEARD_WORDS:])}"' if heard
             else "none of it")
    lines = [f"[They spoke over your last reply and heard {where}."
             + (f' They didn\'t hear: "{_start(cut.unheard)}".' if cut.unheard else "")
             + (" It was stopped before you'd finished it." if cut.unfinished else "") + "]"]
    rest = "the rest of your last reply"
    drop = (f"drop {rest} unless they ask for it; if it held something they need (a warning, a question for them), "
            f"offer it in a few words after")
    if kind == REMARK:
        lines.append(f"[What they said is only a passing remark: answer it in a word or two, then give {rest} as one "
                     f"short sentence, a recap of what they missed, not a repeat of what they heard.]")
    elif kind == POINT:
        lines.append(f"[What they said is a new point: answer that, and {drop}.]")
    else:
        lines.append(f"[If what they said is only a passing remark, answer it in a word or two, then recap {rest} in "
                     f"one short sentence. Otherwise answer it, and {drop}.]")
    return lines


def _start(text: str) -> str:
    if len(text) <= UNHEARD_CHARS:
        return text
    return text[:UNHEARD_CHARS].rsplit(" ", 1)[0] + "…"
