"""Tests for voice reply routing (JarvisAgent, VoiceSession.reply): LiveKit turns, yes scope, rules, brain questions."""

import asyncio

from livekit.agents import llm
from test_consent import model  # noqa: F401  (the fixture)
from test_voice_session import REQUEST, Brain, Runner, call, quiet, reply  # noqa: F401  (quiet: the fixture)

from jarvis.safety import consent
from jarvis.voice import agent, session as voice_session

RULE = {"program": "npm", "why": "it builds", "words": ["npm", "run", "build"]}


def asked(voice) -> None:
    """The request put to the user, as ask_approvals puts it."""
    voice.asks.add(REQUEST)
    voice.floor.said(voice.asks.ask(voice.asks.due()))


# ── LiveKit's side ──────────────────────────────────────────────

def test_the_turn_is_the_last_thing_the_user_said_not_an_earlier_one_or_jarvis_words():
    ctx = llm.ChatContext()
    ctx.add_message(role="user", content="What's on today?")
    ctx.add_message(role="assistant", content="Two meetings.")
    ctx.add_message(role="user", content="And tomorrow?")
    ctx.add_message(role="assistant", content="(still thinking)")
    brain = Brain("Nothing yet.")
    jarvis = agent.JarvisAgent(call(brain))

    async def main():
        return "".join([chunk async for chunk in jarvis.llm_node(ctx, [], None)])

    assert asyncio.run(main()) == "Nothing yet." and brain.asked == ["And tomorrow?"]


def test_a_turn_with_no_words_gets_no_reply_but_still_answers_the_question_open():
    brain = Brain()
    voice = call(brain)
    jarvis = agent.JarvisAgent(voice)

    async def main():
        voice.floor.said("Shall I go ahead?")
        assert voice.floor.question
        empty = "".join([chunk async for chunk in jarvis.llm_node(llm.ChatContext(), [], None)])
        return empty, await reply(voice, "")

    assert asyncio.run(main()) == ("", "") and brain.asked == []
    assert not voice.floor.question


# ── How far a yes goes ──────────────────────────────────────────

def test_a_yes_for_this_session_is_remembered_with_the_answer():
    async def main():
        voice = call()
        asked(voice)
        return voice, await reply(voice, "Yes, for this session.")

    voice, said = asyncio.run(main())
    assert voice.runner.answers == [(13, "approve", "voice", "session", True)] and voice.brain.asked == []
    assert said.startswith("Okay, the task carries on.") and voice.talk.offer.open is None


def test_a_yes_always_offers_the_rule_and_the_next_yes_saves_it():
    async def main():
        voice = call()
        asked(voice)
        first = await reply(voice, "Yes, always.")
        offered = dict(voice.talk.offer.open or {})
        return voice, first, offered, await reply(voice, "Yes.")

    voice, first, offered, second = asyncio.run(main())
    assert voice.runner.answers == [(13, "approve", "voice", "once", True)]
    assert first.startswith("Okay, the task carries on. From now on,") and first.endswith("Save that rule?")
    assert offered["approval"] == 13 and voice.brain.notes[-2].endswith("Save that rule?")
    assert second.startswith("Saved.") and voice.runner.kept == [(13, offered["breadth"], "voice")]
    assert voice.brain.asked == []


def test_a_rule_that_cant_be_saved_says_so_and_the_brain_hears_what_was_agreed():
    class Failing(Runner):
        async def remember(self, approval_id, breadth, via):
            raise OSError("database is locked")

    async def main():
        voice = call(runner=Failing())
        voice.talk.offer.put([], approval=13)
        return voice, await reply(voice, "Yes.")

    voice, said = asyncio.run(main())
    assert said == "Sorry, I couldn't save that rule." and "agreed to remembering" in voice.brain.notes[-1]


