"""Tests for jarvis.tasks.announce: model-worded and fixed task-end wording, kept facts, what the brain hears."""

import asyncio

import pytest
from test_data_edits import FACT, TODO, data, run_task  # noqa: F401  (fixture)
from test_in_place import env  # noqa: F401  (fixture)

from jarvis.brain import persona
from jarvis.llm import usage
from jarvis.tasks import announce
from jarvis.tasks.announce import DONE, FAILED, QUESTION, STOPPED, Ending
from jarvis.tasks.store import edits

TICK = ("tick off the to-do matching 'MCP connection tools'", "JARVIS's check found task 125's work finishes it.")


class Model:
    """Fake wording call: records each prompt; returns its reply or raises the given exception."""

    def __init__(self, reply="The to-do linking work is finished. It'd also like to tick off your connection tools "
                             "to-do."):
        self.reply, self.asked = reply, []

    async def __call__(self, text: str) -> str:
        self.asked.append(text)
        if isinstance(self.reply, BaseException):
            raise self.reply
        return self.reply


@pytest.fixture
def model(monkeypatch):
    fake = Model()
    monkeypatch.setattr(announce, "ask_model", fake)
    monkeypatch.setattr(announce, "ANNOUNCE_ENABLED", True)
    monkeypatch.setattr(usage, "out", lambda now=None: None)
    return fake


def said(ending: Ending) -> str:
    return asyncio.run(announce.spoken(ending))


# ── Fixed words ─────────────────────────────────────────────────


def test_the_fixed_words_say_each_fact_once_without_numbers_or_internals():
    ending = Ending("tick off to-dos when tasks finish", DONE, "Background tasks can now be linked to a to-do.\nMore.",
                    left="the last socket test hasn't been run", needs_user=True,
                    done=['ticked off your to-do "MCP connection tools"'], suggested=[TICK])
    assert announce.plain(ending) == (
        "Your task, tick off to-dos when tasks finish, is done. Background tasks can now be linked to a to-do. It still "
        "needs you: the last socket test hasn't been run. I've ticked off your to-do \"MCP connection tools\". It'd "
        "also like to tick off the to-do matching 'MCP connection tools'.")
    for robotic in ("Task 125", "data files", "suggested edits, pending", "JARVIS's check", "needs you for the rest"):
        assert robotic not in announce.plain(ending)


@pytest.mark.parametrize("ending, words", [
    (Ending("the build", FAILED, "it hit the Claude usage limit"), "Your task, the build, failed: it hit the Claude usage limit."),
    (Ending("the build", STOPPED), "Your task, the build, stopped."),
    (Ending("the build", QUESTION, "Which phone?"), "Your task, the build, has a question: Which phone?"),
    (Ending("the build", DONE, "Found X.", left="Y isn't answered."),
     "Your task, the build, is done. Found X. It may not be finished: Y isn't answered."),
    (Ending("the build", DONE, "Found X.", suggested=[TICK] * 3),
     "Your task, the build, is done. Found X. It has 3 suggestions for your lists and my memory."),
    (Ending("the build", STOPPED, undelivered=True),
     "Your task, the build, stopped. Your message to it wasn't delivered; it goes with your next reply."),
    (Ending("Flights", DONE, lead="Flights: fares dropped to 300 pounds.", suggested=[("add a to-do: Book it", "")]),
     "Flights: fares dropped to 300 pounds. It'd also like to add a to-do: Book it."),
])
def test_each_kind_of_end_in_fixed_words(ending, words):
    assert announce.plain(ending) == words


# ── Worded by the model ─────────────────────────────────────────


def test_the_model_words_the_end_from_its_facts(model):
    ending = Ending("tick off to-dos when tasks finish", DONE, "Background tasks can now be linked to a to-do.",
                    left="the summary says checks were re-run through host_run, but the log shows none",
                    suggested=[TICK])
    assert said(ending) == "The to-do linking work is finished. It'd also like to tick off your connection tools to-do."
    (facts,) = model.asked
    assert "<task>tick off to-dos when tasks finish</task>" in facts and "<ended>done</ended>" in facts
    assert "<report>Background tasks can now be linked to a to-do.</report>" in facts
    assert "<check>May be unfinished: the summary says checks were re-run" in facts
    assert ("<suggestions>\n- tick off the to-do matching 'MCP connection tools' (why: JARVIS's check found task "
            "125's work finishes it.)\n</suggestions>") in facts


def test_the_prompt_asks_for_plain_speech_and_leaves_the_wording_to_the_model():
    for rule in ("never by a number", "Say each thing once", "Leave out task numbers, file names", "data from the task"):
        assert rule in announce.system()


def test_words_said_first_are_kept_and_the_rest_added(model):
    model.reply = "  It'd also like to add\n a to-do to book it. "
    assert said(Ending("Flights", DONE, lead="Flights: fares dropped.", suggested=[("add a to-do: Book it", "")])) == \
        "Flights: fares dropped. It'd also like to add a to-do to book it."
    assert "<said_first>Flights: fares dropped.</said_first>" in model.asked[0] and "<report>" not in model.asked[0]


