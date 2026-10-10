"""How a background task's end is said: in a sentence or two, worded by a short model call from what happened.

The runner gathers the facts of an ending (`Ending`): how the task ended and what it reported, what JARVIS's check of
the work found still missing or waiting on the user (jarvis.tasks.goal), what JARVIS did about it already (a to-do
ticked off), and the edits it suggests that wait for the user's word (jarvis.tasks.data_edits). One call to
ANNOUNCE_MODEL, with no tools, words them as JARVIS would say them in person (prompts/task_announcement.md): the
outcome, then what the user needs to decide, without task numbers, file names or internal terms, and each thing once.
The model decides the wording; nothing here matches words. Words a run already has fit to say (`lead`: a scheduled
run's result, a command's fix) are kept as they are, the rest added after them, and an ending with nothing more to say
needs no call. With the call off (JARVIS_ANNOUNCE=off), failing, slower than ANNOUNCE_SECONDS, or Claude's usage
limit reached, `plain` says the same facts in fixed words.

What's said is only what's said: the facts stay structured on the task (its summary, log and suggested edits) for the
tools, and `context` gives the voice brain the task's number and its pending edits beside the words, unsaid.
"""

import asyncio
import logging
from dataclasses import dataclass, field

from jarvis import prompts
from jarvis.brain import usage
from jarvis.core import profile
from jarvis.core.config import ANNOUNCE_ENABLED, ANNOUNCE_MODEL, ANNOUNCE_SECONDS
from jarvis.tasks import data_edits
from jarvis.tasks.scheduler import headline, sentence

log = logging.getLogger("jarvis.tasks.announce")

def system() -> str:
    return prompts.load("task_announcement", user_name=profile.name())
REPORT_MOST = 4000  # of the task's report, what the wording reads
SAID_MOST = 600  # what's said, at most: a few sentences
DONE, FAILED, STOPPED, QUESTION = "done", "failed", "stopped", "question"


@dataclass
class Ending:
    """What's known of how a task ended, for saying it."""
    title: str
    status: str  # DONE, FAILED, STOPPED or QUESTION
    report: str = ""  # its summary, its error, or its question
    lead: str = ""  # words already fit to say, said first as they are
    left: str = ""  # what JARVIS's check found still missing, or needing the user
    needs_user: bool = False  # what's `left` is for the user, not the task
    done: list[str] = field(default_factory=list)  # what JARVIS did about it already: "ticked off your to-do ..."
    suggested: list[tuple[str, str]] = field(default_factory=list)  # its pending edits: what, in plain words, and why
    undelivered: bool = False  # a message the user sent it went undelivered: it goes with their next reply


def suggestions(task: dict) -> list[tuple[str, str]]:
    """The task's edits waiting for the user's word: what each does, in a few plain words, and why."""
    return [(data_edits.describe(e), e.get("reason", "")) for e in data_edits.pending(task)]


def facts(e: Ending) -> str:
    """What the wording model reads."""
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


def _and(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


def plain(e: Ending) -> str:
    """The ending in fixed words, for when the model can't word it."""
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
        parts.append(sentence(f"It'd also like to {_and([what for what, _ in e.suggested])}"))
    if e.undelivered:
        parts.append("Your message to it wasn't delivered; it goes with your next reply.")
    return " ".join(p for p in parts if p)


async def ask_model(text: str) -> str:
    """One short call to ANNOUNCE_MODEL with no tools: its reply."""
    from jarvis.brain.sdk import ask

    reply, _ = await ask(text, system(), ANNOUNCE_MODEL, effort="low")
    return reply


async def spoken(e: Ending) -> str:
    """What to say of the ending: worded by the model from its facts, or in fixed words (plain)."""
    if not (e.report or e.left or e.done or e.suggested or e.undelivered):
        return plain(e)  # nothing to word: its own words, or a bare stop
    if not ANNOUNCE_ENABLED or usage.out():
        return plain(e)
    try:
        worded = " ".join((await asyncio.wait_for(ask_model(facts(e)), ANNOUNCE_SECONDS)).split())
    except Exception as ex:  # the type alone: nothing of the task's reaches the log
        log.warning("announcement wording failed: %s", type(ex).__name__)
        return plain(e)
    if not worded:
        return plain(e)
    if len(worded) > SAID_MOST:
        worded = worded[:SAID_MOST].rsplit(" ", 1)[0] + "…"
    return f"{e.lead} {worded}" if e.lead else worded


def context(task: dict) -> str:
    """For the voice brain beside an announcement, unsaid: which task it was, and each edit it still has pending, by
    number, for task_edits. Its edits aren't listed while it waits for an answer, as they wait for the end too."""
    said = f"[That was task {task['id']}, \"{task['title']}\"."
    if task["status"] != "waiting" and (waiting := data_edits.pending(task)):
        said += " Its suggested edits, pending: " + "; ".join(
            f"{e['n']}. {data_edits.describe(e)}" + (f" (needs a yes: {e['needs_yes']})" if e.get("needs_yes") else "")
            for e in waiting) + "."
    return said + "]"
