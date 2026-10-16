"""Long-term memory, recent-conversation log and full daily conversation log.

Plain Markdown in the profile's data folder, editable by hand. memory.md holds '- fact (YYYY-MM-DD)' lines under
'## User' and '## Environment', each section size-limited; writes re-render the file in that shape. The voice tools,
the control panel and task edits all change it, so each change happens under its file lock (files.locked).
"""

import re
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

from jarvis.core import profile
from jarvis.core.files import locked, update, write_atomic
from jarvis.safety import scan

RECENT_MAX_CHARS = 6000
SEARCH_MAX_CHARS = 6000
_ENTRY_START = re.compile(r"\n(?=\[\d{2}:\d{2}\] )")


def read_memory() -> str:
    path = profile.current().memory_file
    return path.read_text() if path.exists() else ""


# Section limits in characters (whole lines, dates included) keep the prompt small and force consolidation
SECTIONS = {"user": "User", "environment": "Environment"}  # caller key -> file heading
LIMITS = {"user": 2000, "environment": 3000}
FACT_MAX = 400  # max chars per line, date included
NEAR_FULL = 0.9  # fill ratio that triggers a "consolidate soon" hint
_HEADING = re.compile(r"#+\s*(.*?)[\s#]*")
_DATE = re.compile(r"\s*\((\d{4}-\d{2}-\d{2})\)\.?$")  # a trailing "(YYYY-MM-DD)", maybe with a full stop


def title(section: str) -> str:
    """Section title as shown in the prompt and tool results."""
    return f"About {profile.name()}" if section == "user" else f"{profile.name()}'s environment"


def _section(section: str) -> str:
    """Validate and normalise a section key."""
    key = str(section).strip().lower()
    if key not in SECTIONS:
        raise ValueError("section must be user or environment.")
    return key


def parse(text: str) -> dict[str, list[str]]:
    """Trimmed fact lines by section. Lines under no or an unknown heading count as User."""
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
    """Render sections back to memory.md text."""
    return "\n".join(f"## {h}\n" + "".join(f"{line}\n" for line in found[s]) for s, h in SECTIONS.items())


def facts() -> dict[str, list[str]]:
    """Current memory.md fact lines by section."""
    return parse(read_memory())


def usage(lines: list[str]) -> int:
    """Characters counted against a section's limit."""
    return sum(len(line) for line in lines)


def _used(section: str, lines: list[str]) -> str:
    return f"{usage(lines):,} of {LIMITS[section]:,}"


def _listed(lines: list[str]) -> str:
    """Numbered lines with lengths, to help consolidate."""
    return "\n".join(f"{n}. {line} [{len(line)}]" for n, line in enumerate(lines, 1)) or "(none)"


class Full(ValueError):
    """Section over its limit. Message lists the facts for consolidation; `short` is a one-sentence version."""

    def __init__(self, message: str, short: str) -> None:
        super().__init__(message)
        self.short = short


def check_text(text: str, before: str | None = None) -> None:
    """Raise ValueError (Full for sections) if `text` breaks FACT_MAX or a section limit.

    With `before`, only regressions count, so a hand-edited file already over a limit can shrink step by step."""
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


@contextmanager
def _changing():
    """Yield memory.md's facts by section for editing, under its lock; saved on exit if within the limits (check_text).
    An exception in the block saves nothing."""
    path = profile.current().memory_file
    with locked(path):
        before = read_memory()
        found = parse(before)
        yield found
        text = render(found)
        check_text(text, before)
        write_atomic(path, text)


def _saved(lead: str, section: str, lines: list[str]) -> str:
    """Result message: section, fill level, and a consolidate hint when nearly full."""
    out = f"{lead} {title(section)} ({_used(section, lines)})."
    if usage(lines) >= NEAR_FULL * LIMITS[section]:
        out += " It's nearly full, so consolidate soon: merge or shorten facts with memory_replace."
    return out


def _today() -> str:
    """The date lines are stamped with, in the user's time zone (as the learning review stamps them)."""
    return f"{datetime.now(profile.tz()):%Y-%m-%d}"


def plain(fact: str) -> str:
    """`fact` as saved, before its date: invisible characters stripped, whitespace collapsed, and without the dash and
    stamps of a line copied from the prompt to be rewritten, so it gets just one date. A date at the end that isn't in
    the future reads as a stamp and goes; a future one can't be, so it stays, as the fact's own."""
    fact, today = " ".join(scan.clean(fact).split()).removeprefix("- "), _today()
    while (stamp := _DATE.search(fact)) and stamp[1] <= today:
        fact = fact[: stamp.start()]
    return fact


def _line(fact: str) -> str:
    """Format a fact as a line dated today (see plain). ValueError if empty."""
    if not (fact := plain(fact)):
        raise ValueError("The fact is empty.")
    return f"- {fact} ({_today()})"


