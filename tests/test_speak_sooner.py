"""Tests for speaking sooner: first clause goes to Kokoro early; a lighter model answers simple turns, same session."""

import asyncio

from claude_agent_sdk import ResultMessage, StreamEvent

from alfred.brain import SPEAK_NOW, SpeakNow, cli, filler, timing
from alfred.brain.route import Router
from alfred.brain.timing import CURRENT, Timings
from alfred.llm import quick
from alfred.state import memory
from alfred.voice import clauses
from alfred.voice.clauses import ClauseTokenizer


async def streamed(text: str) -> list[str]:
    stream = ClauseTokenizer().stream()
    for word in text.split():
        stream.push_text(word + " ")
    stream.end_input()
    return [ev.token.strip() async for ev in stream]


def test_a_short_first_sentence_goes_alone_and_later_ones_wait_for_company():
    text = "Half past three. Free till five. Then the review."
    assert asyncio.run(streamed(text)) == ["Half past three.", "Free till five. Then the review."]


def test_a_long_first_sentence_goes_at_its_first_pause():
    text = "I found two flights tomorrow morning, both leaving Heathrow before nine. Shall I book one?"
    assert asyncio.run(streamed(text)) == ["I found two flights tomorrow morning,", "both leaving Heathrow before nine.",
                                           "Shall I book one?"]
    assert ClauseTokenizer().tokenize(text)[0] == "I found two flights tomorrow morning, both leaving Heathrow before nine."


def test_a_long_first_sentence_with_no_pause_goes_at_its_first_joining_word():
    text = "I found two flights tomorrow morning that both leave Heathrow before nine. Shall I book one?"
    assert asyncio.run(streamed(text))[0] == "I found two flights tomorrow morning"
    # a later sentence keeps whole: only the reply's first piece is hurried
    later = "Here's what I found. The two flights both leave Heathrow before nine and land in Lisbon at noon."
    assert asyncio.run(streamed(later))[1] == "The two flights both leave Heathrow before nine and land in Lisbon at noon."


def test_no_cut_too_early_or_too_late_for_a_joining_word():
    assert clauses.clause_spans("So I think we should go and then eat later.", 6, 4, soon=True) == [(0, 23), (23, 43)]
    one_word = "Yes and I can do this for you right now today."  # "and" after one word: too early, and no other
    assert clauses.clause_spans(one_word, 6, 4, soon=True) == [(0, len(one_word))]


def test_the_first_piece_is_timed_with_the_turn(tmp_path):
    token = CURRENT.set(None)
    try:
        timings = Timings(tmp_path / "turns.jsonl")
        timings.heard("what time is it", {"stopped_speaking_at": 1000.0})
        timings.replying()
        asyncio.run(streamed("Half past three. You're free until five."))
        assert timings.turn.record["piece"] == 3
    finally:
        CURRENT.reset(token)


# ── A lighter model for simple turns ─────────────────────────────


class Client:
    def __init__(self, replies: list[str], delay: float = 0.0) -> None:
        self.replies, self.delay = list(replies), delay
        self.asked: list[str] = []

    async def query(self, text: str) -> None:
        self.asked.append(text)

    async def receive_response(self):
        await asyncio.sleep(self.delay)
        yield ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False, num_turns=1,
                            session_id="s", result=self.replies.pop(0))

    async def disconnect(self) -> None:
        pass


def router(*replies: str, delay: float = 0.0) -> tuple[Router, Client]:
    client = Client(list(replies), delay)

    async def connect():
        return client

    return Router("claude-sonnet-5", quick.Quick("test", "prompt", connect=connect)), client


def test_a_turn_judged_simple_goes_to_the_lighter_model_and_anything_else_stays():
    r, client = router('{"route": "quick"}', '{"route": "main"}')

    async def main():
        r.guess("thanks  a lot")
        first = await r.pick("thanks a lot")
        r.guess("what's on my calendar")
        return first, await r.pick("what's on my calendar"), await r.pick("never guessed")

    assert asyncio.run(main()) == ("claude-sonnet-5", None, None)
    assert client.asked == ["<words>thanks a lot</words>", "<words>what's on my calendar</words>"]


def test_a_slow_verdict_leaves_the_main_model(monkeypatch):
    monkeypatch.setattr("alfred.brain.route.WAIT_SECONDS", 0.05)
    r, _ = router('{"route": "quick"}', delay=0.3)

    async def main():
        r.guess("hello there")
        return await r.pick("hello there")

    assert asyncio.run(main()) is None


class SessionClient:
    """Fake ClaudeSDKClient for the voice brain."""

    def __init__(self) -> None:
        self.models: list[str] = []

    async def set_model(self, model: str) -> None:
        self.models.append(model)

    async def query(self, prompt) -> None:
        pass

    async def receive_response(self):
        yield StreamEvent(uuid="1", session_id="s",
                          event={"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Hi."}})
        yield ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False, num_turns=1,
                            session_id="s")