def test_a_question_keeps_its_place(model):
    said(Ending("the build", QUESTION, "Which phone?"))
    assert "<question>Which phone?</question>" in model.asked[0] and "<report>" not in model.asked[0]


def test_a_long_reply_is_cut_at_a_word(model):
    model.reply = "word " * 500
    assert len(words := said(Ending("t", DONE, "Did it."))) <= announce.SAID_MOST + 1 and words.endswith("word…")


def test_nothing_to_word_needs_no_call(model):
    assert said(Ending("the build", STOPPED)) == "Your task, the build, stopped."
    assert said(Ending("Flights", DONE, lead="Flights: fares dropped.")) == "Flights: fares dropped."
    assert model.asked == []


@pytest.mark.parametrize("why", ["off", "fails", "empty", "limit"])
def test_without_the_model_the_same_facts_go_in_fixed_words(model, monkeypatch, why):
    if why == "off":
        monkeypatch.setattr(announce, "ANNOUNCE_ENABLED", False)
    elif why == "fails":
        model.reply = RuntimeError("the model call failed")
    elif why == "empty":
        model.reply = "  "
    else:
        monkeypatch.setattr(usage, "out", lambda now=None: ("seven_day", {}))
    ending = Ending("the build", DONE, "Found X.", left="Y isn't answered.")
    assert said(ending) == announce.plain(ending)
    assert len(model.asked) == (1 if why in ("fails", "empty") else 0)


def test_a_slow_model_gives_way_to_fixed_words(model, monkeypatch):
    async def slow(text):
        await asyncio.sleep(5)

    monkeypatch.setattr(announce, "ask_model", slow)
    monkeypatch.setattr(announce, "ANNOUNCE_SECONDS", 0.01)
    assert said(Ending("the build", DONE, "Found X.")) == "Your task, the build, is done. Found X."


# ── In the runner and the voice brain ───────────────────────────


def test_a_finished_task_is_announced_in_the_models_words_and_its_edits_stay_for_the_tools(data, monkeypatch, model):
    model.reply = "The thing is done. It'd also like to add a to-do to try the push self-test, and save a note."
    r, task = run_task(monkeypatch, "code_change", "video-agent", TODO, FACT)
    assert task["spoken"] == model.reply
    assert "<report>Did it.\nDetails.</report>" in model.asked[0]
    assert "- add a to-do: Try the new push self-test (why: It needs a real phone.)" in model.asked[0]
    assert [(e["n"], e["status"]) for e in edits(task)] == [(1, "pending"), (2, "pending")]  # nothing applied unasked
    context = announce.context(task)
    assert context.startswith('The update on "a thing" was task 1. Its suggested edits, pending: 1. add a to-do: Try the new')
    assert "; 2. save to memory" in context and "Task 1" not in task["spoken"]


def test_the_brain_hears_what_was_said_and_the_rest_but_only_the_words_are_logged(monkeypatch):
    logged = []
    monkeypatch.setattr(persona.memory, "log_announcement", logged.append)
    notes = persona.Notes()
    notes.note("The build is done.", 'The update on "the build" was task 5. Its suggested edits, pending: 1. add a to-do: X.')
    assert logged == ["The build is done."]
    turn = notes.turn("what were they?")
    assert "[You announced since the last turn: The build is done.]" in turn
    assert '<private>The update on "the build" was task 5. Its suggested edits, pending: 1. add a to-do: X.</private>' in turn
    assert "<private>" not in notes.turn("and then?")  # each note goes once


def test_a_note_for_the_brain_alone_is_neither_announced_nor_logged(monkeypatch):
    logged = []
    monkeypatch.setattr(persona.memory, "log_announcement", logged.append)
    notes = persona.Notes()
    notes.note("", "Sam approved task 5's request 3 to run a command that pushes.")
    turn = notes.turn("thanks")
    assert logged == [] and "You announced" not in turn
    assert "<private>Sam approved task 5's request 3 to run a command that pushes.</private>" in turn


def test_a_greeting_says_an_update_in_whole_sentences_and_a_question_whole():
    long = "The site is redesigned. " + "It has a new look. " * 20 + "Have a look?"
    done = {"status": DONE, "spoken": "The restart worked,\neverything's back up"}
    assert announce.brief(done) == ("The restart worked, everything's back up.", True)
    cut, whole = announce.brief({"status": DONE, "spoken": long})
    assert cut.startswith("The site is redesigned. It has a new look.") and cut.endswith("look.") and not whole
    assert len(cut) <= announce.BRIEF_MOST
    assert announce.brief({"status": DONE, "spoken": "x" * 300 + ". Then more."}) == ("x" * 300 + ".", False)
    assert announce.brief({"status": "waiting", "spoken": long}) == (long.strip(), True)


def test_the_update_a_greeting_leads_with_is_the_weightiest_with_words():
    silent = {"status": "waiting", "spoken": " ", "updated": 9}
    stopped = {"status": STOPPED, "spoken": "The scan stopped.", "updated": 3}
    finished = {"status": DONE, "spoken": "Done.", "updated": 1}
    assert announce.first([silent, stopped, finished]) is finished
    assert announce.first([silent, stopped]) is stopped
    assert announce.first([silent]) is None and announce.first([]) is None
