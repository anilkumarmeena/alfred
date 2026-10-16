"""Speech vocabulary: names and unusual words Whisper should expect, plus known mishearings to fix.

Stored as hand-editable Markdown in the profile's vocabulary.md (SEED until first written):

    - JARVIS (heard as: Yavis, Cajarves) (2026-10-08)
    - Siobhan (heard as: Shivon) (2026-10-08, learned)

load() = entries + project names. Terms bias Whisper via initial_prompt; Vocabulary.correct() rewrites listed
mishearings (whole words only); echo() drops transcripts that are just the prompt echoed back. The learning review
adds entries unasked, but keeps a mishearing only if the user said it and it isn't an everyday word. At MAX_ENTRIES
the stalest learned entry is evicted; user-added entries are never evicted. The voice tools, the control panel and
the learning review all change the file, so each change happens under its file lock (files.locked).
"""

import re
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from functools import cache
from pathlib import Path

from jarvis.core import profile
from jarvis.core.files import locked, write_atomic
from jarvis.safety import scan

MAX_ENTRIES = 60
MAX_HEARD = 6  # latest mishearings kept per entry
WORD_MAX = 40  # max chars per word or mishearing
WORD_MAX_WORDS = 4
PROMPT_MAX_CHARS = 600  # ~150 tokens, under Whisper's 223-token prompt cap
ECHO_TERMS = 3  # this many prompt terms and nothing else = echo
HEADER = ("# Vocabulary\n\nNames and words speech recognition should expect, one per line: the right spelling, then what "
          "it has been misheard as. JARVIS adds to it as it learns.\n\n")
SEED = [("JARVIS", ("Yavis", "Cajarves", "Jarves"))]
# The system word list: built into macOS; on Linux from a package such as wamerican (Debian/Ubuntu) or words (Fedora)
DICTIONARIES = (Path("/usr/share/dict/words"), Path("/usr/share/dict/american-english"),
                Path("/usr/share/dict/british-english"))
# Fallback everyday words when the system dictionary is missing
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
    """Normalise for comparison: "AI-Armory" -> "ai armory"."""
    return " ".join(re.findall(r"[^\W_]+", text.lower()))


def same(a: str, b: str) -> bool:
    """Equal ignoring case, spacing and punctuation."""
    return _key(a) == _key(b)


def _today() -> str:
    return f"{datetime.now():%Y-%m-%d}"


def tidy(text: str, what: str) -> str:
    """Clean a word for the file (no invisible chars, brackets or commas). ValueError if empty or too long."""
    text = " ".join(scan.clean(str(text)).replace("(", " ").replace(")", " ").replace(",", " ").split())
    if not _key(text):
        raise ValueError(f"The {what} is empty.")
    if len(text) > WORD_MAX or len(text.split()) > WORD_MAX_WORDS:
        raise ValueError(f"The {what} is too long: at most {WORD_MAX_WORDS} words and {WORD_MAX} characters.")
    return text


def parse(text: str) -> list[Entry]:
    """Entries from vocabulary.md text, in order; non-entry lines ignored."""
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
    """Current entries, or SEED if the file doesn't exist."""
    if not (path := profile.current().vocabulary_file).exists():
        return [Entry(word, list(heard), _today()) for word, heard in SEED]
    return parse(path.read_text())


@contextmanager
def _changing():
    """Yield the entries for editing, under the file's lock; saved on exit. An exception in the block saves nothing."""
    path = profile.current().vocabulary_file
    with locked(path):
        found = entries()
        yield found
        write_atomic(path, render(found))


def find(found: list[Entry], word: str) -> Entry | None:
    return next((e for e in found if same(e.word, word)), None)


def _merged(old: list[str], new: list[str], word: str) -> list[str]:
    """Deduplicated `old` + `new` mishearings, excluding `word`, last MAX_HEARD kept."""
    out: list[str] = []
    for h in [*old, *new]:
        if not same(h, word) and not any(same(h, o) for o in out):
            out.append(h)
    return out[-MAX_HEARD:]


def _room(found: list[Entry]) -> None:
    """Evict the stalest learned entry if full. ValueError if all entries are user-added."""
    if len(found) < MAX_ENTRIES:
        return
    learned = [e for e in found if e.learned]
    if not learned:
        raise ValueError(f"The vocabulary is full ({MAX_ENTRIES} words you added); remove one first.")
    found.remove(min(learned, key=lambda e: e.date))


def _tidied(word: str, heard: list[str] | tuple[str, ...]) -> tuple[str, list[str]]:
    """Tidy a word and its mishearings, dropping blanks."""
    return tidy(word, "word"), [tidy(h, "mishearing") for h in heard if _key(str(h))]


def add(word: str, heard: list[str] | tuple[str, ...] = (), learned: bool = False) -> str:
    """Add a word or merge mishearings into an existing one (re-dated today); return a summary sentence.

    A user add (learned=False) converts a learned entry to user-owned."""
    word, heard = _tidied(word, heard)
    with _changing() as found:
        if have := find(found, word):
            new = _merged(have.heard, heard, have.word)
            added = [h for h in new if h not in have.heard]
            have.heard, have.date, have.learned = new, _today(), have.learned and learned
            return f"{have.word} was already in the vocabulary" + (f"; added heard as {', '.join(added)}." if added else ".")
        _room(found)
        found.append(Entry(word, _merged([], heard, word), _today(), learned))
    return f"Added {word} to the vocabulary" + (f", heard as {', '.join(found[-1].heard)}." if found[-1].heard else ".")


