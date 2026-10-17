"""Learning review: after a conversation ends, suggest what to keep (memory, to-dos, skills, vocabulary).

Runs in the task runner every CHECK_SECONDS. Progress mark (day file + byte offset) lives in data/learning.json.
A conversation is over when the voice agent reports a hang-up (POST /learn) and END_GRACE_SECONDS pass without a
reconnect, or after LEARNING_QUIET_MINUTES of silence. The mark moves before saving, so nothing is reviewed twice;
on first run the existing log is only marked read. Too few user words: skipped without a call.
One tool-less learning-role call returns JSON suggestions, saved as a hidden `review` task's edits (data_edits
checks). Routine ones (new facts, misheard words) apply at once; rewrites, deletions, to-dos, skills and facts
saying work is finished need a yes.
Recent requests (HISTORY_DAYS) let it propose skills for repeated workflows. JARVIS_LEARNING=off disables it.
"""

import asyncio
import json
import logging
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from jarvis import prompts
from jarvis.config import LEARNING_QUIET_MINUTES
from jarvis.core.files import write_atomic
from jarvis.llm import retry
from jarvis.llm.roles import Role
from jarvis.skills import home as skills
from jarvis.state import memory, vocabulary
from jarvis.storage import profile

log = logging.getLogger("jarvis.tasks.learning")
KIND, TITLE = "review", "learning review"  # hidden task holding a review's suggestions
CHECK_SECONDS = 60
END_GRACE_SECONDS = 60  # after a hang-up, window for a reconnect to continue the conversation
MIN_WORDS = 12  # min user words since the last review
MAX_CHARS = 30_000  # conversation sent to the model (latest part)
MAX_SUGGESTIONS = 5
MAX_WORDS = 5  # misheard words, on top of MAX_SUGGESTIONS
HISTORY_DAYS = 21  # lookback for spotting repeated workflows
HISTORY_CHARS = 8_000  # cap on those requests (latest kept)
CALL_TIMEOUT = 180
# Transient failures retry twice, 15 minutes apart; after that, or on other errors, the exchanges are skipped
RETRIES = retry.Policy(waits=(15 * 60, 15 * 60))
_ENTRY = re.compile(r"^\[(\d{2}:\d{2})\] ", re.MULTILINE)

def system() -> str:
    return prompts.load("learning_review", user_name=profile.name(), max_suggestions=MAX_SUGGESTIONS,
                        max_words=MAX_WORDS)


@dataclass
class Unread:
    """Exchanges logged since the last review."""
    parts: list[tuple[str, str]]  # (day, text) per day file, oldest first
    file: str  # new mark: newest day file and its size
    offset: int
    written: float  # mtime of the newest file


def _user_lines(text: str) -> list[str]:
    """The user's own lines in these log entries."""
    return re.findall(rf"^\[\d{{2}}:\d{{2}}\] {re.escape(profile.name())}: (.*)$", text, re.MULTILINE)


def user_words(text: str) -> int:
    """Word count of the user's own lines."""
    return sum(len(line.split()) for line in _user_lines(text))


def span(parts: list[tuple[str, str]]) -> str:
    """Time range of the entries, e.g. '2026-10-07 14:02 to 14:35'."""
    times = [(day, t) for day, text in parts for t in _ENTRY.findall(text)]
    if not times:
        return parts[0][0] if parts else ""
    (d1, t1), (d2, t2) = times[0], times[-1]
    return f"{d1} {t1} to {t2 if d1 == d2 else f'{d2} {t2}'}"


def transcript(parts: list[tuple[str, str]], limit: int = MAX_CHARS) -> str:
    """Exchanges grouped under day headings, trimmed to the latest `limit` chars at an entry boundary."""
    text = "\n".join(f"## {day}\n{chunk.strip()}\n" for day, chunk in parts)
    if len(text) > limit:
        text = text[-limit:]
        cut = _ENTRY.search(text)
        text = "(earlier exchanges left out)\n" + (text[cut.start():] if cut else text)
    return text


