"""Tests for jarvis.voice.turn and brain.quick: per-user pace, and what the brain hears about each turn."""

import asyncio
import json
import time

import numpy as np
from claude_agent_sdk import ResultMessage
from livekit import rtc
from livekit.agents import AgentSession, llm
from livekit.plugins import silero

from jarvis.brain import quick
from jarvis.brain.persona import Notes
from jarvis.core import config
from jarvis.voice import agent, speech, timing, turn
from jarvis.voice.session import VoiceSession
from jarvis.voice.speech_gate import SpeechGate
from jarvis.voice.timing import CURRENT, Timings


def result(text: str) -> ResultMessage:
    return ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False, num_turns=1,
                         session_id="s", result=text)


class Client:
    """Fake CLI session: answers each query with the next reply after `delay` seconds."""

    def __init__(self, replies: list[str], delay: float = 0.0) -> None:
        self.replies, self.delay = list(replies), delay
        self.asked: list[str] = []
        self.closed = False

    async def query(self, text: str) -> None:
        self.asked.append(text)

    async def receive_response(self):
        await asyncio.sleep(self.delay)
        yield result(self.replies.pop(0))

    async def disconnect(self) -> None:
        self.closed = True


class Connect:
    def __init__(self, *clients: Client) -> None:
        self.clients = list(clients)
        self.made: list[Client] = []

    async def __call__(self) -> Client:
        self.made.append(self.clients.pop(0))
        return self.made[-1]


# ── The warm session ─────────────────────────────────────────────


def test_one_warm_session_answers_call_after_call():
    connect = Connect(Client(['{"route": "quick"}', '{"route": "main"}']))
    q = quick.Quick("test", "prompt", connect=connect)

    async def main():
        q.warm()
        return [await q.ask("one", 1.0), await q.ask("two", 1.0)]

    assert asyncio.run(main()) == ['{"route": "quick"}', '{"route": "main"}']
    assert len(connect.made) == 1 and connect.made[0].asked == ["one", "two"]


def test_a_slow_answer_is_none_and_the_session_is_read_to_its_end_not_reused_mid_answer():
    slow, fresh = Client(["late"], delay=0.2), Client(["on time"])
    connect = Connect(slow, fresh)
    q = quick.Quick("test", "prompt", connect=connect)

    async def main():
        first = await q.ask("one", 0.05)
        second = await q.ask("two", 1.0)  # waits for the first call, on the same session
        return first, second

    slow.replies.append("on time")
    assert asyncio.run(main()) == (None, "on time")
    assert slow.asked == ["one", "two"] and not fresh.asked


def test_a_call_whose_caller_has_gone_is_skipped():
    client = Client(["first", "second"], delay=0.1)
    q = quick.Quick("test", "prompt", connect=Connect(client))

    async def main():
        first = asyncio.ensure_future(q.ask("one", 1.0))
        await asyncio.sleep(0.01)
        second = asyncio.ensure_future(q.ask("two", 1.0))
        await asyncio.sleep(0.01)
        second.cancel()  # the caller gave up: the user spoke again
        await first
        await asyncio.sleep(0.15)

    asyncio.run(main())
    assert client.asked == ["one"]


def test_a_failed_session_is_replaced_and_old_ones_are_retired_after_their_uses(monkeypatch):
    monkeypatch.setattr(quick, "USES", 2)

    class Broken(Client):
        async def receive_response(self):
            raise RuntimeError("gone")
            yield

    broken, a, b = Broken([]), Client(["1", "2"]), Client(["3"])
    connect = Connect(broken, a, b)
    q = quick.Quick("test", "prompt", connect=connect)

    async def main():
        out = [await q.ask(str(i), 1.0) for i in range(4)]
        await asyncio.sleep(0.01)  # retired sessions close in the background
        return out

    assert asyncio.run(main()) == [None, "1", "2", "3"]
    assert connect.made == [broken, a, b] and broken.closed and a.closed


def test_the_json_in_a_reply_is_found():
    assert quick.found('Sure: {"route": "main"} done') == {"route": "main"}
    assert quick.found("no json") == {} and quick.found(None) == {} and quick.found("[1]") == {}


# ── Each user's pace ─────────────────────────────────────────────


def test_effort_shows_in_how_their_turns_go_not_in_their_words():
    fluent, halting = turn.Pace(), turn.Pace()
    for _ in range(turn.SETTLED):
        fluent.learn({"ended": "spoken", "logprob": -0.3, "speech": 2.0, "words": 6})
        halting.learn({"ended": "spoken", "logprob": -1.4, "speech": 2.0, "words": 6})
    assert not fluent.effortful and halting.effortful
    slow = turn.Pace()
    for _ in range(turn.SETTLED):
        slow.learn({"ended": "spoken", "logprob": -0.3, "speech": 6.0, "words": 6})
    assert slow.effortful and not turn.Pace(turns=3, unsure=1.0).effortful  # too few turns to tell
    going_on = turn.Pace()
    for _ in range(turn.SETTLED):
        going_on.learn({"ended": "unspoken", "logprob": -0.3, "speech": 2.0, "words": 6})
    assert going_on.effortful
    going_on.learn({"ended": "closed"})
    assert going_on.turns == turn.SETTLED  # a turn the session closed on teaches nothing


