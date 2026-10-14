"""Scratch end-to-end check (deleted after): real Silero VAD + LiveKit AgentSession + JarvisAgent, Kokoro speech in."""

import asyncio
import time

import numpy as np
from kokoro_onnx import Kokoro
from livekit import rtc
from livekit.agents import AgentSession, stt
from livekit.agents.voice.io import AudioInput
from livekit.plugins import silero

from jarvis.core import config
from jarvis.voice import agent, timing
from jarvis.voice.session import VoiceSession

MODELS = "/Users/anilmeena/Desktop/projects/jarvis/models/"
RATE = 24000
FRAME = RATE // 100
STT_SECONDS = 1.1  # Whisper's time on a short clip, from the turn log (transcript ~1.6 s incl. 0.55 s VAD silence)
KOKORO = Kokoro(MODELS + "kokoro-v1.0.onnx", MODELS + "voices-v1.0.bin")


def speak(text: str) -> np.ndarray:
    audio, rate = KOKORO.create(text, voice="am_michael", speed=1.0)
    assert rate == RATE
    return (np.clip(audio, -1, 1) * 32767).astype(np.int16)


class Feed(AudioInput):
    def __init__(self, script) -> None:
        super().__init__(label="feed")
        parts, self.ends, at = [], [], 0
        for s, q in script:
            part = s if s is not None else np.zeros(int(q * RATE), np.int16)
            parts.append(part)
            at += len(part)
            if s is not None:
                self.ends.append(at)
        self.samples = np.concatenate(parts)
        self.i, self.start = 0, None

    async def __anext__(self) -> rtc.AudioFrame:
        if self.start is None:
            self.start = time.time()
        await asyncio.sleep(max(0.0, self.start + self.i / RATE - time.time()))
        chunk = self.samples[self.i:self.i + FRAME]
        if len(chunk) < FRAME:
            chunk = np.zeros(FRAME, np.int16)
        self.i += FRAME
        return rtc.AudioFrame(chunk.tobytes(), RATE, 1, FRAME)


class FakeWhisper(stt.STT):
    def __init__(self, texts: list[str]) -> None:
        super().__init__(capabilities=stt.STTCapabilities(streaming=False, interim_results=False))
        self.texts = list(texts)
        self.hearing, self.heard_at = 0, 0.0

    async def _recognize_impl(self, buffer, *, language=None, conn_options=None):
        self.hearing += 1
        try:
            await asyncio.sleep(STT_SECONDS)
        finally:
            self.hearing -= 1
            self.heard_at = time.monotonic()
        text = self.texts.pop(0) if self.texts else ""
        return stt.SpeechEvent(type=stt.SpeechEventType.FINAL_TRANSCRIPT,
                               alternatives=[stt.SpeechData(language="en", text=text)])

    def take_clips(self):
        return []


class Brain:
    def __init__(self) -> None:
        self.asked: list[tuple[float, str]] = []
        self.next_model = None

    def note(self, *a) -> None:
        pass

    def frame(self, lines) -> None:
        pass

    async def respond(self, text: str):
        self.asked.append((time.time(), text))
        yield "It's half past three."

    def question_open(self) -> bool:
        return False


async def run(script, texts, seconds):
    feed = Feed(script)
    whisper, brain = FakeWhisper(texts), Brain()
    vad = silero.VAD.load(min_speech_duration=config.VAD_MIN_SPEECH, min_silence_duration=config.VAD_MIN_SILENCE,
                          activation_threshold=config.VAD_THRESHOLD)
    session = AgentSession(stt=whisper, llm=agent.BrainPlaceholder(), vad=vad,
                           turn_handling=agent.turn_handling(whisper))
    voice = VoiceSession(brain, timings=timing.Timings(path=None))
    await session.start(agent=agent.JarvisAgent(voice))
    session.input.audio = feed
    await asyncio.sleep(seconds)
    await session.aclose()
    return [feed.start + e / RATE for e in feed.ends], brain.asked


def test_a_finished_question_is_answered_once_whisper_has_it():
    ends, asked = asyncio.run(run([(None, 0.5), (speak("What's the time?"), None), (None, 3.0)],
                                  ["What's the time?"], 5.0))
    print(f"\none clip: brain {asked[0][0] - ends[-1]:.2f} s after speech ended")
    assert [a[1] for a in asked] == ["What's the time?"]


def test_a_pause_mid_sentence_waits_for_the_last_clips_words():
    first, second = speak("Remind me to call mum"), speak("tomorrow at five.")
    ends, asked = asyncio.run(run([(None, 0.5), (first, None), (None, 0.8), (second, None), (None, 4.0)],
                                  ["Remind me to call mum", "tomorrow at five."], 9.0))
    print(f"\n0.8 s pause: brain asked {[a[1] for a in asked]}, {asked[0][0] - ends[-1]:.2f} s after the end")
    assert [a[1] for a in asked] == ["Remind me to call mum tomorrow at five."]