def numbered() -> list[tuple[str, str]]:
    """Memory lines as (section, line), in the 1-based order the review references."""
    found = memory.facts()
    return [(s, line) for s in memory.SECTIONS for line in found[s]]


def memory_block(lines: list[tuple[str, str]]) -> str:
    """Memory for the prompt: each section with its usage and numbered lines."""
    out = []
    for s in memory.SECTIONS:
        mine = [(n, line) for n, (sec, line) in enumerate(lines, 1) if sec == s]
        used = memory.usage([line for _, line in mine])
        out.append(f"{s} ({memory.title(s)}, {used:,} of {memory.LIMITS[s]:,} characters used):")
        out += [f"{n}. {line}" for n, line in mine] or ["(nothing yet)"]
    return "\n".join(out)


def earlier(mark: dict | None, now: datetime, days: int = HISTORY_DAYS, limit: int = HISTORY_CHARS) -> str:
    """User lines from the last `days` days before the mark, one per line with date and time, latest `limit` chars."""
    folder = profile.current().conversations
    if mark is None or not folder.exists():
        return ""
    since = f"{datetime.fromtimestamp(now.timestamp() - days * 86400, profile.tz()):%Y-%m-%d}.md"
    said = rf"^\[(\d{{2}}:\d{{2}})\] {re.escape(profile.name())}: (.*)$"
    lines = []
    for f in sorted(folder.glob("????-??-??.md")):
        if f.name < since or f.name > mark["file"]:
            continue
        data = f.read_bytes()
        text = (data[:mark["offset"]] if f.name == mark["file"] else data).decode(errors="replace")
        lines += [f"{f.stem} {t}: {' '.join(words.split())}" for t, words in re.findall(said, text, re.MULTILINE)]
    out = "\n".join(lines)
    if len(out) > limit:
        out = out[-limit:]
        out = out[out.find("\n") + 1:]
    return out


def vocabulary_block() -> str:
    """Speech vocabulary for the prompt, with known mishearings."""
    found = vocabulary.entries()
    return "\n".join(f"- {e.word}" + (f" (heard as: {', '.join(e.heard)})" if e.heard else "") for e in found) or "(none yet)"


def said_by_user(parts: list[tuple[str, str]]) -> str:
    """The user's own lines across `parts`, newline-joined."""
    return "\n".join(line for _, text in parts for line in _user_lines(text))


def skills_block() -> str:
    found = skills.all_skills()
    return "\n".join(f"- {s.name} ({s.state}): {s.description}" for s in found) or "(none yet)"


def request(parts: list[tuple[str, str]], lines: list[tuple[str, str]], now: datetime, history: str = "",
            have: str = "(none yet)", words: str = "(none yet)") -> str:
    return (f"Today is {now:%A %-d %B %Y}; it's {now:%H:%M} now.\n\n<long_term_memory>\n{memory_block(lines)}\n</long_term_memory>\n\n"
            f"<skills>\n{have}\n</skills>\n\n<earlier_requests>\n{history or '(none)'}\n</earlier_requests>\n\n"
            f"<vocabulary>\n{words}\n</vocabulary>\n\n<conversation>\n{transcript(parts)}</conversation>")


def parse(reply: str) -> list[dict]:
    """Suggestions from the model reply, capped at MAX_SUGGESTIONS (+ MAX_WORDS vocabulary); [] if not valid JSON."""
    found = re.search(r"\{.*\}", reply or "", re.DOTALL)
    try:
        listed = json.loads(found[0])["suggestions"] if found else []
    except (ValueError, KeyError, TypeError):
        return []
    if not isinstance(listed, list):
        return []
    listed = [s for s in listed if isinstance(s, dict)]
    words = [s for s in listed if s.get("action") == "vocabulary"]
    return [s for s in listed if s.get("action") != "vocabulary"][:MAX_SUGGESTIONS] + words[:MAX_WORDS]