def test_the_voice_shells_own_rule_is_saved_here_not_by_the_runner(monkeypatch):
    saved = []

    def save(rules, scope, example, via):
        saved.append((rules, scope, example, via))
        return [{"id": 7}]

    monkeypatch.setattr(voice_session.remembered, "save", save)

    async def main():
        voice = call()
        voice.talk.offer.put([RULE], example="npm run build")
        return voice, await reply(voice, "Yes.")

    voice, said = asyncio.run(main())
    assert said.startswith("Saved.") and voice.runner.kept == []
    assert saved == [([RULE], "always", "npm run build", "voice")]


def test_anything_but_a_yes_or_no_to_a_rule_drops_it_and_goes_to_the_brain():
    async def main():
        voice = call(Brain("It covers npm builds."))
        voice.talk.offer.put([], approval=13)
        return voice, await reply(voice, "What would that cover?")

    voice, said = asyncio.run(main())
    assert said == "It covers npm builds." and voice.talk.offer.open is None and voice.runner.kept == []


# ── Words the lists can't read ──────────────────────────────────

def test_words_the_lists_cant_read_go_to_the_consent_model_with_whats_waiting(model):
    model.answer = '{"answer": "yes", "scope": "session"}'
    words = "fine by me, crack on with that"
    assert consent.words(words) is None  # only the model can tell

    async def main():
        voice = call()
        asked(voice)
        return voice, await reply(voice, words)

    voice, said = asyncio.run(main())
    assert said.startswith("Okay, the task carries on.") and voice.brain.asked == []
    assert voice.runner.answers == [(13, "approve", "voice", "session", True)]
    assert len(model.prompts) == 1 and words in model.prompts[0] and REQUEST["summary"] in model.prompts[0]


def test_words_the_model_finds_unclear_go_to_the_brain(model):
    model.answer = '{"answer": "unclear"}'

    async def main():
        voice = call(Brain("Which one?"))
        asked(voice)
        return voice, await reply(voice, "hmm let me think about the other thing first")

    voice, said = asyncio.run(main())
    assert said == "Which one?" and voice.runner.answers == [] and voice.asks.open is None


# ── The brain's own question ────────────────────────────────────

def test_a_request_put_while_the_brain_answered_is_withdrawn_when_its_reply_asks_its_own_question():
    class Asking(Brain):
        voice = None  # the call it answers in

        async def respond(self, text: str):
            self.asked.append(text)
            if len(self.asked) > 1:
                self.asking = False
                yield "Deleting them."
                return
            yield "Clearing the cache. "
            asked(self.voice)  # a task's request comes up meanwhile
            self.asking = True
            yield "Shall I delete the old renders too?"

    async def main():
        brain = Asking()
        brain.voice = voice = call(brain)
        said = await reply(voice, "Clear the cache.")
        return voice, said, await reply(voice, "Yes.")

    voice, said, then = asyncio.run(main())
    assert said == "Clearing the cache. Shall I delete the old renders too?" and then == "Deleting them."
    assert voice.asks.open is None and voice.runner.answers == []  # the yes is the brain's, not the request's
    assert voice.brain.asked == ["Clear the cache.", "Yes."] and voice.asks.due() is None


def test_only_jarvis_s_own_messages_count_as_spoken():
    spoke = []
    voice = type("Voice", (), {"spoke": lambda self, metrics, interrupted: spoke.append((metrics, interrupted))})()
    agent._item_added(voice, llm.AgentHandoff(new_agent_id="jarvis"))  # has no role
    agent._item_added(voice, llm.FunctionCall(call_id="1", name="todo_list", arguments="{}"))
    agent._item_added(voice, llm.ChatMessage(role="user", content=["hi"], metrics={"stopped_speaking_at": 1.0}))
    agent._item_added(voice, llm.ChatMessage(role="assistant", content=["Hello."], interrupted=True,
                                             metrics={"started_speaking_at": 2.0}))
    assert spoke == [({"started_speaking_at": 2.0}, True)]
