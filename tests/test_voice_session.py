"""Tests for jarvis.voice.session: turn routing (rule offer, request, brain), prompt requests, what a call holds."""

import asyncio

import pytest

from jarvis.state import conversation
from jarvis.tasks import client
from jarvis.voice import floor as floor_module, session as voice_session
from jarvis.voice.floor import Floor
from jarvis.voice.session import VoiceSession

REQUEST = {"id": 13, "task": 117, "summary": "a command that deletes files",
           "spoken": "Task 117 wants to delete files. Okay to go ahead?"}
OTHER = {"id": 14, "task": 118, "summary": "a command that stops a process",
         "spoken": "Task 118 wants to stop a process. Okay to go ahead?"}


class Brain:
    """Fake CLI brain (jarvis.brain.VoiceBrain)."""

    def __init__(self, *reply: str) -> None:
        self.reply = reply or ("Sure.",)
        self.asked: list[str] = []  # the turns it answered
        self.notes: list[str] = []
        self.framed: list[list[str]] = []
        self.next_model: str | None = None
        self.models: list[str | None] = []  # the model each turn was answered on
        self.asking = False  # its shell's own question about a command
        self.ended = False
        self.background = None

    async def start(self) -> None:
        pass

    async def end(self) -> None:
        self.ended = True

    async def respond(self, text: str):
        self.asked.append(text)
        self.models.append(self.next_model)
        for chunk in self.reply:
            yield chunk

    def note(self, text: str, unsaid: str = "") -> None:
        self.notes.append(text)

    def on_background(self, finished) -> None:
        self.background = finished

    def frame(self, lines: list[str]) -> None:
        self.framed.append(lines)

    def question_open(self) -> bool:
        return self.asking


class Runner:
    """Fake task runner client: records what it was sent; returns canned events and lists."""

    def __init__(self, *events: dict, pending: list[dict] = (), unseen: list[dict] = ()) -> None:
        self._events = events
        self.pending, self.unseen = list(pending), list(unseen)
        self.answers: list[tuple] = []
        self.kept: list[tuple] = []
        self.ended = False

    async def answer(self, approval_id, decision, via, remember="once", whole_task=False):
        self.answers.append((approval_id, decision, via, remember, whole_task))
        return {"answered": True, "commands": []}

    async def remember(self, approval_id, breadth, via):
        self.kept.append((approval_id, breadth, via))
        return {"rules": [{"id": 1}]}

    async def events(self):
        for event in self._events:
            yield event
        raise client.RunnerUnavailable("gone")

    async def tasks(self, unseen=False):
        return self.unseen

    async def approvals(self):
        return self.pending

    async def mark_seen(self, ids) -> None:
        pass

    async def session_ended(self) -> None:
        self.ended = True


class Session:
    """Fake LiveKit AgentSession."""

    agent_state = user_state = "listening"

    def __init__(self) -> None:
        self.said: list[str] = []

    async def say(self, text: str, add_to_chat_ctx: bool = True) -> None:
        self.said.append(text)


class Router:
    async def pick(self, text: str) -> str:
        return "quick-model"


@pytest.fixture(autouse=True)
def quiet(monkeypatch):
    """No conversation logging and a fast-polling floor (each test gets its own conversation via conftest)."""
    monkeypatch.setattr(voice_session.memory, "log_exchange", lambda *a, **k: None)
    monkeypatch.setattr(floor_module, "POLL_SECONDS", 0.001)


def call(brain: Brain | None = None, runner: Runner | None = None, **parts) -> VoiceSession:
    return VoiceSession(brain or Brain(), runner=runner or Runner(), **parts)


async def reply(voice: VoiceSession, text: str) -> str:
    return "".join([chunk async for chunk in voice.respond(text)])


async def settle() -> None:
    for _ in range(30):
        await asyncio.sleep(0.002)


# ── Where each turn goes ──────────────────────────────────────


