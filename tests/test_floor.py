"""Tests for jarvis.voice.floor: announcements wait while anyone talks or a question is open, then go once, in order."""

import asyncio

import pytest
from livekit.agents import llm

from jarvis.safety.approvals import VoiceApprovals
from jarvis.voice import agent, floor as floor_module
from jarvis.voice.floor import (
    QUESTION_MINUTES,
    REPEAT_MINUTES,
    WORDS_SECONDS,
    Announcement,
    Announcements,
    Floor,
    asks_question,
)
from jarvis.voice.session import VoiceSession


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class Session:
    """Fake LiveKit AgentSession: its states and what it was asked to say."""

    def __init__(self) -> None:
        self.agent_state = "listening"
        self.user_state = "listening"
        self.said: list[str] = []
        self.fail = False

    async def say(self, text, add_to_chat_ctx: bool = True) -> None:
        if self.fail:
            raise RuntimeError("session closed")
        self.said.append(text if isinstance(text, str) else "".join([c async for c in text]))


class Brain:
    def __init__(self, *reply: str) -> None:
        self.reply = reply
        self.notes: list[str] = []

    def note(self, text: str, unsaid: str = "") -> None:
        self.notes.append(f"{text} {unsaid}".strip())

    async def respond(self, text: str):
        for chunk in self.reply:
            yield chunk

    async def follow_up(self, prompt: str, words: str = ""):
        self.notes.append(f"follow-up: {prompt} {words}".strip())
        for chunk in self.reply:
            yield chunk

    def question_open(self) -> bool:
        return False


class STT:
    hearing = 0


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    monkeypatch.setattr(floor_module, "POLL_SECONDS", 0.001)


async def settle() -> None:
    for _ in range(30):
        await asyncio.sleep(0.002)


def running(coro_fn):
    """Run `coro_fn(clock, floor, announcements, session, brain, seen, stt)` alongside the announcer.

    `seen` collects the task ids marked as heard.
    """

    async def main():
        clock, session, brain, stt, seen = Clock(), Session(), Brain(), STT(), []
        floor = Floor(clock)
        announcements = Announcements(floor, clock)

        async def mark_seen(ids):
            seen.append(ids)

        task = asyncio.create_task(announcements.run(session, brain, stt, None, mark_seen))
        try:
            await coro_fn(clock, floor, announcements, session, brain, seen, stt)
        finally:
            task.cancel()

    asyncio.run(main())


# ── Questions ───────────────────────────────────────────────────


def test_a_reply_asks_a_question_when_it_ends_with_one():
    assert asks_question("Shall I do both?")
    assert asks_question("Shall I go ahead, or would you rather cut deeper?  ")
    assert asks_question('Did you say "stop?"')
    assert not asks_question("Okay, task 117 carries on.")
    assert not asks_question("Shall I? I'll do it anyway.")
    assert not asks_question("")


# ── What holds an announcement ──────────────────────────────────


def test_announcements_wait_while_jarvis_thinks_or_speaks_or_the_user_speaks():
    floor = Floor(Clock())
    for state in ("initializing", "thinking", "speaking"):
        assert floor.held(state, "listening")
    assert floor.held("listening", "speaking")
    assert floor.held("listening", "listening", transcribing=1)
    assert floor.held("listening", "listening") is None
    assert floor.held("idle", "away") is None


def test_the_users_words_hold_announcements_until_the_reply_starts():
    clock = Clock()
    floor = Floor(clock)
    floor.heard("   ")  # blank: nothing heard
    assert floor.held("listening", "listening") is None
    floor.heard("Sure, go ahead.")  # their turn hasn't ended yet
    assert floor.held("listening", "listening")
    floor.turn()
    assert floor.held("listening", "listening") is None


def test_words_that_never_reach_the_brain_stop_holding_announcements():
    clock = Clock()
    floor = Floor(clock)
    floor.heard("um")
    clock.now += WORDS_SECONDS - 1
    assert floor.held("listening", "listening")
    clock.now += 2
    assert floor.held("listening", "listening") is None


def test_an_open_question_holds_announcements_until_the_user_answers():
    clock = Clock()
    floor = Floor(clock)
    floor.said("Shall I do both?")
    clock.now += 60
    assert floor.held("listening", "listening") == "JARVIS asked a question"
    floor.turn()  # the user answered
    assert floor.held("listening", "listening") is None