def test_each_speakers_pace_is_kept_and_read_back():
    kept: dict[str, str] = {}
    paces = turn.Paces(lambda key, default="": kept.get(key, default), kept.__setitem__)
    paces.learn("guest", {"ended": "unspoken"})
    assert json.loads(kept["voice_pace:guest"]) == {"turns": 1, "unsure": 0.0, "went_on": 0.1, "rate": 0.0}
    assert "voice_pace:owner" not in kept
    again = turn.Paces(lambda key, default="": kept.get(key, default), kept.__setitem__)
    assert again["guest"].went_on == 0.1 and again["owner"] == turn.Pace()
    kept["voice_pace:old"] = json.dumps({"min_delay": 1.2, "max_delay": 6.0, "turns": 4})  # unknown keys are dropped
    assert again["old"] == turn.Pace(turns=4)
    kept["voice_pace:broken"] = "{not json"
    assert again["broken"] == turn.Pace()


def test_pacing_learns_for_whoever_spoke_and_hints_for_whoever_is_speaking():
    kept: dict[str, str] = {}
    pacing = turn.Pacing(turn.Paces(lambda key, default="": kept.get(key, default), kept.__setitem__))
    pacing.use("guest")
    pacing.learn({"speaker": "guest", "ended": "spoken"})
    pacing.learn({"ended": "spoken"})  # no speaker: the owner
    assert pacing.paces["guest"].turns == 1 and pacing.paces["owner"].turns == 1
    pacing.paces["guest"].turns, pacing.paces["guest"].went_on = turn.SETTLED, 0.5
    assert pacing.hints(None, None) == ["[Keep it plain: talking to you seems to take them some effort.]"]
    pacing.use("owner")
    assert pacing.hints(None, None) == []


def test_what_the_brain_is_told_about_how_a_turn_was_heard():
    pacing = turn.Pacing(turn.Paces(lambda key, default="": default, lambda key, value: None))
    assert pacing.hints(-0.3, None) == []
    hints = pacing.hints(-1.6, "Okay, great. So")
    assert 'goes on from their last message ("Okay, great. So")' in hints[0] and "unsure" in hints[1]
    pacing.paces["owner"].turns, pacing.paces["owner"].went_on = turn.SETTLED, 0.5
    assert pacing.hints(None, None)[0].startswith("[Keep it plain")


def test_clips_say_who_spoke_and_how_sure_whisper_was():
    assert turn.clips_heard([]) == ("owner", None)
    speaker, logprob = turn.clips_heard([{"words": 3, "logprob": -0.2, "speaker": "guest"},
                                         {"words": 1, "logprob": -1.0, "speaker": None}])
    assert speaker == "guest" and round(logprob, 3) == -0.4


def test_whisper_keeps_each_clip_passed_on_for_the_turn():
    reply = json.dumps({"text": "Let's discuss the delay.", "no_speech": 0.0, "avg_logprob": -0.6}).encode()

    class Executor:
        async def do_inference(self, method, data):
            return reply

    stt = speech.WhisperSTT(gate=SpeechGate("off", record=None, audio_dir=None), executor=Executor())
    asyncio.run(stt._hear([rtc.AudioFrame(np.zeros(480, np.int16).tobytes(), 48000, 1, 480)]))
    assert stt.take_clips() == [{"words": 4, "logprob": -0.6, "speaker": None}] and stt.take_clips() == []


# ── In the voice agent and the brain ─────────────────────────────


class Whisper:
    def __init__(self, hearing: int = 0, heard_at: float = 0.0) -> None:
        self.hearing, self.heard_at = hearing, heard_at


def test_a_turn_ends_on_silence_alone():
    whisper = Whisper()

    async def options():
        return AgentSession(vad=silero.VAD.load(), turn_handling=agent.turn_handling(whisper)).options.turn_handling

    found = asyncio.run(options())
    assert isinstance(found["turn_detection"], turn.Transcribed)  # no model reads the words
    assert found["endpointing"]["mode"] == "fixed" and found["endpointing"]["min_delay"] == config.TURN_SILENCE
    detector = found["turn_detection"]
    assert asyncio.run(detector.unlikely_threshold("en")) is None  # so the silence alone sets the wait
    assert asyncio.run(detector.predict_end_of_turn(None)) == 1.0


