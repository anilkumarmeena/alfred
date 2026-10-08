"""The speech vocabulary: names and unusual words speech recognition should expect, so Whisper writes "JARVIS" rather
than "Yavis", and the user's projects and colleagues' names as they're spelled.

It belongs to whoever uses this JARVIS, like long-term memory: plain markdown in data/vocabulary.md beside memory.md,
to read and edit by hand. One line per entry: the right spelling, then what Whisper has been heard to make of it, if
known, then the date it was added or last confirmed; one the learning review added on its own is marked learned:

    - JARVIS (heard as: Yavis, Cajarves) (2026-10-08)
    - Siobhan (heard as: Shivon) (2026-10-08, learned)

Until the first change the file needn't exist: the list is then SEED, the assistant's own name, which everyone says.
What speech recognition is given (load()) is the entries, then the names of the projects on the project list, so a
project is expected from the day it's added, without being copied here.

Whisper takes them as its prompt (initial_prompt), which biases it towards those spellings (JARVIS_STT_PROMPT=off stops
that). As the prompt doesn't always win, a clip the speech gate passes also has its known mishearings put right
(Vocabulary.correct): whole words or phrases only, and only those listed here. Whisper can echo a prompt back for a
clip with no words in it, so a transcript that is nothing but several of the prompt's words is dropped (echo()).

The learning review (jarvis.tasks.learning) adds the words it judges were misheard without asking: a wrong entry costs
little. It's careful with mishearings, as those change what the user said: one is kept only if it's in the user's own
words in that conversation and isn't an everyday word (plausible_mishearing). The list holds at most MAX_ENTRIES; when
the review adds one more, the learned entry confirmed longest ago makes room. What the user added is never dropped
to make room: a full list of those refuses another until one is removed.
"""

import re
from dataclasses import dataclass, field
from datetime import datetime
from functools import cache
from pathlib import Path

from jarvis.core import profile
from jarvis.core.files import write_atomic
from jarvis.safety import scan

MAX_ENTRIES = 60
MAX_HEARD = 6  # mishearings kept per entry: the latest
WORD_MAX = 40  # characters in an entry's word or a mishearing
WORD_MAX_WORDS = 4
PROMPT_MAX_CHARS = 600  # of Whisper's prompt, about 150 tokens: under the 223 it keeps, so the start isn't cut off
ECHO_TERMS = 3  # a transcript of nothing but this many of the prompt's terms or more is the prompt echoed back
HEADER = ("# Vocabulary\n\nNames and words speech recognition should expect, one per line: the right spelling, then what "
          "it has been misheard as. JARVIS adds to it as it learns.\n\n")
SEED = [("JARVIS", ("Yavis", "Cajarves", "Jarves"))]
DICTIONARY = Path("/usr/share/dict/words")
# Everyday words a mishearing may never be, when the system dictionary is missing
COMMON = frozenset("""a an and are as at be but by can do for from go has have he her his how i if in is it its me my no
not of on or our out say see she so that the them then there they this to up us was we what when who will with yes you
your okay ok hey hi hello thanks thank please right just like""".split())
_LINE = re.compile(r"- (?P<word>.+?)(?: \(heard as: (?P<heard>[^()]*)\))?"
                   r"(?: \((?P<date>\d{4}-\d{2}-\d{2})(?P<learned>, learned)?\))?")


@dataclass
class Entry:
    word: str
    heard: list[str] = field(default_factory=list)
    date: str = ""
    learned: bool = False

    def line(self) -> str:
        heard = f" (heard as: {', '.join(self.heard)})" if self.heard else ""
        return f"- {self.word}{heard} ({self.date or _today()}{', learned' if self.learned else ''})"


def _key(text: str) -> str:
    """Words compared ignoring case, spacing and punctuation: "AI-Armory" is "ai armory"."""
    return " ".join(re.findall(r"[^\W_]+", text.lower()))


def same(a: str, b: str) -> bool:
    """Whether two words are the same but for case, spacing and punctuation."""
    return _key(a) == _key(b)


def _today() -> str:
    return f"{datetime.now():%Y-%m-%d}"


def tidy(text: str, what: str) -> str:
    """A word or mishearing on one line, without invisible characters or the brackets and commas the file uses.
    Raises ValueError for one that's empty, too long, or not a word."""
    text = " ".join(scan.clean(str(text)).replace("(", " ").replace(")", " ").replace(",", " ").split())
    if not _key(text):
        raise ValueError(f"The {what} is empty.")
    if len(text) > WORD_MAX or len(text.split()) > WORD_MAX_WORDS:
        raise ValueError(f"The {what} is too long: at most {WORD_MAX_WORDS} words and {WORD_MAX} characters.")
    return text


