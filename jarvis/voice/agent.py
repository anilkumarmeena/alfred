"""JARVIS voice entry point.

  uv run python -m jarvis.voice.agent console   talk through the Mac's mic and speakers, in the terminal
  uv run python -m jarvis.voice.agent dev       serve rooms on the local LiveKit server, with reload
  uv run python -m jarvis.voice.agent start     serve rooms on the local LiveKit server (what launchd runs 24/7)

Everything runs on this Mac: LiveKit server for audio transport, Whisper for
speech-to-text, LiveKit's local turn detector, Kokoro for the voice, and the
brain (the local claude CLI) for thinking. Each call's own parts and state are one VoiceSession (jarvis.voice.session);
this module is LiveKit's side of it.
"""

import asyncio
import hashlib
import logging
import os

from livekit.agents import Agent, AgentServer, AgentSession, JobContext, JobProcess, cli, inference, llm
from livekit.plugins import silero

from jarvis.brain import make_brain
from jarvis.brain.route import Router
from jarvis.core import config, credentials
from jarvis.notify import push
from jarvis.state import conversation, vocabulary, voiceprints
from jarvis.voice import denoise, quiet, ram, speech, timing, turn
from jarvis.voice.barge_in import BargeIn, BargeInVAD
from jarvis.voice.floor import Moment
from jarvis.voice.session import VoiceSession
from jarvis.voice.speech import KokoroTTS, WhisperSTT

log = logging.getLogger("jarvis")


class BrainPlaceholder(llm.LLM):
    """AgentSession only runs its text pipeline when it has an LLM; JarvisAgent.llm_node replaces it."""

    def chat(self, **kwargs):
        raise NotImplementedError("JARVIS answers through jarvis.brain")


class JarvisAgent(Agent):
    """Each finished turn handed to the call's VoiceSession, and its reply back to LiveKit."""

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


def prewarm(proc: JobProcess) -> None:
    """Each job process's setup. Whisper and Kokoro aren't loaded here: they live in the shared process (speech)."""
    import jarvis.brain.cli  # noqa: F401  (the tools, persona and Agent SDK load here, not when a user connects)

    # A click or breath no longer starts an utterance (config.VAD_MIN_SPEECH); barge-in's 0.5 s is unchanged
    proc.userdata["vad"] = silero.VAD.load(min_speech_duration=config.VAD_MIN_SPEECH,
                                           min_silence_duration=config.VAD_MIN_SILENCE,
                                           activation_threshold=config.VAD_THRESHOLD)
    proc.userdata["denoise"] = denoise.load()  # None when JARVIS_DENOISE=off or the model won't load: the original mic
    whisper = WhisperSTT()
    whisper.warm_up()  # loads the speech gate's VAD
    proc.userdata["stt"] = whisper
    proc.userdata["tts"] = KokoroTTS()
    proc.userdata["tts_stream"] = speech.streaming(proc.userdata["tts"])  # what the session speaks through


# Whisper and Kokoro load once, in LiveKit's inference process, which the worker starts before taking calls and every
# job process shares (jarvis.voice.speech). Each job process loading its own copy, a warm spare included, made an
# 8 GB Mac swap; now a spare (JARVIS_VOICE_IDLE_PROCESSES, default 1) holds only the small VAD and noise filter.
speech.register_runners()
os.environ["LIVEKIT_URL"] = config.LIVEKIT_URL  # the worker reads it from here; .env needn't say it
# The LiveKit keys from the vault (or, on a first setup, .env: jarvis.core.credentials)
server = AgentServer(setup_fnc=prewarm, num_idle_processes=config.VOICE_IDLE_PROCESSES, initialize_process_timeout=120,
                     api_key=credentials.get(credentials.LIVEKIT_KEY),
                     api_secret=credentials.get(credentials.LIVEKIT_SECRET))


