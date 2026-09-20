"""Long-term memory, the recent-conversation log and the full conversation log.

All plain markdown in the profile's data folder (jarvis.core.profile), so they can be read and edited by hand.
Long-term memory, memory.md, is one line per fact, '- fact (YYYY-MM-DD)', under the headings '## User' and
'## Environment', each section with a size limit; changing it rewrites the file in that shape.
"""

import re
from datetime import datetime, timedelta
from pathlib import Path

from jarvis.core import profile
from jarvis.core.files import write_atomic
from jarvis.safety import scan

RECENT_MAX_CHARS = 6000
SEARCH_MAX_CHARS = 6000
_ENTRY_START = re.compile(r"\n(?=\[\d{2}:\d{2}\] )")


def read_memory() -> str:
    path = profile.current().memory_file
    return path.read_text() if path.exists() else ""


# memory.md has two sections, each under its own heading: what JARVIS knows about the user themself, and about their
# machine, setup and projects. Each holds at most so many characters, counting whole lines with their dates, so the
# prompt stays small and JARVIS has to keep it tidy rather than let it grow.
SECTIONS = {"user": "User", "environment": "Environment"}  # the name callers give, then the heading in the file
LIMITS = {"user": 2000, "environment": 3000}
FACT_MAX = 400  # characters in any one line, date included
NEAR_FULL = 0.9  # from this share of its limit, saving to a section says to consolidate soon
_HEADING = re.compile(r"#+\s*(.*?)[\s#]*")


def title(section: str) -> str:
    """The section's name in the prompt and in what JARVIS is told."""
    return f"About {profile.name()}" if section == "user" else f"{profile.name()}'s environment"


def _section(section: str) -> str:
    """The section named, checked."""
    key = str(section).strip().lower()
    if key not in SECTIONS:
        raise ValueError("section must be user or environment.")
    return key


def parse(text: str) -> dict[str, list[str]]:
    """memory.md's fact lines by section, each as written but trimmed, leaving out blank lines and headings. Lines
    under no heading, or under one that isn't a section's, count as User."""
    found: dict[str, list[str]] = {s: [] for s in SECTIONS}
    section = "user"
    for line in text.splitlines():
        if not (line := line.strip()):
            continue
        if heading := _HEADING.fullmatch(line):
            section = next((s for s, h in SECTIONS.items() if heading[1].lower() == h.lower()), "user")
            continue
        found[section].append(line)
    return found


def render(found: dict[str, list[str]]) -> str:
    """memory.md as written back: each section's heading, then its lines."""
    return "\n".join(f"## {h}\n" + "".join(f"{line}\n" for line in found[s]) for s, h in SECTIONS.items())


def facts() -> dict[str, list[str]]:
    """memory.md's lines by section, as written, leaving out blank ones and headings."""
    return parse(read_memory())


def usage(lines: list[str]) -> int:
    """The characters a section's lines take, as its limit counts them."""
    return sum(len(line) for line in lines)


def _used(section: str, lines: list[str]) -> str:
    return f"{usage(lines):,} of {LIMITS[section]:,}"


def _listed(lines: list[str]) -> str:
    """A section's lines numbered, each with its length: what's needed to consolidate it."""
    return "\n".join(f"{n}. {line} [{len(line)}]" for n, line in enumerate(lines, 1)) or "(none)"


class Full(ValueError):
    """A section with no room for a change: its message lists the section's facts for consolidating them, and
    `short` says it in a sentence."""

    def __init__(self, message: str, short: str) -> None:
        super().__init__(message)
        self.short = short


