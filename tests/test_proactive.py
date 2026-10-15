"""Tests for proactive speech (jarvis.voice.floor): judged timing, urgent cut-ins, phone fallback, follow-ups."""

import asyncio
from datetime import datetime, timedelta

import pytest

from jarvis.brain import quick
from jarvis.core import profile
from jarvis.tasks import data_edits, learning, schedules
from jarvis.voice import floor as floor_module
from jarvis.voice.floor import CUT_IN, HOLD_MINUTES, Announcement, Announcements, Floor, Moment


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class Session:
    agent_state = user_state = "listening"

    def __init__(self) -> None:
        self.said: list[str] = []

    async def say(self, text: str, add_to_chat_ctx: bool = True) -> None:
        self.said.append(text)


class Brain:
    def __init__(self) -> None:
        self.notes: list[str] = []

    def note(self, text: str, unsaid: str = "") -> None:
        self.notes.append(f"{text} {unsaid}".strip())


class Choices:
    """Fake Moment: answers each call with the next list of choices."""

    def __init__(self, *answers) -> None:
        self.answers = list(answers)
        self.asked: list[list[str]] = []

    async def decide(self, items, floor, now):
        self.asked.append([a.text for a in items])
        return self.answers.pop(0)


@pytest.fixture
def setup():
    clock, wall = Clock(), Clock()
    wall.now = 2_000_000.0
    pushed: list[str] = []

    async def push(text: str) -> None:
        pushed.append(text)

    def make(*answers):
        floor = Floor(clock)
        moment = Choices(*answers)
        return floor, moment, Announcements(floor, clock, moment, push, wall)

    return make, clock, wall, pushed


def turn(announcements: Announcements, session: Session) -> Announcement | None:
    return asyncio.run(announcements._take_turn(session, Brain(), None))


def test_an_item_held_for_a_better_moment_is_judged_again_after_the_next_exchange(setup):
    make, clock, _, _ = setup
    floor, moment, announcements = make(["pause"], ["now"])
    session = Session()
    announcements.add(Announcement("The Chat fix is done."))
    assert turn(announcements, session) is None and session.said == []
    assert turn(announcements, session) is None and len(moment.asked) == 1  # nothing new, so not asked again
    floor.turn()  # they spoke and JARVIS replied
    assert turn(announcements, session).text == "The Chat fix is done."
    assert session.said == ["The Chat fix is done."] and not announcements.queue


def test_only_an_urgent_item_cuts_in_and_it_says_so(setup):
    make, _, wall, _ = setup
    floor, moment, announcements = make(["cut_in", "cut_in"])
    session = Session()
    announcements.add(Announcement("A newsletter came."))
    announcements.add(Announcement("Standup starts in 3 minutes.", urgency="soon", at=wall.now + 180))
    said = turn(announcements, session)
    assert said.text == "Standup starts in 3 minutes."  # its time is close: urgent
    assert session.said == [CUT_IN + "Standup starts in 3 minutes."]
    assert announcements.queue[0].judged is not None  # the newsletter waits


def test_what_the_conversation_doesnt_take_goes_to_the_phone_unless_it_is_there_already(setup):
    make, _, _, pushed = setup
    floor, moment, announcements = make(["phone", "phone"])
    session = Session()
    announcements.add(Announcement("The lookup on flights came back."))
    announcements.add(Announcement("Heads up. Mail from Alice.", pushed=True))

    async def main():
        await announcements._take_turn(session, Brain(), None)
        await asyncio.sleep(0)

    asyncio.run(main())
    assert pushed == ["The lookup on flights came back."] and session.said == [] and not announcements.queue


def test_held_too_long_goes_to_the_phone_and_past_its_time_is_dropped(setup):
    make, clock, wall, pushed = setup
    floor, moment, announcements = make(["pause", "pause"])
    session = Session()
    announcements.add(Announcement("The render task is done."))
    announcements.add(Announcement("Review starts in 20 minutes.", at=wall.now + 1200))
    turn(announcements, session)
    clock.now += HOLD_MINUTES * 60
    wall.now += 1200 + floor_module.STALE_MINUTES * 60 + 1

    async def main():
        await announcements._take_turn(session, Brain(), None)
        await asyncio.sleep(0)

    asyncio.run(main())
    assert pushed == ["The render task is done."] and not announcements.queue and session.said == []


