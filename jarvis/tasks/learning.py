"""The learning review: once a conversation is over, JARVIS reads it back and suggests what to keep in long-term memory,
so the user needn't say "remember" every time.

It runs inside the task runner, beside the heartbeat. Every CHECK_SECONDS it looks at data/conversations for exchanges
it hasn't reviewed yet (data/learning.json keeps how far it has read: a day's file and a byte offset), and reviews them
when the conversation is over:

  hung up    the voice agent tells the runner its session ended (POST /learn); END_GRACE_SECONDS later, unless the
             user is talking again by then (a dropped call that reconnected), the review runs
  quiet      nothing new has been logged for LEARNING_QUIET_MINUTES

Never per message: each review takes every exchange since the last one, and moves the mark past them before saving
anything, so no exchange is reviewed twice. The first time it runs, what's already logged is only marked as read, like
the heartbeat's first look at mail. A conversation with too few of the user's own words is passed over without a call.

The review is one call to LEARNING_MODEL (Haiku unless set), with no tools: its reply is read only as JSON listing
suggestions, which become the edits of a hidden `review` task, through the same checks as any task's suggested edits
(jarvis.tasks.data_edits). The runner applies the routine ones straight away, as JARVIS would; the rest (rewriting
or deleting memory lines, adding to-dos) are marked needs_yes and announced, so JARVIS asks first. A review with
nothing worth keeping says nothing at all. JARVIS_LEARNING=off turns it off.

It also reads the user's own requests from the last HISTORY_DAYS of earlier conversations, and the skills JARVIS has
(jarvis.skills.home), so when the user has asked for the same kind of multi-step workflow again and again it can
suggest a skill for it, or a better version of one that this conversation showed falling short. A skill is always
needs_yes: the review never saves, installs or switches on a skill itself.

And it spots words speech recognition misheard (the user corrected one, spelt it out, or a word in their line makes no
sense but plainly stands for a name or term), and adds them to the speech vocabulary (jarvis.state.vocabulary) with
what Whisper wrote, applied without asking like a routine memory fact, as a wrong word costs little. A mishearing is
kept only if it's in the user's own words in the conversation, and isn't an everyday word (data_edits).
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
from jarvis.brain import retry
from jarvis.core import profile
from jarvis.core.config import LEARNING_MODEL, LEARNING_QUIET_MINUTES
from jarvis.core.files import write_atomic
from jarvis.skills import home as skills
from jarvis.state import memory, vocabulary

log = logging.getLogger("jarvis.tasks.learning")
KIND, TITLE = "review", "learning review"  # the task its suggestions are kept on
CHECK_SECONDS = 60
END_GRACE_SECONDS = 60  # after a hang-up, how long a reconnect has to carry the same conversation on
MIN_WORDS = 12  # of the user's own, in the exchanges since the last review; fewer have nothing to learn from
MAX_CHARS = 30_000  # of the conversation sent to the model: the latest part, if it's longer
MAX_SUGGESTIONS = 5
MAX_WORDS = 5  # misheard words, apart from the suggestions
HISTORY_DAYS = 21  # of earlier conversations whose requests the review sees, to tell a workflow asked for again and again
HISTORY_CHARS = 8_000  # of those requests: the latest
CALL_TIMEOUT = 180
# A review that fails on a passing error (jarvis.brain.retry) is tried again 15 minutes later, twice; then, or on any
# other error, the exchanges are passed over
RETRIES = retry.Policy(waits=(15 * 60, 15 * 60))
_ENTRY = re.compile(r"^\[(\d{2}:\d{2})\] ", re.MULTILINE)

def system() -> str:
    return prompts.load("learning_review", user_name=profile.name(), max_suggestions=MAX_SUGGESTIONS,
                        max_words=MAX_WORDS)


@dataclass
class Unread:
    """The exchanges logged since the last review."""
    parts: list[tuple[str, str]]  # (day, text) for each day's file, oldest first
    file: str  # where reading stopped: the newest day's file, and its size then
    offset: int
    written: float  # when anything was last logged


def _user_lines(text: str) -> list[str]:
    """What the user said in these log entries, line by line (JARVIS's replies and announcements left out)."""
    return re.findall(rf"^\[\d{{2}}:\d{{2}}\] {re.escape(profile.name())}: (.*)$", text, re.MULTILINE)


def user_words(text: str) -> int:
    """How many words the user said in these log entries (JARVIS's replies and announcements don't count)."""
    return sum(len(line.split()) for line in _user_lines(text))


def span(parts: list[tuple[str, str]]) -> str:
    """'2026-10-07 14:02 to 14:35', from the first and last entries' times."""
    times = [(day, t) for day, text in parts for t in _ENTRY.findall(text)]
    if not times:
        return parts[0][0] if parts else ""
    (d1, t1), (d2, t2) = times[0], times[-1]
    return f"{d1} {t1} to {t2 if d1 == d2 else f'{d2} {t2}'}"


def transcript(parts: list[tuple[str, str]], limit: int = MAX_CHARS) -> str:
    """The exchanges as the model reads them, each day under its date; only the latest `limit` characters of a long
    conversation, cut at an entry."""
    text = "\n".join(f"## {day}\n{chunk.strip()}\n" for day, chunk in parts)
    if len(text) > limit:
        text = text[-limit:]
        cut = _ENTRY.search(text)
        text = "(earlier exchanges left out)\n" + (text[cut.start():] if cut else text)
    return text


def numbered() -> list[tuple[str, str]]:
    """Long-term memory's lines as (section, line), in the order the review numbers them from 1."""
    found = memory.facts()
    return [(s, line) for s in memory.SECTIONS for line in found[s]]


def memory_block(lines: list[tuple[str, str]]) -> str:
    """Memory as the review reads it: each section with how full it is, its lines numbered."""
    out = []
    for s in memory.SECTIONS:
        mine = [(n, line) for n, (sec, line) in enumerate(lines, 1) if sec == s]
        used = memory.usage([line for _, line in mine])
        out.append(f"{s} ({memory.title(s)}, {used:,} of {memory.LIMITS[s]:,} characters used):")
        out += [f"{n}. {line}" for n, line in mine] or ["(nothing yet)"]
    return "\n".join(out)


def earlier(mark: dict | None, now: datetime, days: int = HISTORY_DAYS, limit: int = HISTORY_CHARS) -> str:
    """The user's own words in the conversations logged before the mark (what's being reviewed starts there), from
    the last `days` days, one line each with its date and time: the latest `limit` characters."""
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
    """The speech vocabulary as the review reads it: each word, with what it's been misheard as."""
    found = vocabulary.entries()
    return "\n".join(f"- {e.word}" + (f" (heard as: {', '.join(e.heard)})" if e.heard else "") for e in found) or "(none yet)"


def said_by_user(parts: list[tuple[str, str]]) -> str:
    """The user's own lines in these log entries, one per line."""
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
    """The suggestions in the model's reply, at most MAX_SUGGESTIONS and MAX_WORDS misheard words; [] for anything
    that isn't the JSON asked for."""
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
    """A memory line's fact, without its dash and date."""
    return re.sub(r"\s*\(\d{4}-\d{2}-\d{2}\)$", "", line.removeprefix("- ")).strip()


def to_edits(suggestions: list[dict], lines: list[tuple[str, str]], today: str, said: str = "") -> list[dict]:
    """The suggestions as propose_data_edit's arguments (jarvis.tasks.data_edits), memory lines named by number
    turned into their exact text. A misheard word keeps only the mishearings found in `said`, the user's own words.
    What can't be made sense of is left out; data_edits.check judges the rest."""
    out = []
    for s in suggestions:
        action, reason = s.get("action"), " ".join(str(s.get("reason") or "").split()) or "From the conversation."
        fact, item = str(s.get("fact") or "").strip(), str(s.get("item") or "").strip()
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
            new = f"- {' '.join(fact.split())} ({today})"
            out.append({"action": "edit", "file": "memory.md", "old": line, "new": new, "reason": reason})
        elif action == "forget" and line:
            out.append({"action": "forget", "text": line.removeprefix("- "), "reason": reason})
        elif action == "todo_add" and item:
            out.append({"action": "todo_add", "item": item, "reason": reason})
        elif action == "follow_up" and (text := " ".join(str(s.get("text") or "").split())) and s.get("when"):
            out.append({"action": "follow_up", "text": text, "when": str(s["when"]).strip(), "reason": reason})
    return out


async def model_review(prompt: str) -> str:
    """One short call to LEARNING_MODEL with no tools; returns its reply."""
    from jarvis.brain.sdk import ask

    return (await ask(prompt, system(), LEARNING_MODEL, timeout=CALL_TIMEOUT))[0]


Save = Callable[[list[dict], str], Awaitable[object]]


class Learning:
    """Watches the conversation log and reviews each conversation once it's over. `save(edits, about)` keeps what a
    review suggests (Runner.learned); `review(prompt)` is the model call."""

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
        self.ended_at: float | None = None  # when the voice agent last said its session ended
        self.failures, self.retry_at = 0, 0.0

    @property
    def state_file(self) -> Path:
        """How far through the conversations it has read: the profile's record, unless given one."""
        return self._state_file or profile.current().learning_file

    def ended(self) -> None:
        """The voice session has ended: review it shortly, unless the user reconnects and carries on."""
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
        """What's been logged since the mark, or None. With no mark yet, it's set at the end of the log first, so the
        exchanges from before the review existed aren't reviewed as a backlog."""
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
            start = 0 if start > len(data) else start  # the file was cut short by hand: read it all again
            if text := data[start:].decode(errors="replace"):
                parts.append((f.stem, text))
            end = len(data)
        return Unread(parts, newest.name, end, newest.stat().st_mtime) if parts else None

    def due(self, written: float, now: float) -> bool:
        """Whether the conversation is over: hung up a little while ago without carrying on, or quiet long enough."""
        if self.ended_at is not None:
            if written > self.ended_at:  # talking again since: the conversation carries on
                self.ended_at = None
            elif now - self.ended_at >= END_GRACE_SECONDS:
                return True
        return now - written >= self.quiet

    async def check(self) -> bool:
        """One look at the log; reviews a conversation that's over. Returns whether a review ran."""
        now = self.clock()
        if (unread := self.unread()) is None or not self.due(unread.written, now) or now < self.retry_at:
            return False
        self.ended_at = None
        words = sum(user_words(text) for _, text in unread.parts)
        if words < MIN_WORDS:  # announcements alone, or a word or two: nothing to learn from
            self._move_mark(unread.file, unread.offset)
            log.info("learning review: %d words from %s since the last one, skipped", words, profile.name())
            return False
        lines = numbered()
        stamp = datetime.fromtimestamp(now, profile.tz())
        prompt = request(unread.parts, lines, stamp, earlier(self._mark(), stamp), skills_block(), vocabulary_block())
        try:
            reply = await self.review(prompt)  # model_review has its own timeout
        except Exception as e:  # the type alone keeps the conversation out of the log
            self.failures += 1
            kind = RETRIES.kind(e)
            log.warning("learning review call failed (try %d, %s): %s", self.failures, kind, type(e).__name__)
            if (wait := RETRIES.wait(self.failures, kind)) is not None:
                self.retry_at = now + wait
                return False
            reply = ""  # given up on: these exchanges are passed over
        self.failures, self.retry_at = 0, 0.0
        self._move_mark(unread.file, unread.offset)  # before saving, so nothing is ever reviewed twice
        edits = to_edits(parse(reply), lines, f"{stamp:%Y-%m-%d}", said_by_user(unread.parts))
        when = span(unread.parts)
        log.info("learning review of %s: %d suggestions", when, len(edits))
        if edits:
            await self.save(edits, f"Review the conversation of {when} for lasting facts.")
        return True