def check_text(text: str, before: str | None = None) -> None:
    """Raise ValueError, saying what to do, if `text` as memory.md breaks the limits: a line over FACT_MAX, or a
    section over its size. Given the file as it was `before`, only what got worse counts, so a file already over a
    limit (edited by hand) can still be brought back under it a step at a time."""
    new, old = parse(text), parse(before) if before is not None else None
    for s in SECTIONS:
        for line in new[s]:
            if len(line) > FACT_MAX and (old is None or line not in old[s]):
                raise ValueError(f"A memory fact may be at most {FACT_MAX} characters, its date included, and this "
                                 f"one is {len(line)}; say it more briefly or split it: {line}")
        if usage(new[s]) > LIMITS[s] and (old is None or usage(new[s]) > usage(old[s])):
            now = new[s] if old is None else old[s]
            short = f"{title(s)} is full: this would make it {_used(s, new[s])} characters."
            raise Full(f"{short} Its facts now ({_used(s, now)}, each with its length):\n{_listed(now)}\nMerge, "
                       "shorten or drop some of them, keeping what still matters, then save again.", short)


def _write(found: dict[str, list[str]], before: str) -> None:
    text = render(found)
    check_text(text, before)
    write_atomic(profile.current().memory_file, text)


def _saved(lead: str, section: str, lines: list[str]) -> str:
    """What saving to a section says: where, how full it is now, and a nudge once it's nearly full."""
    out = f"{lead} {title(section)} ({_used(section, lines)})."
    if usage(lines) >= NEAR_FULL * LIMITS[section]:
        out += " It's nearly full, so consolidate soon: merge or shorten facts with memory_replace."
    return out


def _line(fact: str) -> str:
    """A fact as memory.md writes it, dated today, without invisible characters (jarvis.safety.scan). Raises
    ValueError for an empty one."""
    if not (fact := " ".join(scan.clean(fact).split())):
        raise ValueError("The fact is empty.")
    return f"- {fact} ({datetime.now():%Y-%m-%d})"


def remember(fact: str, section: str) -> str:
    """Add a fact at the end of a section. One the section has no room for is refused with ValueError, which lists
    the section's facts so they can be consolidated first."""
    section, line = _section(section), _line(fact)
    before = read_memory()
    found = parse(before)
    found[section].append(line)
    _write(found, before)
    return _saved("Saved to", section, found[section])


def replace(old: str, fact: str) -> str:
    """Rewrite the one line containing `old` (in any case) as the fact `fact`, dated today, in the same section and
    place: for merging or shortening facts, or updating one rather than adding a near-duplicate."""
    if not (needle := " ".join(old.split()).lower()):
        raise ValueError("Say which memory to replace: text found in that line only.")
    line = _line(fact)
    before = read_memory()
    found = parse(before)
    hits = [(s, i) for s in SECTIONS for i, have in enumerate(found[s]) if needle in " ".join(have.split()).lower()]
    if not hits:
        raise ValueError(f"No memory line contains '{old}'.")
    if len(hits) > 1:
        listed = "\n".join(found[s][i] for s, i in hits)
        raise ValueError(f"'{old}' is in {len(hits)} memory lines; quote text found only in the one meant:\n{listed}")
    (section, i), = hits
    found[section][i] = line
    _write(found, before)
    return _saved("Replaced in", section, found[section])


def forget(text: str) -> str:
    found = parse(read_memory())
    removed = 0
    for s in SECTIONS:
        kept = [line for line in found[s] if text.lower() not in line.lower()]
        removed += len(found[s]) - len(kept)
        found[s] = kept
    write_atomic(profile.current().memory_file, render(found))
    return f"Removed {removed} matching line(s)."


def replace_fact(line: str, new: str = "") -> str:
    """Rewrite the one line of memory.md that reads exactly `line` as the fact `new`, dated today, or delete it if
    `new` is empty. The control panel's edit and forget, which name a line exactly rather than by a few words."""
    before = read_memory()
    found = parse(before)
    if not (section := next((s for s in SECTIONS if line in found[s]), None)):
        raise ValueError("That memory has changed or gone since the list was shown; refresh and try again.")
    i = found[section].index(line)
    found[section][i : i + 1] = [_line(new)] if new.strip() else []
    _write(found, before)
    return f"Changed: {' '.join(new.split())}" if new.strip() else "Forgotten."


def prompt_block() -> str:
    """Long-term memory as the prompt shows it: each section with how full it is, so JARVIS knows when to tidy. Any
    invisible characters a hand edit brought in are left out."""
    found = facts()
    parts = [f"{title(s)} ({_used(s, found[s])} characters):\n" + ("\n".join(map(scan.clean, found[s])) or "(nothing yet)")
             for s in SECTIONS]
    return "<long_term_memory>\n" + "\n".join(parts) + "\n</long_term_memory>"