def save(old: str, word: str, heard: list[str]) -> str:
    """Control-panel edit: replace entry `old` with `word` and exactly `heard`, as user-owned."""
    word, heard = _tidied(word, heard)
    with _changing() as found:
        if not (have := find(found, old)):
            raise ValueError(f"'{old}' isn't in the vocabulary any more; refresh and try again.")
        if (other := find(found, word)) and other is not have:
            raise ValueError(f"{other.word} is already in the vocabulary.")
        have.word, have.heard, have.date, have.learned = word, _merged([], heard, word), _today(), False
    return f"Saved {word}."


def remove(word: str) -> str:
    with _changing() as found:
        if not (have := find(found, word)):
            raise ValueError(f"'{word}' isn't in the vocabulary.")
        found.remove(have)
    return f"Removed {have.word} from the vocabulary."


# ── What the review may learn ───────────────────────────────────


@cache
def _dictionary() -> frozenset[str]:
    """Lowercase system-dictionary words (capitalised ones are names) plus COMMON."""
    for path in DICTIONARIES:
        try:
            return frozenset(w for w in path.read_text().split() if w.islower()) | COMMON
        except OSError:
            continue
    return COMMON


def everyday(token: str) -> bool:
    """Whether a token is an everyday word, allowing common suffixes ("services", "talked", "Bob's")."""
    words = _dictionary()
    token = token.lower().removesuffix("'s")
    stems = {token, token.removesuffix("s"), token.removesuffix("es"), token.removesuffix("ed"),
             token.removesuffix("d"), token.removesuffix("ing"), token.removesuffix("ing") + "e"}
    return any(s in words for s in stems if s)


def plausible_mishearing(heard: str, word: str) -> bool:
    """Whether `heard` is safe to auto-correct to `word`: >= 3 letters, not `word`, not all everyday words.

    Everyday words are excluded because correcting them would rewrite real speech."""
    tokens = _key(heard).split()
    if not tokens or len("".join(tokens)) < 3 or same(heard, word):
        return False
    return not all(everyday(t) for t in tokens)


def learnable(word: str, heard: list[str]) -> tuple[str, list[str]]:
    """Filter a learning-review suggestion to new, plausible mishearings.

    ValueError if the word already exists with nothing new to add."""
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
    """Regex matching `heard` as a whole phrase, with any spaces/hyphens between words."""
    return r"(?<![\w'-])" + r"[\s-]+".join(map(re.escape, _key(heard).split())) + r"(?![\w-])"


def said_in(heard: str, text: str) -> bool:
    """Whether `heard` occurs in `text` as a whole phrase (case-insensitive)."""
    return bool(_key(heard)) and re.search(_phrase(heard), text, re.IGNORECASE) is not None


# ── What speech recognition gets ───────────────────────────────


def _spoken(name: str, aliases: list[str]) -> str:
    """Spoken form of a project name: a cased alias ("AI Armory" for ai-armory), else separators as spaces."""
    cased = next((a for a in aliases if same(a, name) and a != a.lower()), None)
    return cased or" ".join(name.replace("-", " ").replace("_", " ").split())


def project_terms() -> list[str]:
    """Spoken project names; [] on any error."""
    try:
        from jarvis.tasks import projects

        return [_spoken(p.name, p.aliases) for p in projects.load()]
    except Exception:
        return []


@dataclass(frozen=True)
class Vocabulary:
    """Per-conversation STT hints: terms (priority order) and (mishearing, word) fixes."""
    terms: tuple[str, ...] = ()
    fixes: tuple[tuple[str, str], ...] = ()

    def prompt(self, limit: int = PROMPT_MAX_CHARS) -> str:
        """Comma-separated terms that fit in `limit` chars; "" if none."""
        shown, size = [], 0
        for t in self.terms:
            if shown and size + len(t) + 2 > limit:
                break
            shown.append(t)
            size += len(t) + 2
        return f"{', '.join(shown)}." if shown else ""

    def correct(self, text: str) -> str:
        """Replace known mishearings (whole phrases, case-insensitive), longest first."""
        for heard, word in sorted(self.fixes, key=lambda f: -len(f[0])):
            text = re.sub(_phrase(heard), lambda _, w=word: w, text, flags=re.IGNORECASE)
        return text

    def echo(self, text: str) -> bool:
        """True if `text` is only prompt terms, at least ECHO_TERMS of them; one or two terms never count."""
        said, terms = _key(text), sorted({k for t in self.terms if (k := _key(t))}, key=len, reverse=True)
        if not said or not terms:
            return False
        found = 0
        for t in terms:
            said, n = re.subn(rf"\b{re.escape(t)}\b", " ", said)
            found += n
        return found >= ECHO_TERMS and not said.strip()


def build(found: list[Entry], projects: list[str]) -> Vocabulary:
    """Order terms for truncation priority: user entries, projects, then learned (newest first)."""
    learned = sorted((e for e in found if e.learned), key=lambda e: e.date, reverse=True)
    words = [e.word for e in found if not e.learned] + projects + [e.word for e in learned]
    terms, taken = [], set()
    for w in words:  # dedupe, keeping first position
        if _key(w) not in taken:
            terms.append(w)
            taken.add(_key(w))
    fixes = [(h, e.word) for e in found for h in e.heard if _key(h) not in taken and len(_key(h).replace(" ", "")) >= 2]
    return Vocabulary(tuple(terms), tuple(fixes))


def load() -> Vocabulary:
    """Current Vocabulary for speech recognition."""
    try:
        found = entries()
    except OSError:
        found = []
    return build(found, project_terms())