def test_a_reply_that_asks_again_keeps_the_question_open():
    floor = Floor(Clock())
    floor.said("Shall I do both?")
    floor.turn()
    floor.said("Sorry, I only caught 'cut'. Should I stop here, or go ahead?")
    assert floor.held("listening", "listening")
    floor.turn()
    floor.said("Going ahead with both.")
    assert floor.held("listening", "listening") is None


def test_an_unanswered_question_lapses_after_a_couple_of_minutes_of_silence():
    clock = Clock()
    floor = Floor(clock)
    floor.said("Shall I do both?")
    clock.now += QUESTION_MINUTES * 60 - 1
    assert floor.held("listening", "listening")
    clock.now += 2
    assert floor.held("listening", "listening") is None
    assert not floor.question  # it doesn't come back


def test_talking_that_isnt_an_answer_keeps_the_question_open_longer():
    clock = Clock()
    floor = Floor(clock)
    floor.said("Shall I do both?")
    clock.now += QUESTION_MINUTES * 60 - 10
    assert floor.held("listening", "speaking")  # speech the gate then drops as noise
    clock.now += 30
    assert floor.held("listening", "listening") == "JARVIS asked a question"
    clock.now += QUESTION_MINUTES * 60
    assert floor.held("listening", "listening") is None


# ── Saying them ─────────────────────────────────────────────────


def test_an_announcement_goes_straight_away_when_nothing_holds_it():
    async def check(clock, floor, announcements, session, brain, seen, stt):
        assert announcements.add(Announcement("The build is done.", "[That was task 5 ... edits]", [5]))
        await settle()
        assert session.said == ["The build is done."]
        assert brain.notes == ["The build is done. [That was task 5 ... edits]"]  # said, then the note meant only for the brain
        assert seen == [[5]]

    running(check)


def test_an_email_announcement_waits_for_the_answer_to_a_question_and_the_reply_to_it():
    """Otherwise an alert right after a question gets mixed up with its answer."""

    async def check(clock, floor, announcements, session, brain, seen, stt):
        floor.said("Shall I do both?")
        announcements.add(Announcement("Heads up. New email from support@linode.com."))
        await settle()
        assert session.said == []  # the question is open
        session.user_state = "speaking"
        await settle()
        session.user_state = "listening"
        stt.hearing = 1  # Whisper is transcribing the answer
        await settle()
        stt.hearing = 0
        floor.heard("Yes, do both.")
        await settle()
        assert session.said == []  # the turn hasn't ended, so the brain doesn't have it yet
        floor.turn()
        session.agent_state = "thinking"
        await settle()
        session.agent_state = "speaking"
        floor.said("Doing both now.")
        await settle()
        assert session.said == []
        session.agent_state = "listening"
        await settle()
        assert session.said == ["Heads up. New email from support@linode.com."]

    running(check)


def test_announcements_are_said_once_each_in_order_and_one_that_asks_holds_the_rest():
    async def check(clock, floor, announcements, session, brain, seen, stt):
        session.agent_state = "speaking"
        announcements.add(Announcement("Task 112 is done."))
        announcements.add(Announcement("Task 111 has a question: which folder?"))
        announcements.add(Announcement("Job 3 is done."))
        await settle()
        assert session.said == []
        session.agent_state = "listening"
        await settle()
        assert session.said == ["Task 112 is done.", "Task 111 has a question: which folder?"]
        floor.turn()
        await settle()
        assert session.said == ["Task 112 is done.", "Task 111 has a question: which folder?", "Job 3 is done."]

    running(check)


def test_the_same_announcement_isnt_said_twice():
    async def check(clock, floor, announcements, session, brain, seen, stt):
        session.agent_state = "speaking"
        assert announcements.add(Announcement("Task 115 is done.", seen=[115]))
        assert not announcements.add(Announcement("Task 115 is done.", seen=[115, 116]))  # still waiting to be said
        session.agent_state = "listening"
        await settle()
        assert not announcements.add(Announcement("Task 115 is done.", seen=[115]))  # just said
        await settle()
        assert session.said == ["Task 115 is done."]
        assert seen == [[115, 116]]
        clock.now += REPEAT_MINUTES * 60 + 1
        assert announcements.add(Announcement("Task 115 is done."))  # old enough to be news again
        await settle()
        assert session.said == ["Task 115 is done.", "Task 115 is done."]

    running(check)