def test_the_brain_answers_one_turn_on_the_lighter_model_then_goes_back(tmp_path, monkeypatch):
    monkeypatch.setattr(memory, "log_exchange", lambda *a, **k: None)
    brain = cli.CliBrain()
    brain.client = SessionClient()
    brain._main = brain._model = "claude-opus-5-5"
    token = CURRENT.set(None)
    timings = Timings(tmp_path / "turns.jsonl")

    async def turn(text: str, model: str | None) -> dict:
        timings.heard(text, {"stopped_speaking_at": 1000.0})
        record = timings.replying().record
        brain.next_model = model
        "".join([c async for c in brain.respond(text)])
        return record

    async def main():
        return [await turn("thanks", "claude-sonnet-5"), await turn("and my calendar?", None),
                await turn("cheers", None)]

    try:
        quick_turn, main_turn, _ = asyncio.run(main())
    finally:
        CURRENT.reset(token)
    assert brain.client.models == ["claude-sonnet-5", "claude-opus-5-5"]
    assert quick_turn["model"] == "claude-sonnet-5" and "model" not in main_turn
    assert brain.next_model is None
    assert timing.describe(quick_turn | {"ended": "spoken"})


# ── Speaking before a tool runs ──────────────────────────────────


def _text(text: str) -> StreamEvent:
    return StreamEvent(uuid="1", session_id="s",
                       event={"type": "content_block_delta", "delta": {"type": "text_delta", "text": text}})


def _tool_starts(name: str = "calendar_events") -> StreamEvent:
    return StreamEvent(uuid="1", session_id="s",
                       event={"type": "content_block_start", "content_block": {"type": "tool_use", "name": name}})


class ToolClient(SessionClient):
    """Says a line, calls a tool, then answers."""

    def __init__(self, events: list) -> None:
        super().__init__()
        self.events = events

    async def receive_response(self):
        for event in self.events:
            yield event
        yield ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False, num_turns=1,
                            session_id="s")


def _reply(monkeypatch, events: list) -> list[str]:
    monkeypatch.setattr(memory, "log_exchange", lambda *a, **k: None)
    brain = cli.CliBrain()
    brain.client = ToolClient(events)
    brain._main = brain._model = "claude-opus-5-5"

    async def main():
        return [c async for c in brain.respond("am I free at four?")]

    return asyncio.run(main())


def test_words_before_a_tool_call_are_released_as_the_tool_starts(monkeypatch):
    message_start = StreamEvent(uuid="1", session_id="s", event={"type": "message_start"})
    chunks = _reply(monkeypatch, [_text("Let me check."), _tool_starts(), message_start, _text("You're free.")])
    assert chunks == ["Let me check.", "", " ", "You're free."] and chunks[1] is SPEAK_NOW
    assert "".join(chunks) == "Let me check. You're free."


def test_a_tool_call_before_any_words_gets_a_short_lead_in_at_once(monkeypatch):
    """49 of 61 tool turns had nothing said before the tool, and sat silent ~13 s."""
    message_start = StreamEvent(uuid="1", session_id="s", event={"type": "message_start"})
    chunks = _reply(monkeypatch, [_tool_starts(), _tool_starts("calendar_free_time"), message_start,
                                  _text("You're free.")])
    lead_in, again, space, answer = chunks
    assert isinstance(lead_in, SpeakNow) and lead_in in filler.PHRASES["look"]
    assert again is SPEAK_NOW and space == " " and answer == "You're free."  # one lead-in a reply


def test_words_of_the_models_own_before_a_tool_get_no_lead_in(monkeypatch):
    chunks = _reply(monkeypatch, [_text("Checking your calendar."), _tool_starts()])
    assert chunks == ["Checking your calendar.", ""] and chunks[1] is SPEAK_NOW


def test_lead_ins_fit_the_tool_and_never_repeat():
    import random

    assert filler.kind("mcp__alfred__task_list") == "tasks" and filler.kind("mcp__alfred__task_start") == "start"
    assert filler.kind("mcp__gitlab__list_todos") == "look" and filler.kind("mcp__alfred__remember") == "other"
    rng, last = random.Random(4), None
    for _ in range(20):
        said = filler.pick("mcp__alfred__file_lookup", last, rng)
        assert said in filler.PHRASES["look"] and said != last
        last = said


def test_the_agent_ends_its_speech_where_the_brain_says_to_speak_now():
    from livekit.agents import FlushSentinel, llm

    from alfred.apps.agent import AlfredAgent

    class Voice:
        async def respond(self, text):
            for chunk in ("Let me check.", SPEAK_NOW, " You're free."):
                yield chunk

    chat = llm.ChatContext()
    chat.add_message(role="user", content="am I free at four?")

    async def main():
        return [c async for c in AlfredAgent(Voice()).llm_node(chat, [], None)]

    first, flush, rest = asyncio.run(main())
    assert first == "Let me check." and isinstance(flush, FlushSentinel) and rest == " You're free."


def test_the_agent_speaks_a_lead_in_then_ends_its_speech():
    from livekit.agents import FlushSentinel, llm

    from alfred.apps.agent import AlfredAgent

    class Voice:
        async def respond(self, text):
            for chunk in (SpeakNow("One moment."), " You're free."):
                yield chunk

    chat = llm.ChatContext()
    chat.add_message(role="user", content="am I free at four?")

    async def main():
        return [c async for c in AlfredAgent(Voice()).llm_node(chat, [], None)]

    said, flush, rest = asyncio.run(main())
    assert said == "One moment." and type(said) is str and isinstance(flush, FlushSentinel)