def _same(a: str, b: str) -> bool:
    return " ".join(a.lower().split()).strip(" .") == " ".join(b.lower().split()).strip(" .")


def _fact(line: str) -> str:
    """Memory line without its leading dash and trailing date."""
    return memory.plain(line)


def to_edits(suggestions: list[dict], lines: list[tuple[str, str]], today: str, said: str = "") -> list[dict]:
    """Convert suggestions to propose_data_edit args, resolving line numbers to memory text.

    Mishearings are kept only if they occur in `said` (the user's words). Malformed suggestions are dropped;
    data_edits.check validates the rest.
    """
    out = []
    for s in suggestions:
        action, reason = s.get("action"), " ".join(str(s.get("reason") or "").split()) or "From the conversation."
        fact, item = memory.plain(str(s.get("fact") or "")), str(s.get("item") or "").strip()
        if action == "vocabulary":
            word, heard = " ".join(str(s.get("word") or "").split()), s.get("heard_as")
            heard = [" ".join(h.split()) for h in heard if isinstance(h, str)] if isinstance(heard, list) else []
            if word and not any(e["action"] == "vocabulary" and vocabulary.same(e["word"], word) for e in out):
                kept = [h for h in heard if vocabulary.said_in(h, said) and not vocabulary.same(h, word)]
                out.append({"action": "vocabulary", "word": word, "reason": reason} | ({"heard_as": kept} if kept else {}))
            continue
        if action == "skill":
            name, about = skills.slug(str(s.get("name") or "")), " ".join(str(s.get("description") or "").split())
            steps = str(s.get("instructions") or "").strip()
            if name and about and steps and not any(e["action"] == "skill" for e in out):
                out.append({"action": "skill", "name": name, "description": about, "instructions": steps,
                            "reason": reason})
            continue
        try:
            n = int(s.get("line"))
            line = lines[n - 1][1] if n >= 1 else None
        except (TypeError, ValueError, IndexError):
            line = None
        if action == "remember" and fact:
            if any(_same(_fact(have), fact) for _, have in lines):
                continue
            out.append({"action": "remember", "section": str(s.get("section") or ""), "fact": fact, "reason": reason})
        elif action == "update" and fact and line and not _same(_fact(line), fact):
            new = f"- {fact} ({today})"
            out.append({"action": "edit", "file": "memory.md", "old": line, "new": new, "reason": reason})
        elif action == "forget" and line:
            out.append({"action": "forget", "text": line.removeprefix("- "), "reason": reason})
        elif action == "todo_add" and item:
            out.append({"action": "todo_add", "item": item, "reason": reason})
        elif action == "follow_up" and (text := " ".join(str(s.get("text") or "").split())) and s.get("when"):
            out.append({"action": "follow_up", "text": text, "when": str(s["when"]).strip(), "reason": reason})
    return out


async def model_review(prompt: str) -> str:
    """One tool-less learning-role call; returns the reply text."""
    from jarvis.llm.claude import ask

    return (await ask(prompt, system(), Role.LEARNING, timeout=CALL_TIMEOUT))[0]


Save = Callable[[list[dict], str], Awaitable[object]]


