"""The full conversation log (data/conversations, one file a day, written by jarvis.state.memory) as the web page's
History shows it: which days have conversations, one day's exchanges, and the exchanges that mention some words.

Everything comes back newest first, the days and the exchanges within a day alike, so scrolling down goes back in
time. An exchange is who spoke and what they said, the commands their words said yes to, and JARVIS's reply; something
JARVIS said between turns, such as a task update, is an announcement. Each install of JARVIS is one person's, with its
own data/ and passcode, so this is only ever that person's log.
"""

import re
from datetime import date as Date, datetime
from pathlib import Path

from jarvis.core import profile
from jarvis.state import memory

DAY = re.compile(r"\d{4}-\d{2}-\d{2}")
DAYS_PAGE = 30  # days listed at once, before "older"
SEARCH_MOST = 100  # matching exchanges returned at once
PREVIEW_CHARS = 140
_HEAD = re.compile(r"\[(\d{2}:\d{2})\] ([^:\n]{1,60}?)( \(announced\))?: ")
_TIME = re.compile(r"\[(\d{2}:\d{2})\] ?")
_APPROVED = re.compile(r"\(Approved: (.*)\)")


def _is_day(text: str) -> bool:
    try:
        return bool(DAY.fullmatch(text)) and bool(Date.fromisoformat(text))
    except ValueError:
        return False


def _check(day: object) -> str:
    if not isinstance(day, str) or not _is_day(day):
        raise ValueError("date must be a date, as YYYY-MM-DD.")
    return day


def _files() -> list[Path]:
    """The day files, newest first."""
    folder = profile.current().conversations
    return sorted((f for f in folder.glob("????-??-??.md") if _is_day(f.stem)), reverse=True)


def _parse(raw: str) -> dict:
    """One logged entry (jarvis.state.memory.log_exchange and log_announcement): {"time", "kind", ...}. An exchange
    ("exchange") has "who", "said", "approved", "reply" and "interrupted"; something JARVIS said between turns
    ("announcement") has "said"; anything else, such as a hand edit, is a "note" with its text as written."""
    head = _HEAD.match(raw)
    if not head:
        found = _TIME.match(raw)
        return {"time": found[1] if found else "", "kind": "note", "said": raw[found.end():] if found else raw}
    time, who, announced, rest = head[1], head[2].strip(), head[3], raw[head.end():]
    if announced or who == "JARVIS":
        return {"time": time, "kind": "announcement", "said": rest.strip()}
    said, approved, reply = [], [], None
    for line in rest.split("\n"):
        if reply is not None:
            reply.append(line)
        elif line == "JARVIS:" or line.startswith("JARVIS: "):
            reply = [line.removeprefix("JARVIS:").removeprefix(" ")]
        elif yes := _APPROVED.fullmatch(line):
            approved.append(yes[1])
        else:
            said.append(line)
    text = "\n".join(reply or []).strip()
    interrupted = text == memory.NOT_REPLIED or text.endswith(memory.CUT_OFF)
    if interrupted:
        text = "" if text == memory.NOT_REPLIED else text.removesuffix(memory.CUT_OFF).rstrip()
    return {"time": time, "kind": "exchange", "who": who, "said": "\n".join(said).strip(), "approved": approved,
            "reply": text, "interrupted": interrupted}


def _entries(path: Path) -> list[dict]:
    """A day file's entries, newest first, each numbered by its place in the day (oldest is 1)."""
    return [_parse(e) | {"n": n} for n, e in reversed(list(enumerate(memory.day_entries(path), 1)))]


def _preview(entries: list[dict]) -> str:
    """The latest thing the user said that day, or failing that the latest thing JARVIS said, shortened."""
    found = next((e["said"] for e in entries if e["kind"] == "exchange" and e["said"]), "") \
        or next((e.get("reply") or e["said"] for e in entries), "")
    found = " ".join(found.split())
    return found if len(found) <= PREVIEW_CHARS else found[:PREVIEW_CHARS - 1].rstrip() + "…"


def days(before: str = "", limit: int = DAYS_PAGE) -> dict:
    """The days with conversations, newest first: up to `limit` of them, older than `before` (YYYY-MM-DD) if given.
    Each says how many exchanges it holds, its first and last times and a preview. `more` says whether there are older
    days still; `today` is the date on the Mac, for the page to call it Today."""
    if before:
        _check(before)
    files = [f for f in _files() if not before or f.stem < before]
    shown = []
    for f in files[:limit]:
        entries = _entries(f)
        shown.append({"date": f.stem, "count": len(entries), "exchanges": sum(e["kind"] == "exchange" for e in entries),
                      "first": entries[-1]["time"] if entries else "", "last": entries[0]["time"] if entries else "",
                      "preview": _preview(entries)})
    return {"days": shown, "more": len(files) > limit, "today": f"{datetime.now(profile.tz()):%Y-%m-%d}"}


def day(on: str) -> dict:
    """One day's entries, newest first, with the days either side that have conversations (None at either end)."""
    path = profile.current().conversations / f"{_check(on)}.md"
    stems = [f.stem for f in _files()]
    return {"date": on, "entries": _entries(path) if path.is_file() else [],
            "newer": min((s for s in stems if s > on), default=None),
            "older": max((s for s in stems if s < on), default=None)}


def _haystack(entry: dict) -> str:
    return " ".join([entry.get("who", ""), entry["said"], *entry.get("approved", []), entry.get("reply", "")]).lower()


def search(text: str, limit: int = SEARCH_MOST) -> dict:
    """The entries that contain every word of `text`, in any case, newest first: up to `limit` of them, each with its
    date. `more` says whether older ones were left out."""
    if not (words := text.lower().split()):
        raise ValueError("Say what to look for.")
    hits: list[dict] = []
    for f in _files():
        for e in _entries(f):
            if all(w in _haystack(e) for w in words):
                if len(hits) == limit:
                    return {"text": " ".join(words), "hits": hits, "more": True}
                hits.append(e | {"date": f.stem})
    return {"text": " ".join(words), "hits": hits, "more": False}
