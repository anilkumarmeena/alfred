"""JARVIS voice entry point (LiveKit side; per-call state lives in VoiceSession).

  uv run python -m jarvis.voice.agent console   talk via the computer's mic and speakers in the terminal
  uv run python -m jarvis.voice.agent dev       serve rooms on the local LiveKit server, with reload
  uv run python -m jarvis.voice.agent start     serve rooms on the local LiveKit server (launchd, 24/7)

All local: LiveKit server, Whisper STT, Silero VAD (a turn ends on silence), Kokoro TTS, claude CLI brain.
"""

import asyncio
import hashlib
import logging
import os
from urllib.parse import urlparse

from livekit.agents import Agent, AgentServer, AgentSession, JobContext, JobProcess, TurnHandlingOptions, cli, llm
from livekit.plugins import silero

from jarvis.brain import make_brain
from jarvis.brain.route import Router
from jarvis.core import config, credentials, platform
from jarvis.notify import push
from jarvis.state import conversation, vocabulary, voiceprints
from jarvis.voice import denoise, quiet, ram, speech, timing, turn, unspoken
from jarvis.voice.barge_in import BargeIn, BargeInVAD
from jarvis.voice.floor import Moment
from jarvis.voice.session import VoiceSession
from jarvis.voice.speech import KokoroTTS, WhisperSTT

log = logging.getLogger("jarvis")


class BrainPlaceholder(llm.LLM):
    """AgentSession needs an LLM to run its text pipeline; JarvisAgent.llm_node does the real work."""

    def chat(self, **kwargs):
        raise NotImplementedError("JARVIS answers through jarvis.brain")


class JarvisAgent(Agent):
    """Bridges finished turns to the call's VoiceSession and streams replies back to LiveKit."""

    def __init__(self, voice: VoiceSession) -> None:
        super().__init__(instructions="Answers come from jarvis.brain.")
        self.voice = voice

    async def on_user_turn_completed(self, turn_ctx: llm.ChatContext, new_message: llm.ChatMessage) -> None:
        self.voice.completed(new_message.text_content or "", new_message.metrics)

    async def llm_node(self, chat_ctx: llm.ChatContext, tools, model_settings):
        text = next(
            (m.text_content for m in reversed(chat_ctx.items) if isinstance(m, llm.ChatMessage) and m.role == "user"),
            None,
        )
        async for chunk in self.voice.respond(text):
            yield chunk

    # Everything said (replies and session.say alike) passes these two, so private notes are neither heard nor shown
    def tts_node(self, text, model_settings):
        return Agent.default.tts_node(self, unspoken.scrubbed(text), model_settings)

    def transcription_node(self, text, model_settings):
        return Agent.default.transcription_node(self, unspoken.scrubbed(text), model_settings)


def prewarm(proc: JobProcess) -> None:
    """Per-job-process setup. Whisper and Kokoro live in the shared speech process, not here."""
    import jarvis.brain.cli  # noqa: F401  (preload tools, persona, Agent SDK before a user connects)

    # VAD_MIN_SPEECH keeps clicks and breaths from starting an utterance; barge-in keeps its own 0.5 s
    proc.userdata["vad"] = silero.VAD.load(min_speech_duration=config.VAD_MIN_SPEECH,
                                           min_silence_duration=config.VAD_MIN_SILENCE,
                                           activation_threshold=config.VAD_THRESHOLD)
    proc.userdata["denoise"] = denoise.load()  # None (raw mic) if JARVIS_DENOISE=off or the model fails
    whisper = WhisperSTT()
    whisper.warm_up()  # loads the speech gate's VAD
    proc.userdata["stt"] = whisper
    proc.userdata["tts"] = KokoroTTS()
    proc.userdata["tts_stream"] = speech.streaming(proc.userdata["tts"])  # the session's TTS


# Whisper and Kokoro load once, in LiveKit's shared inference process: a copy per job process (spares included)
# makes an 8 GB machine swap. Spares hold only the VAD and noise filter.
speech.register_runners()
os.environ["LIVEKIT_URL"] = config.LIVEKIT_URL  # the worker reads it from env
# LiveKit keys from the vault (or .env on first setup)
# LiveKit sends the worker's connection through HTTPS_PROXY/HTTP_PROXY, ignoring NO_PROXY; a company proxy can't
# reach this machine's server and answers 500, so a local server goes direct
LOCAL_LIVEKIT = urlparse(config.LIVEKIT_URL).hostname in ("127.0.0.1", "localhost", "::1")
server = AgentServer(setup_fnc=prewarm, num_idle_processes=config.VOICE_IDLE_PROCESSES, initialize_process_timeout=120,
                     api_key=credentials.get(credentials.LIVEKIT_KEY),
                     api_secret=credentials.get(credentials.LIVEKIT_SECRET),
                     **({"http_proxy": None} if LOCAL_LIVEKIT else {}))