def remember(fact: str, section: str) -> str:
    """Append a fact to a section. Raises Full (listing the facts) if there's no room."""
    section, line = _section(section), _line(fact)
    with _changing() as found:
        found[section].append(line)
    return _saved("Saved to", section, found[section])


def replace(old: str, fact: str) -> str:
    """Replace the single line containing `old` (case-insensitive) with `fact`, in place. For merging/updating."""
    if not (needle := " ".join(old.split()).lower()):
        raise ValueError("Say which memory to replace: text found in that line only.")
    line = _line(fact)
    with _changing() as found:
        hits = [(s, i) for s in SECTIONS for i, have in enumerate(found[s]) if needle in " ".join(have.split()).lower()]
        if not hits:
            raise ValueError(f"No memory line contains '{old}'.")
        if len(hits) > 1:
            listed = "\n".join(found[s][i] for s, i in hits)
            raise ValueError(f"'{old}' is in {len(hits)} memory lines; quote text found only in the one meant:\n{listed}")
        (section, i), = hits
        found[section][i] = line
    return _saved("Replaced in", section, found[section])


def forget(text: str) -> str:
    removed = 0
    with _changing() as found:
        for s in SECTIONS:
            kept = [line for line in found[s] if text.lower() not in line.lower()]
            removed += len(found[s]) - len(kept)
            found[s] = kept
    return f"Removed {removed} matching line(s)."


def replace_fact(line: str, new: str = "") -> str:
    """Replace the line exactly equal to `line` with `new` (dated), or delete it if `new` is empty. Control panel."""
    with _changing() as found:
        if not (section := next((s for s in SECTIONS if line in found[s]), None)):
            raise ValueError("That memory has changed or gone since the list was shown; refresh and try again.")
        i = found[section].index(line)
        found[section][i : i + 1] = [_line(new)] if new.strip() else []
    return f"Changed: {' '.join(new.split())}" if new.strip() else "Forgotten."


def prompt_block() -> str:
    """Memory for the prompt, with each section's fill level; invisible characters stripped."""
    found = facts()
    parts = [f"{title(s)} ({_used(s, found[s])} characters):\n" + ("\n".join(map(scan.clean, found[s])) or "(nothing yet)")
             for s in SECTIONS]
    return "<long_term_memory>\n" + "\n".join(parts) + "\n</long_term_memory>"


def read_recent() -> str:
    path = profile.current().recent_file
    return path.read_text() if path.exists() else ""


NOT_REPLIED, CUT_OFF = "(interrupted before replying)", " (interrupted)"  # markers for cut-off replies


def _reply(assistant: str, interrupted: bool) -> str:
    """Reply text as logged, marked if the user cut it off."""
    said = assistant.strip()
    if not interrupted:
        return said
    return f"{said}{CUT_OFF}" if said else NOT_REPLIED


def log_exchange(user: str, assistant: str, approved: list[str] | tuple[str, ...] = (), interrupted: bool = False) -> None:
    """Append to the rolling recent log (for new sessions) and the day's full log.

    `approved`: commands the user's words approved. `interrupted`: the reply was cut off by the user."""
    now = datetime.now(profile.tz())
    reply = _reply(assistant, interrupted)
    entry = f"[{now:%Y-%m-%d %H:%M}] {profile.name()}: {user.strip()}\nJARVIS: {reply}\n\n"
    update(profile.current().recent_file, lambda before: _trimmed(before + entry))
    yes = "".join(f"(Approved: {c.strip()})\n" for c in approved)
    _log_day(now, f"{profile.name()}: {user.strip()}\n{yes}JARVIS: {reply}\n\n")


def _trimmed(text: str) -> str:
    """The last RECENT_MAX_CHARS of the recent log, from the start of an entry."""
    if len(text) <= RECENT_MAX_CHARS:
        return text
    text = text[-RECENT_MAX_CHARS:]
    return text[text.find("\n[") + 1 :] if "\n[" in text else text


def log_announcement(text: str) -> None:
    """Log something said between turns (e.g. a task update) to the day's log."""
    _log_day(datetime.now(profile.tz()), f"JARVIS (announced): {text.strip()}\n\n")


def _log_day(now: datetime, entry: str) -> None:
    (folder := profile.current().conversations).mkdir(parents=True, exist_ok=True)
    path = folder / f"{now:%Y-%m-%d}.md"
    head = "" if path.exists() else f"# Conversations, {now:%A %-d %B %Y}\n\n"
    with path.open("a") as f:
        f.write(f"{head}[{now:%H:%M}] {entry}")


def day_entries(path: Path) -> list[str]:
    """A day file's "[HH:MM] ..." entries, oldest first, without the heading."""
    return [e.strip() for e in _ENTRY_START.split(path.read_text(errors="replace")) if e.startswith("[")]


def search_conversations(text: str = "", date: str = "", days: int = 0) -> str:
    """Log entries containing every word of `text`, newest first, on `date` or within `days` (else all days)."""
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
