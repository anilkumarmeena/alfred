"""Tests for jarvis.brain.timing: LiveKit measures and JARVIS marks, one record per turn when spoken or cut off."""

import asyncio
import json

import pytest
from claude_agent_sdk import ResultMessage, StreamEvent
from livekit.agents import llm

from jarvis.apps import agent
from jarvis.brain import cli, timing
from jarvis.brain.timing import CURRENT, Timings
from jarvis.state import memory
from jarvis.voice.session import VoiceSession


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def heard_at(stopped: float, spoke_for: float = 2.0) -> dict:
    """LiveKit's metrics for a user turn that ended at `stopped`."""
    return {"started_speaking_at": stopped - spoke_for, "stopped_speaking_at": stopped, "transcription_delay": 0.3,
            "end_of_turn_delay": 0.9}


def records(path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines()]


@pytest.fixture
def timed(tmp_path):
    clock = Clock()
    timings = Timings(tmp_path / "turns.jsonl", clock)
    token = CURRENT.set(None)
    yield timings, clock, tmp_path / "turns.jsonl"
    CURRENT.reset(token)


def test_a_spoken_turn_is_one_record_of_its_stages_after_the_user_stopped(timed):
    timings, clock, path = timed
    timings.heard("what's on my calendar today", heard_at(1000.0, 1.5))
    clock.now = 1000.95
    timings.replying()
    clock.now = 1001.0
    timing.mark("sent")
    clock.now = 1002.2
    timing.mark("first_text")
    timing.tool("Checking your calendar. ")
    clock.now = 1003.0
    timing.mark("first_text")  # only the first counts
    timing.tool("Checking your calendar. You have")
    clock.now = 1004.5
    timing.mark("done")
    timings.spoke({"started_speaking_at": 1002.7, "stopped_speaking_at": 1008.7, "tts_node_ttfb": 0.21}, False)
    [r] = records(path)
    assert r == {"time": 1000.0, "turn": 1, "words": 5, "speech": 1.5, "transcript": 0.3, "turn_end": 0.9,
                 "brain": 0.95, "sent": 1.0, "first_text": 2.2, "ack": "Checking your calendar.", "first_tool": 2.2,
                 "tools": 2, "done": 4.5, "first_audio": 2.7, "tts_first": 0.21, "spoke": 6.0, "ended": "spoken"}
    assert timings.turn is None
    line = timing.describe(r)
    assert line.startswith("voice turn 1 (1.5 s of speech, 5 words): transcript 0.30, turn end 0.90, brain 0.95")
    assert "first audio 2.70" in line and "2 tools; spoken; ack 'Checking your calendar.'" in line


def test_marks_outside_a_turn_do_nothing(timed):
    timing.mark("sent")
    timing.tool("Checking.")


def test_a_reply_the_user_went_on_over_is_unspoken_with_how_long_they_paused(timed):
    timings, clock, path = timed
    timings.heard("Let's discuss what you", heard_at(1000.0))
    clock.now = 1001.0
    timings.replying()
    timings.heard("think about the delay", heard_at(1004.0, spoke_for=2.6))  # they kept talking 1.4 s after stopping
    [r] = records(path)
    assert r["ended"] == "unspoken" and r["resumed"] == 1.4 and "first_audio" not in r
    assert timings.turn.record["turn"] == 2 and timings.turn.record["words"] == 4


def test_a_reply_talked_over_while_speaking_is_interrupted(timed):
    timings, clock, path = timed
    timings.heard("tell me a long story", heard_at(1000.0))
    timings.replying()
    timings.spoke({"started_speaking_at": 1003.0, "stopped_speaking_at": 1004.0}, True)
    assert records(path)[0]["ended"] == "interrupted"


def test_only_a_reply_to_the_turn_under_way_ends_it(timed):
    timings, clock, path = timed
    timings.spoke({"started_speaking_at": 990.0}, False)  # the greeting: no turn yet
    timings.heard("hello", heard_at(1000.0))
    timings.spoke({"started_speaking_at": 1001.0}, False)  # before the reply started: not its reply
    timings.replying()
    timings.spoke({"started_speaking_at": 999.0}, True)  # the previous reply, cut off after the user started this turn
    assert not path.exists() and timings.turn is not None
    timings.spoke({"started_speaking_at": 1001.5}, False)
    assert records(path)[0]["first_audio"] == 1.5