def test_an_announcement_that_fails_isnt_marked_heard_and_asks_nothing():
    async def check(clock, floor, announcements, session, brain, seen, stt):
        session.fail = True
        announcements.add(Announcement("Task 9 has a question: which one?", seen=[9]))
        await settle()
        assert seen == [] and not floor.question
        session.fail = False
        assert announcements.add(Announcement("Task 9 has a question: which one?", seen=[9]))  # a failed say doesn't count as said
        await settle()
        assert session.said == ["Task 9 has a question: which one?"] and seen == [[9]]

    running(check)


def test_a_request_left_unanswered_is_closed_before_an_announcement():
    """So a yes after the announcement isn't taken as answering the earlier request."""

    async def main():
        clock, session, brain = Clock(), Session(), Brain()
        floor, asks = Floor(clock), VoiceApprovals()
        announcements = Announcements(floor, clock)
        asks.add({"id": 13, "task": 117, "spoken": "Task 117 wants to delete files. Okay to go ahead?"})
        floor.said(asks.ask(asks.due()))
        task = asyncio.create_task(announcements.run(session, brain, None, asks))
        announcements.add(Announcement("Heads up. New email."))
        await settle()
        assert session.said == [] and asks.open
        clock.now += QUESTION_MINUTES * 60 + 1
        await settle()
        task.cancel()
        assert session.said == ["Heads up. New email."]
        assert asks.open is None and asks.due() is None  # asked once this call: the app's buttons have it now
        assert asks.hear("yes") is None

    asyncio.run(main())


# ── In the voice agent ──────────────────────────────────────────


def chat(text: str) -> llm.ChatContext:
    ctx = llm.ChatContext()
    ctx.add_message(role="user", content=text)
    return ctx


async def reply(jarvis: agent.JarvisAgent, text: str) -> str:
    return "".join([chunk async for chunk in jarvis.llm_node(chat(text), [], None)])


def test_the_agent_opens_a_question_with_a_reply_that_asks_one_and_closes_it_on_the_next_turn():
    async def main():
        floor = Floor(Clock())
        brain = Brain("I'd trim four parts. ", "Shall I go ahead?")
        jarvis = agent.JarvisAgent(VoiceSession(brain, floor=floor))
        floor.heard("Just check the kiosk doc.")
        assert await reply(jarvis, "Just check the kiosk doc.") == "I'd trim four parts. Shall I go ahead?"
        assert floor.question and floor.words_at is None
        brain.reply = ("Trimming it now.",)
        await reply(jarvis, "Sure, go ahead.")
        assert not floor.question

    asyncio.run(main())


def test_an_approval_waits_for_the_answer_to_the_brains_question():
    """Otherwise "Sure, go ahead" answers the request instead of the brain's question."""

    async def main():
        session, floor = Session(), Floor(Clock())
        call = VoiceSession(Brain(), floor=floor)
        floor.said("Shall I go ahead, or would you rather cut deeper?")
        call.asks.add({"id": 13, "task": 117, "spoken": "Task 117 wants to delete files. Okay to go ahead?"})
        task = asyncio.create_task(call.ask_approvals(session))
        await settle()
        assert session.said == []
        floor.turn()
        floor.said("Trimming it now.")
        await settle()
        task.cancel()
        assert session.said == ["Task 117 wants to delete files. Okay to go ahead?"]
        assert floor.question  # the brain's question holds announcements until the yes or no

    asyncio.run(main())


def test_a_brain_turn_of_its_own_is_spoken_and_never_merged_with_another():
    async def main():
        floor, session, brain = Floor(), Session(), Brain("Merged", " into master.")
        queue = Announcements(floor)
        for _ in range(2):
            assert queue.add(Announcement("The task merge has finished.",
                                          speak=lambda b: b.follow_up("[merged]", "merge it")))
        while queue.queue:
            await queue._take_turn(session, brain, None)
        return session.said, brain, floor

    said, brain, floor = asyncio.run(main())
    assert said == ["Merged into master.", "Merged into master."]
    assert brain.notes == ["follow-up: [merged] merge it"] * 2  # nothing else noted: the brain spoke it itself
    assert floor.recent[-1] == ("JARVIS", "Merged into master.")


def test_a_brain_turn_with_nothing_to_say_stays_quiet():
    async def main():
        session = Session()
        queue = Announcements(Floor())
        queue.add(Announcement("The gmail search has finished.", speak=lambda b: b.follow_up("[found]")))
        return await queue._take_turn(session, Brain(), None), session.said

    assert asyncio.run(main()) == (None, [])