def test_a_turn_with_nothing_asked_goes_to_the_brain_on_the_model_the_router_picks():
    async def main():
        voice = call(Brain("It's half past three."), router=Router())
        return voice, await reply(voice, "What's the time?")

    voice, said = asyncio.run(main())
    assert said == "It's half past three."
    assert voice.brain.asked == ["What's the time?"] and voice.brain.models == ["quick-model"]
    assert voice.runner.answers == voice.runner.kept == []


def test_a_yes_to_a_tasks_request_goes_to_the_runner_not_the_brain():
    async def main():
        voice = call()
        voice.asks.add(REQUEST)
        voice.floor.said(voice.asks.ask(voice.asks.due()))
        return voice, await reply(voice, "Yes, go ahead.")

    voice, said = asyncio.run(main())
    assert said == "Okay, task 117 carries on."
    assert voice.runner.answers == [(13, "approve", "voice", "once", True)]
    assert voice.brain.asked == [] and voice.asks.open is None
    assert not voice.floor.question and voice.floor.recent[-1] == ("JARVIS", said)
    assert voice.brain.notes[-1].endswith("approved task 117's request 13 to run a command that deletes files.")


def test_a_no_to_a_tasks_request_turns_it_down():
    async def main():
        voice = call()
        voice.asks.add(REQUEST)
        voice.asks.ask(voice.asks.due())
        return voice, await reply(voice, "No.")

    voice, said = asyncio.run(main())
    assert said == "Okay, task 117 won't run it."
    assert voice.runner.answers == [(13, "deny", "voice", "once", True)] and voice.brain.asked == []


def test_words_that_dont_answer_the_request_go_to_the_brain_and_leave_it_to_the_app():
    async def main():
        voice = call(Brain("It's sunny."))
        voice.asks.add(REQUEST)
        voice.asks.ask(voice.asks.due())
        return voice, await reply(voice, "What's the weather like?")

    voice, said = asyncio.run(main())
    assert said == "It's sunny." and voice.brain.asked == ["What's the weather like?"]
    assert voice.runner.answers == [] and voice.asks.open is None and voice.asks.due() is None


def test_a_yes_to_a_rule_offered_saves_it_before_anything_else():
    async def main():
        voice = call()
        voice.asks.add(REQUEST)
        voice.asks.ask(voice.asks.due())  # a request is open too: the offer, put after it, comes first
        voice.talk.offer.put([], approval=13, breadth="kind")
        return voice, await reply(voice, "Yes.")

    voice, said = asyncio.run(main())
    assert said.startswith("Saved.") and voice.runner.kept == [(13, "kind", "voice")]
    assert voice.runner.answers == [] and voice.brain.asked == [] and voice.talk.offer.open is None
    assert any("agreed to remembering" in n for n in voice.brain.notes)


def test_a_no_to_a_rule_offered_keeps_nothing():
    async def main():
        voice = call()
        voice.talk.offer.put([], approval=13)
        return voice, await reply(voice, "No thanks.")

    voice, said = asyncio.run(main())
    assert said == "Okay, I won't remember it." and voice.runner.kept == [] and voice.brain.asked == []


def test_a_runner_that_cant_be_reached_is_said_so():
    class Down(Runner):
        async def answer(self, *a, **k):
            raise client.RunnerUnavailable("down")

    async def main():
        voice = call(runner=Down())
        voice.asks.add(REQUEST)
        voice.asks.ask(voice.asks.due())
        return await reply(voice, "Yes.")

    assert asyncio.run(main()) == "Sorry, I couldn't pass that on to the task runner."


# ── Tasks' requests, as they come ─────────────────────────────


def test_a_request_is_put_to_the_user_as_soon_as_it_comes_not_on_a_timer():
    async def main():
        voice, session = call(), Session()
        asking = asyncio.create_task(voice.ask_approvals(session))
        await settle()
        assert session.said == []
        voice.asks.add(REQUEST)
        await settle()  # well under two seconds
        asking.cancel()
        return voice, session

    voice, session = asyncio.run(main())
    assert session.said == [REQUEST["spoken"]] and voice.asks.open["id"] == 13
    assert voice.brain.notes == [REQUEST["spoken"]] and voice.floor.question