class Learning:
    """Watch the conversation log and review each finished conversation.

    `save(edits, about)` stores suggestions (Runner.learned); `review(prompt)` is the model call.
    """

    def __init__(
        self,
        save: Save,
        review: Callable[[str], Awaitable[str]] = model_review,
        state_file: Path | None = None,
        quiet_minutes: int = LEARNING_QUIET_MINUTES,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[object]] = asyncio.sleep,
    ) -> None:
        self.save = save
        self.review = review
        self._state_file = state_file
        self.quiet = quiet_minutes * 60
        self.clock = clock
        self.sleep = sleep
        self.ended_at: float | None = None  # last hang-up reported by the voice agent
        self.failures, self.retry_at = 0, 0.0

    @property
    def state_file(self) -> Path:
        """Read-progress file (profile default unless overridden)."""
        return self._state_file or profile.current().learning_file

    def ended(self) -> None:
        """Note a hang-up; review after END_GRACE_SECONDS unless the user reconnects."""
        self.ended_at = self.clock()

    async def run(self) -> None:
        while True:
            try:
                await self.check()
            except Exception:
                log.exception("learning review failed")
            await self.sleep(CHECK_SECONDS)

    def _mark(self) -> dict | None:
        try:
            state = json.loads(self.state_file.read_text())
            return {"file": str(state["file"]), "offset": int(state["offset"])}
        except FileNotFoundError:
            return None
        except (ValueError, KeyError, TypeError):
            log.warning("learning review state in %s is unreadable; starting from now", self.state_file.name)
            return None

    def _move_mark(self, file: str, offset: int) -> None:
        write_atomic(self.state_file, json.dumps({"file": file, "offset": offset, "at": self.clock()}))

    def unread(self) -> Unread | None:
        """Exchanges since the mark, or None. With no mark, set it at the log's end (no backlog review)."""
        folder = profile.current().conversations
        files = sorted(folder.glob("????-??-??.md")) if folder.exists() else []
        if not files:
            return None
        newest = files[-1]
        if (mark := self._mark()) is None:
            self._move_mark(newest.name, newest.stat().st_size)
            return None
        parts, end = [], 0
        for f in files:
            if f.name < mark["file"]:
                continue
            data = f.read_bytes()
            start = mark["offset"] if f.name == mark["file"] else 0
            start = 0 if start > len(data) else start  # file truncated by hand: reread it all
            if text := data[start:].decode(errors="replace"):
                parts.append((f.stem, text))
            end = len(data)
        return Unread(parts, newest.name, end, newest.stat().st_mtime) if parts else None

    def due(self, written: float, now: float) -> bool:
        """Whether the conversation is over: hang-up grace elapsed, or quiet long enough."""
        if self.ended_at is not None:
            if written > self.ended_at:  # talking again: conversation continues
                self.ended_at = None
            elif now - self.ended_at >= END_GRACE_SECONDS:
                return True
        return now - written >= self.quiet

    async def check(self) -> bool:
        """Review a finished conversation if one is due. Returns whether a review ran."""
        now = self.clock()
        if (unread := self.unread()) is None or not self.due(unread.written, now) or now < self.retry_at:
            return False
        self.ended_at = None
        words = sum(user_words(text) for _, text in unread.parts)
        if words < MIN_WORDS:
            self._move_mark(unread.file, unread.offset)
            log.info("learning review: %d words from %s since the last one, skipped", words, profile.name())
            return False
        lines = numbered()
        stamp = datetime.fromtimestamp(now, profile.tz())
        prompt = request(unread.parts, lines, stamp, earlier(self._mark(), stamp), skills_block(), vocabulary_block())
        try:
            reply = await self.review(prompt)  # model_review has its own timeout
        except Exception as e:  # log only the type: keep conversation text out of logs
            self.failures += 1
            kind = RETRIES.kind(e)
            log.warning("learning review call failed (try %d, %s): %s", self.failures, kind, type(e).__name__)
            if (wait := RETRIES.wait(self.failures, kind)) is not None:
                self.retry_at = now + wait
                return False
            reply = ""  # give up: skip these exchanges
        self.failures, self.retry_at = 0, 0.0
        self._move_mark(unread.file, unread.offset)  # before saving: never review twice
        edits = to_edits(parse(reply), lines, f"{stamp:%Y-%m-%d}", said_by_user(unread.parts))
        when = span(unread.parts)
        log.info("learning review of %s: %d suggestions", when, len(edits))
        if edits:
            await self.save(edits, f"Review the conversation of {when} for lasting facts.")
        return True
