"""Tests for speaking sooner: first clause goes to Kokoro early; a lighter model answers simple turns, same session."""

import asyncio

from claude_agent_sdk import ResultMessage, StreamEvent

from jarvis.brain import cli, timing
from jarvis.brain.route import Router
from jarvis.brain.timing import CURRENT, Timings
from jarvis.llm import quick
from jarvis.state import memory
from jarvis.voice.clauses import ClauseTokenizer


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
    monkeypatch.setattr("jarvis.brain.route.WAIT_SECONDS", 0.05)
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