@server.rtc_session()
async def entrypoint(ctx: JobContext) -> None:
    talk = conversation.begin()  # this conversation's own state, and the profile it runs as
    stt = ctx.proc.userdata["stt"]
    barge = BargeIn(stt.speaker)  # while JARVIS replies, only the user's voice interrupts it
    # The local mini turn detector, with a fast model deciding when it's unsure (jarvis.voice.turn)
    turns = turn.JudgedTurns(inference.TurnDetector(version="v1-mini", local_fallback=True),
                             turn.Judge() if config.TURN_JUDGE else None)
    pacing = turn.Pacing()  # each user's own end-of-turn delays, the owner's to start with
    session = AgentSession(
        stt=stt,
        llm=BrainPlaceholder(),
        tts=ctx.proc.userdata["tts_stream"],
        vad=BargeInVAD(denoise.session_vad(ctx.proc.userdata["vad"], ctx.proc.userdata["denoise"]), barge),
        turn_handling={
            # Local models only, and VAD-based barge-in (the "adaptive" interruption model runs on LiveKit Cloud)
            "turn_detection": turns,
            "endpointing": await asyncio.to_thread(pacing.options),
            "interruption": {"mode": "vad"},
            # Speculative replies would write half-heard turns into the brain's history.
            "preemptive_generation": {"enabled": False},
        },
    )
    pacing.attach(session)
    # Voice, speed, the speech gate, the speech vocabulary and the speaker check as Settings and the database have them
    # now, so a change there (or a new voiceprint) needs no restart
    ctx.proc.userdata["tts"].use(config.tts_voice(), config.tts_speed())
    mode = config.speech_gate()
    stt.gate.use(mode, config.speech_gate_save_audio(mode))
    stt.use_vocabulary(await asyncio.to_thread(vocabulary.load), config.STT_PROMPT)
    stt.take_clips()  # none of the last conversation's
    stt.speaker.use(config.speaker_check(), await asyncio.to_thread(voiceprints.load), config.speaker_threshold())
    speaker_ready = asyncio.create_task(asyncio.to_thread(stt.speaker.warm_up))  # while the room connects
    voice = VoiceSession(make_brain(), talk, stt=stt, turns=turns, pacing=pacing,
                         router=Router(config.VOICE_QUICK_MODEL) if config.VOICE_QUICK_MODEL else None,
                         moment=Moment() if config.PROACTIVE_JUDGE else None,  # when to bring things up
                         timings=timing.Timings(written=pacing.learn),  # each turn's stages, in logs/voice-turns.jsonl
                         push=_to_phone)  # what isn't said goes to the phone
    voice.open()

    @session.on("user_input_transcribed")
    def heard(ev) -> None:
        if ev.is_final:
            voice.transcribed(ev.transcript)

    @session.on("agent_state_changed")
    def speaking(ev) -> None:
        stt.speaker.jarvis_speaking(ev.new_state == "speaking")  # a short clip just after it is likely a reply
        barge.replying(ev.new_state)

    @session.on("conversation_item_added")
    def added(ev) -> None:
        if ev.item.role == "assistant":
            voice.spoke(ev.item.text_content or "", ev.item.metrics, ev.item.interrupted)

    ctx.add_shutdown_callback(voice.close)
    warmup = asyncio.create_task(voice.brain.start())  # start the CLI while the room connects
    greeting = asyncio.create_task(voice.greeting())
    await session.start(
        room=ctx.room,
        agent=JarvisAgent(voice),
        room_options=denoise.room_options(ctx.proc.userdata["denoise"]),  # the mic through DeepFilterNet
    )
    quiet.follow(session, ctx.room)  # speaker off: replies as text only, with no speech made
    quiet.apply(session, (await ctx.wait_for_participant()).attributes)  # as the user joined, before the greeting
    hello = await greeting
    voice.floor.said(hello)  # it may end by asking about a task's request
    await session.say(hello)
    voice.start(session)
    await asyncio.gather(warmup, speaker_ready)


async def _to_phone(text: str) -> None:
    """An announcement the conversation didn't take, as a push to the phone."""
    if config.PUSH_ENABLED:
        await push.send_push("JARVIS", text, "/", "voice-" + hashlib.sha256(text.encode()).hexdigest()[:16])


def _child_memory() -> None:
    """Have the worker's child processes hand freed memory back to macOS (JARVIS_VOICE_MALLOC_LARGE_CACHE). Its malloc
    otherwise keeps large freed blocks cached, still counted against the process: Kokoro's buffers for one long
    sentence kept the speech process 1 GB bigger. Read when each process starts, so it's set before the worker starts
    any; this process has its own malloc already, and is small."""
    if not config.VOICE_MALLOC_LARGE_CACHE:
        os.environ.setdefault("MallocLargeCache", "0")


if __name__ == "__main__":
    _child_memory()
    ram.watch(config.VOICE_MEMORY_LOG_MINUTES)  # one line in logs/agent.log for all the voice processes
    cli.run_app(server)