def turn_handling(stt: WhisperSTT) -> TurnHandlingOptions:
    return {
        # A turn ends on silence alone, once Whisper has all its words: no model reads them, so nothing waits on one
        "turn_detection": turn.Transcribed(stt),
        "endpointing": {"mode": "fixed", "min_delay": config.TURN_SILENCE},
        # Local only: VAD barge-in (the "adaptive" interruption model needs LiveKit Cloud)
        "interruption": {"mode": "vad"},
        # Speculative replies would put half-heard turns into the brain's history.
        "preemptive_generation": {"enabled": False},
    }


@server.rtc_session()
async def entrypoint(ctx: JobContext) -> None:
    talk = conversation.begin()  # conversation state and profile
    stt = ctx.proc.userdata["stt"]
    barge = BargeIn(stt.speaker)  # while replying, only the user's voice interrupts
    pacing = turn.Pacing()  # each speaker's pace, for the brain's hints
    session = AgentSession(
        stt=stt,
        llm=BrainPlaceholder(),
        tts=ctx.proc.userdata["tts_stream"],
        vad=BargeInVAD(denoise.session_vad(ctx.proc.userdata["vad"], ctx.proc.userdata["denoise"]), barge),
        turn_handling=turn_handling(stt),
    )
    # Read per call so Settings changes (or a new voiceprint) need no restart
    ctx.proc.userdata["tts"].use(config.tts_voice(), config.tts_speed())
    mode = config.speech_gate()
    stt.gate.use(mode, config.speech_gate_save_audio(mode))
    stt.use_vocabulary(await asyncio.to_thread(vocabulary.load), config.STT_PROMPT)
    stt.take_clips()  # drop the last conversation's clips
    stt.speaker.use(config.speaker_check(), await asyncio.to_thread(voiceprints.load), config.speaker_threshold())
    speaker_ready = asyncio.create_task(asyncio.to_thread(stt.speaker.warm_up))  # while the room connects
    voice = VoiceSession(make_brain(), talk, stt=stt, pacing=pacing,
                         router=Router(config.VOICE_QUICK_MODEL) if config.VOICE_QUICK_MODEL else None,
                         moment=Moment() if config.PROACTIVE_JUDGE else None,  # when to raise things
                         timings=timing.Timings(written=pacing.learn),  # logs/voice-turns.jsonl
                         push=_to_phone)  # unspoken announcements go to the phone
    voice.open()

    @session.on("user_input_transcribed")
    def heard(ev) -> None:
        if ev.is_final:
            voice.transcribed(ev.transcript)

    @session.on("agent_state_changed")
    def speaking(ev) -> None:
        stt.speaker.jarvis_speaking(ev.new_state == "speaking")  # a short clip right after is likely a reply
        barge.replying(ev.new_state)

    @session.on("conversation_item_added")
    def added(ev) -> None:
        _item_added(voice, ev.item)

    ctx.add_shutdown_callback(voice.close)
    warmup = asyncio.create_task(voice.brain.start())  # start the CLI while the room connects
    greeting = asyncio.create_task(voice.greeting())
    await session.start(
        room=ctx.room,
        agent=JarvisAgent(voice),
        room_options=denoise.room_options(ctx.proc.userdata["denoise"]),  # mic through DeepFilterNet
    )
    quiet.follow(session, ctx.room)  # speaker off: text-only replies
    quiet.apply(session, (await ctx.wait_for_participant()).attributes)  # initial switch, before the greeting
    hello = await greeting
    voice.greeted(hello)  # it may hold a task's question
    said = session.say(hello)
    await said  # until played out, cut off or failed
    voice.start(session, heard=not said.interrupted and said.exception() is None)
    await asyncio.gather(warmup, speaker_ready)


def _item_added(voice: VoiceSession, item: llm.ChatItem) -> None:
    """Pass on what JARVIS said; other items (tool calls, agent handoffs, config updates) have no role."""
    if isinstance(item, llm.ChatMessage) and item.role == "assistant":
        voice.spoke(item.metrics, item.interrupted)


async def _to_phone(text: str) -> None:
    """Push an announcement the conversation didn't take to the phone."""
    if config.PUSH_ENABLED:
        await push.send_push("JARVIS", text, "/", "voice-" + hashlib.sha256(text.encode()).hexdigest()[:16])


def _child_memory() -> None:
    """Make child processes return freed memory to the system (unless JARVIS_VOICE_MALLOC_LARGE_CACHE).

    macOS malloc caches large freed blocks; Kokoro's buffers for one long sentence kept the speech process 1 GB bigger.
    glibc does the same once its mmap threshold has grown to the largest block freed, so it's pinned at its default.
    Read at process start, so it must be set before the worker spawns children."""
    if not config.VOICE_MALLOC_LARGE_CACHE:
        os.environ.setdefault("MallocLargeCache", "0")
        if platform.LINUX:
            os.environ.setdefault("MALLOC_MMAP_THRESHOLD_", "131072")


if __name__ == "__main__":
    _child_memory()
    ram.watch(config.VOICE_MEMORY_LOG_MINUTES)  # periodic memory line in logs/agent.log
    cli.run_app(server)