def parse(text: str) -> list[Entry]:
    """The entries in vocabulary.md's text, in order; lines that aren't entries are left out."""
    found = []
    for raw in text.splitlines():
        if not (m := _LINE.fullmatch(raw.strip())) or not _key(m["word"]):
            continue
        heard = [h.strip() for h in (m["heard"] or "").split(",") if _key(h)]
        found.append(Entry(m["word"].strip(), heard, m["date"] or "", bool(m["learned"])))
    return found


def render(entries: list[Entry]) -> str:
    return HEADER + "".join(e.line() + "\n" for e in entries)


def entries() -> list[Entry]:
    """The vocabulary as it is now: the file's entries, or SEED before it exists."""
    if not (path := profile.current().vocabulary_file).exists():
        return [Entry(word, list(heard), _today()) for word, heard in SEED]
    return parse(path.read_text())


def _write(found: list[Entry]) -> None:
    write_atomic(profile.current().vocabulary_file, render(found))


def find(found: list[Entry], word: str) -> Entry | None:
    return next((e for e in found if same(e.word, word)), None)


def _merged(old: list[str], new: list[str], word: str) -> list[str]:
    """`old` mishearings and then the `new` ones not among them, leaving out the word itself, the latest MAX_HEARD."""
    out: list[str] = []
    for h in [*old, *new]:
        if not same(h, word) and not any(same(h, o) for o in out):
            out.append(h)
    return out[-MAX_HEARD:]


def _room(found: list[Entry]) -> None:
    """Make room for one more entry: the learned one confirmed longest ago goes. Raises ValueError when all are the
    user's own."""
    if len(found) < MAX_ENTRIES:
        return
    learned = [e for e in found if e.learned]
    if not learned:
        raise ValueError(f"The vocabulary is full ({MAX_ENTRIES} words you added); remove one first.")
    found.remove(min(learned, key=lambda e: e.date))


def _tidied(word: str, heard: list[str] | tuple[str, ...]) -> tuple[str, list[str]]:
    """A word and its mishearings, each tidied; blank mishearings are left out."""
    return tidy(word, "word"), [tidy(h, "mishearing") for h in heard if _key(str(h))]


def add(word: str, heard: list[str] | tuple[str, ...] = (), learned: bool = False) -> str:
    """Add a word, or more ways it's misheard to one already there, which is then confirmed today. The user's own
    addition (learned False) makes a learned entry theirs. Returns what changed, in a sentence."""
    word, heard = _tidied(word, heard)
    found = entries()
    if have := find(found, word):
        new = _merged(have.heard, heard, have.word)
        added = [h for h in new if h not in have.heard]
        have.heard, have.date, have.learned = new, _today(), have.learned and learned
        _write(found)
        return f"{have.word} was already in the vocabulary" + (f"; added heard as {', '.join(added)}." if added else ".")
    _room(found)
    found.append(Entry(word, _merged([], heard, word), _today(), learned))
    _write(found)
    return f"Added {word} to the vocabulary" + (f", heard as {', '.join(found[-1].heard)}." if found[-1].heard else ".")


def save(old: str, word: str, heard: list[str]) -> str:
    """Rewrite the entry for `old` as `word` with exactly these mishearings, confirmed by the user today (the panel's
    edit)."""
    word, heard = _tidied(word, heard)
    found = entries()
    if not (have := find(found, old)):
        raise ValueError(f"'{old}' isn't in the vocabulary any more; refresh and try again.")
    if (other := find(found, word)) and other is not have:
        raise ValueError(f"{other.word} is already in the vocabulary.")
    have.word, have.heard, have.date, have.learned = word, _merged([], heard, word), _today(), False
    _write(found)
    return f"Saved {word}."


def remove(word: str) -> str:
    found = entries()
    if not (have := find(found, word)):
        raise ValueError(f"'{word}' isn't in the vocabulary.")
    found.remove(have)
    _write(found)
    return f"Removed {have.word} from the vocabulary."


# ── What the review may learn ───────────────────────────────────


@cache
def _dictionary() -> frozenset[str]:
    """The everyday words: the system dictionary's lowercase ones (its capitalised ones are names), else COMMON."""
    try:
        return frozenset(w for w in DICTIONARY.read_text().split() if w.islower()) | COMMON
    except OSError:
        return COMMON


def everyday(token: str) -> bool:
    """Whether a word is an everyday one, or one with a common ending ("services", "talked", "running", "Bob's")."""
    words = _dictionary()
    token = token.lower().removesuffix("'s")
    stems = {token, token.removesuffix("s"), token.removesuffix("es"), token.removesuffix("ed"),
             token.removesuffix("d"), token.removesuffix("ing"), token.removesuffix("ing") + "e"}
    return any(s in words for s in stems if s)


