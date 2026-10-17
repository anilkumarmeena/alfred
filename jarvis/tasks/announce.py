"""Spoken announcement of a task's end: a sentence or two worded by one tool-less announce-role call.

Input is an `Ending`: outcome and report, what the goal check found missing, what JARVIS already did, and pending
suggested edits. `lead` (words already fit to say) is kept verbatim and the rest appended. Falls back to `plain`
fixed wording when JARVIS_ANNOUNCE=off, on error, past ANNOUNCE_SECONDS, or at the usage limit.

Only the spoken text is produced here; structured facts stay on the task. `context` gives the voice brain the task
number and pending edits, unspoken. `first` and `brief` pick and shorten the update a greeting leads with.
"""

import asyncio
import logging
import re
from dataclasses import dataclass, field

from jarvis import prompts
from jarvis.config import ANNOUNCE_ENABLED, ANNOUNCE_SECONDS
from jarvis.llm import usage
from jarvis.llm.roles import Role
from jarvis.storage import profile
from jarvis.tasks import data_edits
from jarvis.tasks.scheduler import headline, sentence

log = logging.getLogger("jarvis.tasks.announce")

def system() -> str:
    return prompts.load("task_announcement", user_name=profile.name())
REPORT_MOST = 4000  # chars of the report the model reads
SAID_MOST = 600  # max chars spoken
BRIEF_MOST = 200  # chars of an update a greeting leads with, in whole sentences
_SENTENCE = re.compile(r"(?<=[.!?…])\s+")
DONE, FAILED, STOPPED, QUESTION = "done", "failed", "stopped", "question"


@dataclass
class Ending:
    """Facts about a task's end, for wording it."""
    title: str
    status: str  # DONE, FAILED, STOPPED or QUESTION
    report: str = ""  # summary, error, or question
    lead: str = ""  # said first, verbatim
    left: str = ""  # what the goal check found missing
    needs_user: bool = False  # `left` is for the user, not the task
    done: list[str] = field(default_factory=list)  # already done: "ticked off your to-do ..."
    suggested: list[tuple[str, str]] = field(default_factory=list)  # pending edits: (what, why)
    undelivered: bool = False  # a user message missed the task; it goes with their next reply


def suggestions(task: dict) -> list[tuple[str, str]]:
    """Pending edits as (plain description, reason)."""
    return [(data_edits.describe(e), e.get("reason", "")) for e in data_edits.pending(task)]


def facts(e: Ending) -> str:
    """The prompt for the wording model."""
    parts = [f"<task>{e.title}</task>",
             f"<ended>{'asked a question and waits for the answer' if e.status == QUESTION else e.status}</ended>"]
    if e.lead:
        parts.append(f"<said_first>{e.lead}</said_first>")
    if e.status == QUESTION:
        parts.append(f"<question>{e.report}</question>")
    elif report := e.report.strip():
        parts.append(f"<report>{report[:REPORT_MOST]}{' …' if len(report) > REPORT_MOST else ''}</report>")
    if e.left:
        parts.append(f"<check>{'Needs ' + profile.name() if e.needs_user else 'May be unfinished'}: {e.left}</check>")
    if e.done:
        parts.append("<done>\n" + "\n".join(f"- {d}" for d in e.done) + "\n</done>")
    if e.suggested:
        listed = "\n".join(f"- {what}" + (f" (why: {why})" if why else "") for what, why in e.suggested)
        parts.append(f"<suggestions>\n{listed}\n</suggestions>")
    if e.undelivered:
        parts.append(f"<note>{profile.name()} sent the task a message it didn't get in time: it goes with their next "
                     "reply.</note>")
    return "\n".join(parts)


def joined(items: list[str]) -> str:
    """"a", "a and b", "a, b and c"."""
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


def plain(e: Ending) -> str:
    """Fixed-wording fallback."""
    name = f"Your task, {e.title},"
    head = e.lead or {DONE: f"{name} is done. {headline(e.report)}",
                      FAILED: f"{name} failed: {sentence(e.report)}",
                      STOPPED: f"{name} stopped.",
                      QUESTION: f"{name} has a question: {e.report}"}[e.status]
    parts = [head.strip()]
    if e.left:
        parts.append(f"{'It still needs you' if e.needs_user else 'It may not be finished'}: {sentence(e.left)}")
    parts += [sentence(f"I've {d}") for d in e.done]
    if len(e.suggested) > 2:
        parts.append(f"It has {len(e.suggested)} suggestions for your lists and my memory.")
    elif e.suggested:
        parts.append(sentence(f"It'd also like to {joined([what for what, _ in e.suggested])}"))
    if e.undelivered:
        parts.append("Your message to it wasn't delivered; it goes with your next reply.")
    return " ".join(p for p in parts if p)


async def ask_model(text: str) -> str:
    """One tool-less announce-role call; returns its reply."""
    from jarvis.llm.claude import ask

    reply, _ = await ask(text, system(), Role.ANNOUNCE, effort="low")
    return reply


async def spoken(e: Ending) -> str:
    """The words to speak: model-worded, or `plain` on any fallback."""
    if not (e.report or e.left or e.done or e.suggested or e.undelivered):
        return plain(e)  # nothing to word
    if not ANNOUNCE_ENABLED or usage.out():
        return plain(e)
    try:
        worded = " ".join((await asyncio.wait_for(ask_model(facts(e)), ANNOUNCE_SECONDS)).split())
    except Exception as ex:  # log the type only: no task content
        log.warning("announcement wording failed: %s", type(ex).__name__)
        return plain(e)
    if not worded:
        return plain(e)
    if len(worded) > SAID_MOST:
        worded = worded[:SAID_MOST].rsplit(" ", 1)[0] + "…"
    return f"{e.lead} {worded}" if e.lead else worded


def _weight(task: dict) -> int:
    """How much a waiting update matters, most first: it needs the user, it failed, work finished, then routine
    (scheduled runs, anything else). A fix of a scheduled run inherits its job but isn't routine."""
    if task["status"] == "waiting":
        return 0
    if task["status"] == FAILED:
        return 1
    return 2 if task["status"] == DONE and (not task.get("job") or task.get("fixes")) else 3


def first(tasks: list[dict]) -> dict | None:
    """The update to lead with: the weightiest with words to say, the newest among equals."""
    return min((t for t in tasks if (t.get("spoken") or "").strip()),
               key=lambda t: (_weight(t), -t.get("updated", 0)), default=None)


def brief(task: dict) -> tuple[str, bool]:
    """The task's latest update, cut to whole sentences within BRIEF_MOST chars (at least one), and whether nothing was
    cut. A question is kept whole: it ends with what's asked."""
    said = " ".join(task["spoken"].split())
    if task["status"] == "waiting":
        return said, True
    sentences = _SENTENCE.split(said)
    kept, n = sentences[0], 1
    for s in sentences[1:]:
        if len(kept) + 1 + len(s) > BRIEF_MOST:
            break
        kept, n = kept + " " + s, n + 1
    return sentence(kept), n == len(sentences)


def context(task: dict) -> str:
    """Unspoken note for the voice brain: task id and numbered pending edits (for task_edits).

    Edits are omitted while the task is waiting on a question."""
    said = f"The update on \"{task['title']}\" was task {task['id']}."
    if task["status"] != "waiting" and (waiting := data_edits.pending(task)):
        said += " Its suggested edits, pending: " + "; ".join(
            f"{e['n']}. {data_edits.describe(e)}" + (f" (needs a yes: {e['needs_yes']})" if e.get("needs_yes") else "")
            for e in waiting) + "."
    return said