def test_a_request_waits_for_the_brains_own_question_and_follows_the_turn_that_answers_it():
    async def main():
        brain = Brain("Done.")
        voice, session = call(brain), Session()
        brain.asking = True
        voice.asks.add(REQUEST)
        asking = asyncio.create_task(voice.ask_approvals(session))
        await settle()
        assert session.said == []
        brain.asking = False  # this turn answers the shell's question
        await reply(voice, "Yes, run it.")
        await settle()
        asking.cancel()
        return session

    assert asyncio.run(main()).said == [REQUEST["spoken"]]


def test_a_rule_offered_is_dropped_when_a_request_is_put():
    """A yes after the request is for the request."""

    async def main():
        voice, session = call(), Session()
        voice.talk.offer.put([], approval=12)
        voice.asks.add(REQUEST)
        asking = asyncio.create_task(voice.ask_approvals(session))
        await settle()
        asking.cancel()
        return voice, await reply(voice, "Yes.")

    voice, said = asyncio.run(main())
    assert said == "Okay, task 117 carries on." and voice.runner.kept == []


def test_the_runners_events_bring_requests_and_take_back_those_answered_elsewhere():
    async def main():
        runner = Runner({"type": "approval", "approval": REQUEST}, {"type": "approval", "approval": OTHER},
                        {"type": "approval_answered", "approval": {"id": 13}},
                        {"type": "alert", "text": "Your 3 o'clock starts soon.", "tasks": None})
        voice, session = call(runner=runner), Session()
        watching = asyncio.create_task(voice.watch_tasks())
        asking = asyncio.create_task(voice.ask_approvals(session))
        await settle()
        watching.cancel()
        asking.cancel()
        return voice, session

    voice, session = asyncio.run(main())
    assert session.said == [OTHER["spoken"]]  # 13 was answered from the app before it came up
    assert [a.text for a in voice.announcements.queue] == ["Your 3 o'clock starts soon."]


def test_the_greeting_mentions_updates_and_ends_with_the_oldest_request():
    async def main():
        voice = call(runner=Runner(pending=[OTHER, REQUEST], unseen=[{"id": 5}]))
        return voice, await voice.greeting()

    voice, greeting = asyncio.run(main())
    assert greeting.endswith(f"There is one task update waiting. {REQUEST['spoken']}")
    assert voice.asks.open["id"] == 13 and list(voice.asks.waiting) == [14]


# ── What the call holds ───────────────────────────────────────


class Speaker:
    announce = None
    cancelled = False

    def cancel_enrollment(self) -> bool:
        self.cancelled = True
        return False


class STT:
    def __init__(self) -> None:
        self.speaker = Speaker()


def test_the_call_lends_its_speaker_check_and_lookups_to_the_tools_through_its_conversation():
    async def main():
        talk = conversation.begin()
        voice, session = call(talk=talk, stt=STT(), floor=Floor()), Session()
        voice.open()
        assert conversation.current() is talk
        assert talk.speaker is voice.stt.speaker and talk.lookups is voice.lookups
        voice.stt.speaker.announce("Your voiceprint is saved.")
        voice.brain.background("Merged.", "[the merge ended]", "merge it")
        [*_, merged] = voice.announcements.queue
        assert merged.text == "Merged." and merged.speak is not None
        voice.start(session)
        await settle()
        await voice.close()
        return voice, talk

    voice, talk = asyncio.run(main())
    assert talk.speaker is None and talk.lookups is None and voice.stt.speaker.cancelled
    assert conversation.current() is not talk  # the process goes back to its own conversation
    assert voice.brain.ended and voice.runner.ended
    assert all(loop.done() for loop in voice._loops)