def read_recent() -> str:
    path = profile.current().recent_file
    return path.read_text() if path.exists() else ""


NOT_REPLIED, CUT_OFF = "(interrupted before replying)", " (interrupted)"  # how a cut-off reply is logged


def _reply(assistant: str, interrupted: bool) -> str:
    """JARVIS's side of an exchange as logged; a reply cut off by the user's next words says so."""
    said = assistant.strip()
    if not interrupted:
        return said
    return f"{said}{CUT_OFF}" if said else NOT_REPLIED


def log_exchange(user: str, assistant: str, approved: list[str] | tuple[str, ...] = (), interrupted: bool = False) -> None:
    """Keep a short rolling transcript so a new session knows what was just said, and add the
    exchange to the day's full log. `approved` are the commands the user's words said yes to;
    `interrupted` means the user spoke again before the reply was done, so it was cut off."""
    now = datetime.now(profile.tz())
    reply = _reply(assistant, interrupted)
    entry = f"[{now:%Y-%m-%d %H:%M}] {profile.name()}: {user.strip()}\nJARVIS: {reply}\n\n"
    text = read_recent() + entry
    if len(text) > RECENT_MAX_CHARS:
        text = text[-RECENT_MAX_CHARS:]
        text = text[text.find("\n[") + 1 :] if "\n[" in text else text
    profile.current().recent_file.write_text(text)
    yes = "".join(f"(Approved: {c.strip()})\n" for c in approved)
    _log_day(now, f"{profile.name()}: {user.strip()}\n{yes}JARVIS: {reply}\n\n")


def log_announcement(text: str) -> None:
    """Something JARVIS said between turns, such as a task update, for the day's full log."""
    _log_day(datetime.now(profile.tz()), f"JARVIS (announced): {text.strip()}\n\n")


def _log_day(now: datetime, entry: str) -> None:
    (folder := profile.current().conversations).mkdir(parents=True, exist_ok=True)
    path = folder / f"{now:%Y-%m-%d}.md"
    head = "" if path.exists() else f"# Conversations, {now:%A %-d %B %Y}\n\n"
    with path.open("a") as f:
        f.write(f"{head}[{now:%H:%M}] {entry}")


def day_entries(path: Path) -> list[str]:
    """A day file's entries, oldest first, each from its "[HH:MM] " on, its heading left out."""
    return [e.strip() for e in _ENTRY_START.split(path.read_text(errors="replace")) if e.startswith("[")]


def search_conversations(text: str = "", date: str = "", days: int = 0) -> str:
    """Entries from the full log, newest first: those containing every word of `text` (any, if empty),
    on `date` (YYYY-MM-DD) or in the last `days` days (every day, if neither)."""
    if date and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        raise ValueError("date must be YYYY-MM-DD")
    folder = profile.current().conversations
    files = sorted(folder.glob("????-??-??.md"), reverse=True) if folder.exists() else []
    if date:
        files = [f for f in files if f.stem == date]
    elif days > 0:
        since = f"{datetime.now(profile.tz()).date() - timedelta(days=days - 1):%Y-%m-%d}"
        files = [f for f in files if f.stem >= since]
    words = text.lower().split()
    found: list[str] = []
    for f in files:
        found += [f"{f.stem} {e}" for e in reversed(day_entries(f)) if all(w in e.lower() for w in words)]
    if not found:
        where = f" on {date}" if date else f" in the last {days} days" if days > 0 else ""
        return f"No conversations{where}" + (f" mention '{text}'." if words else " logged.")
    shown, size = [], 0
    for e in found:
        if shown and size + len(e) > SEARCH_MAX_CHARS:
            break
        shown.append(e)
        size += len(e) + 2
    more = f"\n\n({len(found) - len(shown)} older matches not shown; narrow the search.)" if len(shown) < len(found) else ""
    return "\n\n".join(shown) + more