def test_a_typed_turn_is_timed_from_when_it_arrived(timed):
    timings, clock, path = timed
    turn = timings.replying()  # no user turn ended: typed
    clock.now = 1002.0
    timing.mark("first_text")
    timings.close()
    [r] = records(path)
    assert r["typed"] and r["brain"] == 0.0 and r["first_text"] == 2.0 and r["ended"] == "closed"
    assert turn is not timings.replying()  # and the next one is a turn of its own


def test_the_session_ending_writes_the_turn_under_way(timed):
    timings, clock, path = timed
    timings.close()
    assert not path.exists()
    timings.heard("bye", heard_at(1000.0))
    timings.close()
    assert records(path)[0]["ended"] == "closed"


# ── In the voice agent and the brain ─────────────────────────────


class Brain:
    def __init__(self, *reply: str) -> None:
        self.reply = reply

    def note(self, text: str, unsaid: str = "") -> None:
        pass

    async def respond(self, text: str):
        timing.mark("sent")
        for chunk in self.reply:
            yield chunk

    def question_open(self) -> bool:
        return False


def test_the_agent_times_its_reply_from_the_users_turn_to_its_first_words(timed):
    timings, clock, path = timed
    jarvis = agent.JarvisAgent(VoiceSession(Brain("Half past three."), timings=timings))
    message = llm.ChatMessage(role="user", content=["What's the time?"], metrics=heard_at(999.0))

    async def main():
        await jarvis.on_user_turn_completed(llm.ChatContext(), message)
        ctx = llm.ChatContext()
        ctx.add_message(role="user", content="What's the time?")
        return "".join([chunk async for chunk in jarvis.llm_node(ctx, [], None)])

    assert asyncio.run(main()) == "Half past three."
    timings.spoke({"started_speaking_at": 1000.4}, False)
    [r] = records(path)
    assert r["words"] == 3 and r["brain"] == r["sent"] == r["first_text"] == 1.0 and r["first_audio"] == 1.4


class ToolClient:
    """Fake ClaudeSDKClient: says a phrase, calls a tool, then answers."""

    async def query(self, prompt) -> None:
        pass

    async def receive_response(self):
        def event(e: dict) -> StreamEvent:
            return StreamEvent(uuid="1", session_id="s", event=e)

        yield event({"type": "content_block_delta", "delta": {"type": "text_delta", "text": "One sec."}})
        yield event({"type": "content_block_start", "content_block": {"type": "tool_use", "name": "calendar"}})
        yield event({"type": "message_start"})
        yield event({"type": "content_block_delta", "delta": {"type": "text_delta", "text": "You're free."}})
        yield ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False, num_turns=2,
                            session_id="s")


def test_the_brain_marks_sending_its_first_tool_call_and_the_end(timed, monkeypatch):
    timings, clock, path = timed
    monkeypatch.setattr(memory, "log_exchange", lambda *a, **k: None)
    brain = cli.CliBrain()
    brain.client = ToolClient()

    async def main():
        timings.heard("am I free at four", heard_at(1000.0))
        timings.replying()
        return "".join([chunk async for chunk in brain.respond("am I free at four")])

    assert asyncio.run(main()) == "One sec. You're free."
    timings.close()
    [r] = records(path)
    assert {"sent", "first_tool", "done"} <= r.keys() and r["tools"] == 1 and r["ack"] == "One sec."


# ── The summary ─────────────────────────────────────────────────


def test_the_summary_gives_each_stage_and_wait_and_the_slowest_turns(tmp_path):
    path = tmp_path / "turns.jsonl"
    rows = [{"time": 2000.0 + i, "turn": i, "words": 4, "turn_end": 0.9, "sent": 1.0, "first_text": 1.0 + i,
             "first_audio": 1.5 + i, "ended": "spoken"} for i in range(1, 4)]
    rows.append({"time": 2010.0, "turn": 4, "words": 3, "turn_end": 0.9, "brain": 1.0, "resumed": 1.6,
                 "ended": "unspoken"})
    rows.append({"time": 10.0, "turn": 1, "words": 1, "first_audio": 99.0, "ended": "spoken"})  # too old
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n{cut short")
    out = timing.summary(path, since=1000.0, slowest=2)
    assert out.startswith("4 turns: 3 spoken, 1 unspoken.")
    assert "first_audio" in out and "median  3.50 s" in out
    assert "model to first words" in out and "median  2.00 s" in out
    assert "pause before going on" in out and "median  1.60 s" in out
    slowest = out.split("Slowest to first audio:\n")[1].splitlines()
    assert len(slowest) == 2 and "voice turn 3" in slowest[0] and "99" not in out
    assert timing.summary(tmp_path / "none.jsonl").startswith("No turns yet")