def test_the_turn_waits_for_whisper_to_finish_a_clip_in_flight():
    whisper = Whisper(hearing=1)
    detector = turn.Transcribed(whisper)

    async def main():
        deciding = asyncio.ensure_future(detector.predict_end_of_turn(None))
        await asyncio.sleep(0.15)
        assert not deciding.done()  # the last clip's words aren't in yet
        whisper.hearing, whisper.heard_at = 0, time.monotonic()
        return await asyncio.wait_for(deciding, 0.2)

    assert asyncio.run(main()) == 1.0


def test_a_clip_about_to_reach_whisper_is_waited_for_unless_one_just_finished(monkeypatch):
    whisper = Whisper()
    detector = turn.Transcribed(whisper)

    async def main():
        deciding = asyncio.ensure_future(detector.predict_end_of_turn(None))
        await asyncio.sleep(0)
        whisper.hearing = 1  # the clip whose speech just ended reaches Whisper
        await asyncio.sleep(0.1)
        assert not deciding.done()
        whisper.hearing = 0
        return await asyncio.wait_for(deciding, 0.2)

    assert asyncio.run(main()) == 1.0
    whisper.heard_at = time.monotonic()  # this check was started by the transcript that just came in
    started = time.monotonic()
    asyncio.run(detector.predict_end_of_turn(None))
    assert time.monotonic() - started < turn.Transcribed.SETTLE
    monkeypatch.setattr(turn.Transcribed, "MOST", 0.05)
    whisper.hearing = 1  # Whisper stuck: the turn ends anyway
    assert asyncio.run(asyncio.wait_for(detector.predict_end_of_turn(None), 1.0)) == 1.0


def test_framing_goes_with_the_next_turn_only():
    notes = Notes()
    notes.frame(["[Speech recognition was unsure of these words.]"])
    assert notes.turn("the delay").endswith("\nthe delay\n[Speech recognition was unsure of these words.]")
    assert notes.turn("again").endswith("\nagain")


class Brain:
    def __init__(self) -> None:
        self.framed: list[list[str]] = []
        self.next_model = None

    def note(self, text: str, unsaid: str = "") -> None:
        pass

    def frame(self, lines: list[str]) -> None:
        self.framed.append(lines)

    async def respond(self, text: str):
        yield "Sure."

    def question_open(self) -> bool:
        return False


class Clips:
    def __init__(self, *turns: list[dict]) -> None:
        self.turns = list(turns)

    def take_clips(self) -> list[dict]:
        return self.turns.pop(0)


class Router:
    def __init__(self) -> None:
        self.guessed: list[str] = []

    def guess(self, words: str) -> None:
        self.guessed.append(words)

    async def pick(self, text: str):
        return None


def test_the_agent_logs_who_spoke_each_turn_and_tells_the_brain_one_went_on_from_the_last(tmp_path):
    token = CURRENT.set(None)
    learned: list[dict] = []
    timings = Timings(tmp_path / "turns.jsonl", written=learned.append)
    pacing = turn.Pacing(turn.Paces(lambda key, default="": default, lambda key, value: None))
    brain, router = Brain(), Router()
    voice = VoiceSession(brain, timings=timings, pacing=pacing, router=router,
                         stt=Clips([{"words": 3, "logprob": -0.4, "speaker": None}],
                                   [{"words": 5, "logprob": -1.5, "speaker": None}]))
    jarvis = agent.JarvisAgent(voice)

    async def say(text: str, stopped: float) -> str:
        await asyncio.sleep(0)
        voice.transcribed(text)
        message = llm.ChatMessage(role="user", content=[text],
                                  metrics={"started_speaking_at": stopped - 1, "stopped_speaking_at": stopped})
        await jarvis.on_user_turn_completed(llm.ChatContext(), message)
        ctx = llm.ChatContext()
        ctx.add_message(role="user", content=text)
        return "".join([c async for c in jarvis.llm_node(ctx, [], None)])

    async def main():
        await say("Okay, great. So", 1000.0)
        await say("let's discuss what to do next.", 1002.0)  # before any of the first reply was heard

    try:
        asyncio.run(main())
    finally:
        CURRENT.reset(token)
    [first] = learned
    assert first["ended"] == "unspoken" and first["resumed"] == 1.0 and first["speaker"] == "owner"
    assert first["logprob"] == -0.4 and not {"eot", "judge", "wait"} & first.keys()
    assert "resumed after 1.00 s" in timing.describe(first)
    assert router.guessed == ["Okay, great. So", "let's discuss what to do next."]  # each turn's own words
    assert brain.framed[0][0].startswith('[This goes on from their last message ("Okay, great. So")')
    assert "unsure" in brain.framed[0][1] and len(brain.framed) == 1
