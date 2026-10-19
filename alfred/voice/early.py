"""Whisper early: start on an utterance at a short silence, before the VAD ends its clip.

The VAD ends a clip after VAD_MIN_SILENCE (0.55 s) of silence, then Whisper takes ~1.1 s on the MacBook Air, and the
turn ends once Whisper has the words (alfred.voice.turn.Transcribed). At WHISPER_EARLY (0.25 s) of silence this starts
Whisper on the utterance so far: if nothing more is said, the clip uses those words, ~0.3 s sooner. If speech goes on,
that hearing is dropped (its time on the GPU is lost) and starts again at the next pause.

Only the STT's own VAD stream does this, through the StreamAdapter it's given (`adapter`); the session's other VAD
streams (turn-taking, barge-in) are left alone. A clip is matched to its early hearing by the audio where its speech
starts, which the VAD's start-of-speech and end-of-speech buffers share.
"""

import asyncio

from livekit.agents import stt, vad

from alfred.config import VAD_MIN_SILENCE, WHISPER_EARLY

ONSET_SECONDS = 0.1  # audio up to where speech was heard to start, matching a clip to its early hearing


def adapter(whisper, vad_: vad.VAD, after: float = WHISPER_EARLY) -> stt.STT:
    """The session's STT: `whisper` on `vad_`'s clips, heard early at `after` seconds of silence. Off (0, or not
    under VAD_MIN_SILENCE) it's `whisper` itself, which LiveKit adapts the same way minus the early start."""
    if not 0 < after < VAD_MIN_SILENCE:
        return whisper
    return stt.StreamAdapter(stt=whisper, vad=EarlyVAD(vad_, whisper, after))


class EarlyVAD(vad.VAD):
    """Wraps the VAD, passing every event through and starting `whisper` early on each utterance."""

    def __init__(self, inner: vad.VAD, whisper, after: float) -> None:
        super().__init__(capabilities=inner.capabilities)
        self._inner, self._whisper, self._after = inner, whisper, after

    @property
    def model(self) -> str:
        return self._inner.model

    @property
    def provider(self) -> str:
        return self._inner.provider

    def stream(self) -> "EarlyStream":
        return EarlyStream(self, self._inner.stream(), self._whisper, self._after)


class EarlyStream(vad.VADStream):
    def __init__(self, vad_: EarlyVAD, inner: vad.VADStream, whisper, after: float) -> None:
        self._inner, self._whisper, self._after = inner, whisper, after
        self._frames: list | None = None  # this utterance's audio so far, while speaking
        self._onset: tuple[int, bytes] = (0, b"")
        self._silence = 0.0
        self._early = False  # Whisper started on it since speech last went on
        super().__init__(vad_)  # starts _main_task

    async def _main_task(self) -> None:
        forward = asyncio.create_task(self._forward())
        try:
            async for item in self._input_ch:
                if isinstance(item, self._FlushSentinel):
                    self._inner.flush()
                else:
                    self._inner.push_frame(item)
            self._inner.end_input()
            await forward
        finally:
            forward.cancel()
            await self._inner.aclose()

    async def _forward(self) -> None:
        async for ev in self._inner:
            self.see(ev)
            self._event_ch.send_nowait(ev)

    def see(self, ev: vad.VADEvent) -> None:
        if ev.type == vad.VADEventType.START_OF_SPEECH:
            frame = ev.frames[0]  # the speech buffer so far: padding, then the speech that started the utterance
            data = frame.data.tobytes()
            size = min(len(data), round(ONSET_SECONDS * frame.sample_rate) * 2 * frame.num_channels)
            cut = len(data) - size
            self._onset = (cut, data[cut:])
            self._frames, self._silence, self._early = [frame], 0.0, False
        elif ev.type == vad.VADEventType.INFERENCE_DONE and self._frames is not None and ev.speaking:
            self._frames.append(ev.frames[0])  # the window just judged, as the VAD adds it to the speech buffer
            silence = ev.raw_accumulated_silence
            if self._early and silence < self._silence:  # speech went on: the clip will be longer
                self._whisper.drop_early(self._onset)
                self._early = False
            self._silence = silence
            if not self._early and silence >= self._after:
                self._whisper.hear_early(self._onset, list(self._frames))
                self._early = True
        elif ev.type == vad.VADEventType.END_OF_SPEECH:
            self._frames = None  # the clip goes to Whisper now, which takes its early hearing
