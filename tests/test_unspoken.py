"""Tests for the last check before speech: private notes and internal markers never reach the user's ears."""

import asyncio
import random

import pytest

from jarvis.voice.unspoken import Scrubber, scrub, scrubbed


@pytest.mark.parametrize("said, heard", [
    ('Your Chat fix is done. [That was task 230, "Chat fix". Its suggested edits, pending: 1. add a to-do.] Anything '
     'else?', "Your Chat fix is done. Anything else?"),
    ("The <private>update was task 5, on its branch</private> build passed.", "The build passed."),
    ("Done. [Note: nested [inner] thing] Next.", "Done. Next."),
    ("All set. [You announced since the last turn: something", "All set. "),
    ("<announced>It's merged.</announced>", "It's merged."),
])
def test_bracketed_and_private_notes_are_never_said(said, heard):
    assert scrub(said) == heard


@pytest.mark.parametrize("said, heard", [
    ("Task 5, the Chat fix, wants to run a command.", "The task, the Chat fix, wants to run a command."),
    ("Okay, task 117 carries on.", "Okay, the task carries on."),
    ("Is the task #12 done?", "Is the task done?"),
    ("I merged jarvis/237-keep-notes-unspoken into master.", "I merged the branch into master."),
    ("It's on branch jarvis/237-keep-notes-unspoken now.", "It's on the branch now."),
    ("In /Users/sam/projects/app/voice/turn.py, the Judge decides.", "In turn.py, the Judge decides."),
    ("See app/voice/turn.py and voice/clauses.py.", "See turn.py and clauses.py."),
    ("It's in ~/Desktop/projects/app/.", "It's in app."),
    ("I called task_start for it.", "I called task start for it."),
    ("See [the full doc](https://example.com/doc) for more.", "See the full doc for more."),
])
def test_internal_details_are_said_in_plain_words(said, heard):
    assert scrub(said) == heard


@pytest.mark.parametrize("said", [
    "It runs 24/7, and/or on 13/10/2026 at example.com/docs/page or https://example.com/a/b.",
    "It costs about 3.5 dollars, e.g. lunch. Half past three!",
    "If x < 5 and y > 3, the task is done.",
    "Two tasks finished, and a third one is on its way.",
    "Your 3 tasks are done: the Chat fix, the merge and the review.",
    "The task 2 weeks ago was bigger than this one, by a task 3 times over.",
])
def test_normal_speech_is_left_as_it_is(said):
    assert scrub(said) == said


LEAKY = [
    'Your Chat fix is done. [That was task 230, "Chat fix". Its suggested edits, pending: 1. add a to-do.] Next?',
    "It is on branch jarvis/237-keep-notes-unspoken now, and the task 12 is done.",
    "In /Users/sam/projects/app/voice/turn.py, the Judge decides; see [the doc](https://example.com/doc) too.",
    "The <private>task 5, branch x</private> build passed, at example.com/docs/page on 13/10/2026.",
    "Done. [Note: nested [inner] thing] Next, I called task_start.",
]


@pytest.mark.parametrize("said", LEAKY)
def test_a_stream_is_scrubbed_as_the_whole_text_would_be_however_it_is_cut(said):
    rng = random.Random(said)
    for _ in range(300):
        scrubber, out, at = Scrubber(), [], 0
        while at < len(said):
            step = rng.randint(1, 7)
            out.append(scrubber.feed(said[at:at + step]))
            at += step
        out.append(scrubber.end())
        assert " ".join("".join(out).split()) == " ".join(scrub(said).split())


def test_a_stream_holds_back_only_what_could_still_change():
    scrubber = Scrubber()
    assert scrubber.feed("Checking your calendar. ") == "Checking your calendar. "  # nothing waits on a finished word
    assert scrubber.feed("Okay, the") == "Okay, "  # "the task 5" may follow
    assert scrubber.feed(" task") == ""
    assert scrubber.feed(" 5 is done. [That was") == "the task is done. "
    assert scrubber.feed(' task 5.] Anything') == " "
    assert scrubber.end() == "Anything"


def test_a_flush_marker_passes_through_after_what_was_held():
    flush = object()

    async def chunks():
        yield "Half past"
        yield flush
        yield " three [task 5]."

    async def main():
        return [chunk async for chunk in scrubbed(chunks())]

    assert asyncio.run(main()) == ["Half ", "past", flush, " three ", "."]


def test_what_jarvis_shows_and_says_goes_through_the_check(monkeypatch):
    from livekit.agents import Agent

    from jarvis.voice.agent import JarvisAgent

    async def main():
        async def text():
            for chunk in ["Done. [That was ", "task 5.] It's ", "on branch jarvis/5-fix."]:
                yield chunk

        node = JarvisAgent(voice=None).transcription_node(text(), None)
        return "".join([chunk async for chunk in node])

    assert asyncio.run(main()) == "Done.  It's on the branch."
    seen = []
    monkeypatch.setattr(Agent.default, "tts_node", lambda agent, text, settings: seen.append(text) or text)
    JarvisAgent(voice=None).tts_node("text", None)
    assert seen and seen[0].__name__ == "scrubbed"