def plausible_mishearing(heard: str, word: str) -> bool:
    """Whether the learning review may keep `heard` as a mishearing of `word`, to be replaced by it in what the user
    says: at least 3 letters, not the word itself, and not made only of everyday words, which the user may well have
    meant ("service" for "JARVIS" would change every "service" they say)."""
    tokens = _key(heard).split()
    if not tokens or len("".join(tokens)) < 3 or same(heard, word):
        return False
    return not all(everyday(t) for t in tokens)


def learnable(word: str, heard: list[str]) -> tuple[str, list[str]]:
    """A word the learning review suggests, tidied, with only the mishearings it may keep (plausible_mishearing) that
    the vocabulary doesn't have yet; unusable ones are left out. Raises ValueError for a word already there with all
    of them."""
    word, kept = tidy(word, "word"), []
    for h in heard:
        try:
            h = tidy(h, "mishearing")
        except ValueError:
            continue
        if plausible_mishearing(h, word) and not any(same(h, k) for k in kept):
            kept.append(h)
    if have := find(entries(), word):
        if not (kept := [h for h in kept if not any(same(h, k) for k in have.heard)]):
            raise ValueError(f"{have.word} is already in the vocabulary, with those mishearings.")
    return word, kept


def _phrase(heard: str) -> str:
    """A regex for `heard` as a whole word or phrase, in any spacing or hyphens between its words."""
    return r"(?<![\w'-])" + r"[\s-]+".join(map(re.escape, _key(heard).split())) + r"(?![\w-])"


def said_in(heard: str, text: str) -> bool:
    """Whether `heard` is in `text` as a whole word or phrase, in any case."""
    return bool(_key(heard)) and re.search(_phrase(heard), text, re.IGNORECASE) is not None


# ── What speech recognition gets ───────────────────────────────


def _spoken(name: str, aliases: list[str]) -> str:
    """A project's name as it's said and written: an alias that's the same name in other case or spacing ("AI Armory"
    for ai-armory), else the name with its hyphens and underscores as spaces."""
    cased = next((a for a in aliases if same(a, name) and a != a.lower()), None)
    return cased or" ".join(name.replace("-", " ").replace("_", " ").split())


def project_terms() -> list[str]:
    """The project list's names, as said; none if it can't be read."""
    try:
        from jarvis.tasks import projects

        return [_spoken(p.name, p.aliases) for p in projects.load()]
    except Exception:
        return []


@dataclass(frozen=True)
class Vocabulary:
    """What speech recognition is given for a conversation: the words to expect, most important first, and the known
    mishearings, each with the word it stands for."""
    terms: tuple[str, ...] = ()
    fixes: tuple[tuple[str, str], ...] = ()

    def prompt(self, limit: int = PROMPT_MAX_CHARS) -> str:
        """Whisper's prompt: the terms as a list, as many as fit in `limit` characters; "" for none."""
        shown, size = [], 0
        for t in self.terms:
            if shown and size + len(t) + 2 > limit:
                break
            shown.append(t)
            size += len(t) + 2
        return f"{', '.join(shown)}." if shown else ""

    def correct(self, text: str) -> str:
        """`text` with each known mishearing, as a whole word or phrase in any case, replaced by its word. Longer
        mishearings go first, so "hey Yavis" is put right before "Yavis"."""
        for heard, word in sorted(self.fixes, key=lambda f: -len(f[0])):
            text = re.sub(_phrase(heard), lambda _, w=word: w, text, flags=re.IGNORECASE)
        return text

    def echo(self, text: str) -> bool:
        """Whether `text` is only the prompt echoed back: ECHO_TERMS or more of its terms and nothing else, which no
        one says. A user saying a term or two ("JARVIS", "open AI Armory") is never an echo."""
        said, terms = _key(text), sorted({k for t in self.terms if (k := _key(t))}, key=len, reverse=True)
        if not said or not terms:
            return False
        found = 0
        for t in terms:
            said, n = re.subn(rf"\b{re.escape(t)}\b", " ", said)
            found += n
        return found >= ECHO_TERMS and not said.strip()


def build(found: list[Entry], projects: list[str]) -> Vocabulary:
    """The user's own entries, then the projects, then the learned ones newest first: the order the prompt keeps them
    in when it's cut short."""
    learned = sorted((e for e in found if e.learned), key=lambda e: e.date, reverse=True)
    words = [e.word for e in found if not e.learned] + projects + [e.word for e in learned]
    terms, taken = [], set()
    for w in words:  # each once, where it first comes
        if _key(w) not in taken:
            terms.append(w)
            taken.add(_key(w))
    fixes = [(h, e.word) for e in found for h in e.heard if _key(h) not in taken and len(_key(h).replace(" ", "")) >= 2]
    return Vocabulary(tuple(terms), tuple(fixes))


def load() -> Vocabulary:
    """The vocabulary as speech recognition uses it now."""
    try:
        found = entries()
    except OSError:
        found = []
    return build(found, project_terms())