def test_the_sessions_end_sends_whats_left_to_the_phone(setup):
    make, _, _, pushed = setup
    _, _, announcements = make()
    announcements.add(Announcement("The lookup on hotels came back."))
    asyncio.run(announcements.leave())
    assert pushed == ["The lookup on hotels came back."] and not announcements.queue


def test_an_item_said_long_after_it_came_says_so(setup):
    make, clock, _, _ = setup
    floor, _, announcements = make(["pause"], ["now"])
    session = Session()
    announcements.add(Announcement("The build passed."))
    turn(announcements, session)
    clock.now += 4 * 60
    floor.turn()
    turn(announcements, session)
    assert session.said == ["From 4 minutes ago: The build passed."]


def test_with_no_answer_from_the_model_items_go_at_once_one_at_a_time(setup):
    make, _, _, _ = setup
    _, _, announcements = make(None)
    session = Session()
    announcements.add(Announcement("One."))
    announcements.add(Announcement("Two."))
    turn(announcements, session)
    turn(announcements, session)
    assert session.said == ["One.", "Two."]


def test_the_model_sees_the_conversation_and_each_items_urgency_and_time():
    client_asked: list[str] = []

    class Client:
        async def query(self, text):
            client_asked.append(text)

        async def receive_response(self):
            from claude_agent_sdk import ResultMessage
            yield ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False, num_turns=1,
                                session_id="s", result='{"decisions": ["pause", "now"]}')

    async def connect():
        return Client()

    clock = Clock()
    floor = Floor(clock)
    floor.heard("so the plan for the migration is <b>")
    floor.said("Go on.")
    clock.now += 12
    items = [Announcement("The Chat fix is done."), Announcement("Standup starts soon.", at=10_000.0 + 600)]
    for a in items:
        a.came = clock.now
    moment = Moment(quick.Quick("test", "prompt", connect=connect))
    assert asyncio.run(moment.decide(items, floor, 10_000.0)) == ["pause", "now"]
    [prompt] = client_asked
    assert "so the plan for the migration is b" in prompt and "JARVIS: Go on." in prompt
    assert "Last spoke: 12 seconds ago." in prompt
    assert "1. [soon] The Chat fix is done." in prompt and "2. [soon, about 10 minutes from now]" in prompt


def test_a_reply_that_doesnt_fit_the_items_counts_as_none():
    class Fake:
        async def ask(self, text, timeout):
            return '{"decisions": ["now"]}'

    moment = Moment(Fake())
    assert asyncio.run(moment.decide([Announcement("a"), Announcement("b")], Floor(Clock()), 0.0)) is None


# ── Follow-ups from the learning review ─────────────────────────


def test_a_follow_up_the_review_finds_becomes_a_reminder_on_the_users_yes():
    later = (datetime.now(profile.tz()) + timedelta(hours=2)).replace(second=0, microsecond=0).strftime("%Y-%m-%dT%H:%M")
    [edit] = learning.to_edits([{"action": "follow_up", "text": "Check the build", "when": later,
                                 "reason": "Said they'd check it after lunch."}], [], "2026-10-10")
    checked = data_edits.check(edit, "review")
    assert data_edits.needs_yes(checked, "review")
    assert data_edits.describe(checked).endswith(": Check the build")
    said = asyncio.run(data_edits.apply(checked))
    assert said.startswith("I'll remind") and any(j["text"] == "Check the build" for j in schedules.items())
    with pytest.raises(ValueError):
        data_edits.check(edit, "")  # only the review may suggest them
    with pytest.raises(ValueError):
        data_edits.check(edit | {"when": "2020-01-01T09:00"}, "review")  # already past
