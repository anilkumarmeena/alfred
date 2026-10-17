"""How each user turn ends and was heard: who spoke, how sure Whisper was, and each speaker's pace.

A turn ends on silence alone (TURN_SILENCE after the last speech, jarvis.voice.agent) once Whisper has every clip's
words (Transcribed); nothing judges the words. Pace is learned per speaker (voiceprint, else owner) and stored in
settings: running shares of low-confidence turns and of turns the user went on from before hearing the reply, plus
speaking rate, flag "effortful" speakers; the brain then gets hints asking for plainer replies.
"""

import asyncio
import json
import logging
import time
from dataclasses import asdict, dataclass, fields

from jarvis.config import WHISPER_UNSURE_LOGPROB
from jarvis.storage import profile

log = logging.getLogger("jarvis.voice.turn")

OWNER = "owner"  # voiceprints' name for the primary user


class Transcribed:
    """LiveKit turn detector that reads no words: it holds the turn's end until Whisper has the clips in flight.

    LiveKit ends a turn TURN_SILENCE after speech stops as soon as it has any of the turn's text. A pause longer than
    the VAD's silence splits an utterance into clips, and Whisper's text for the last one comes later than that, so
    its words would start a turn of their own. Always "finished" (1.0): the silence alone sets the wait."""

    model, provider = "whisper-transcribed", "jarvis"
    SETTLE = 0.05  # for a clip whose speech just ended to reach Whisper
    FRESH = 0.1  # a clip finished this recently is the transcript that started this check
    MOST = 10.0  # never hold a turn longer than this for Whisper
    POLL = 0.02  # a held turn ends this soon after Whisper finishes

    def __init__(self, stt, clock=time.monotonic) -> None:
        self.stt = stt  # WhisperSTT: `hearing` clips in flight, `heard_at` when the last one finished
        self.clock = clock

    async def unlikely_threshold(self, language) -> None:
        return None

    async def supports_language(self, language) -> bool:
        return True

    async def predict_end_of_turn(self, chat_ctx, *, timeout: float | None = None) -> float:
        if self.clock() - self.stt.heard_at > self.FRESH:
            await asyncio.sleep(self.SETTLE)
        deadline = self.clock() + self.MOST
        while self.stt.hearing and self.clock() < deadline:
            await asyncio.sleep(self.POLL)
        return 1.0

AVERAGE = 0.1  # EMA weight
SETTLED = 10  # turns before shares count
# Effortful if this share of turns is low-confidence or resumed, or rate is below this (words/s)
EFFORT_UNSURE, EFFORT_WENT_ON, EFFORT_RATE = 0.25, 0.3, 1.6


@dataclass
class Pace:
    turns: int = 0
    unsure: float = 0.0  # EMA share of low-confidence turns
    went_on: float = 0.0  # EMA share of turns resumed before the reply
    rate: float = 0.0  # EMA words/s; 0 until measured

    def learn(self, record: dict) -> None:
        """Update from one finished timing record."""
        ended = record.get("ended")
        if ended not in ("spoken", "interrupted", "unspoken"):
            return  # closed: nothing to learn
        self.turns += 1
        self.went_on += AVERAGE * ((ended == "unspoken") - self.went_on)
        if (logprob := record.get("logprob")) is not None:
            self.unsure += AVERAGE * ((logprob < WHISPER_UNSURE_LOGPROB) - self.unsure)
        if (speech := record.get("speech") or 0) >= 1.0 and record.get("words"):
            rate = record["words"] / speech
            self.rate = rate if not self.rate else self.rate + AVERAGE * (rate - self.rate)

    @property
    def effortful(self) -> bool:
        """True if talking to JARVIS seems to take this speaker effort."""
        return self.turns >= SETTLED and (self.unsure >= EFFORT_UNSURE or self.went_on >= EFFORT_WENT_ON
                                          or 0 < self.rate < EFFORT_RATE)


class Paces:
    """Per-speaker Pace, stored in settings as voice_pace:<speaker>."""

    def __init__(self, get=None, put=None) -> None:
        if get is None or put is None:
            from jarvis.state import settings
            get, put = get or settings.get, put or settings.put
        self._get, self._put = get, put
        self._known: dict[str, Pace] = {}

    def __getitem__(self, speaker: str) -> Pace:
        if speaker not in self._known:
            pace = Pace()
            try:
                saved = json.loads(self._get(f"voice_pace:{speaker}") or "{}")
                pace = Pace(**{f.name: saved[f.name] for f in fields(Pace) if f.name in saved})
            except Exception as e:
                log.warning("couldn't read %s's pace, starting afresh: %s", speaker, e)
            self._known[speaker] = pace
        return self._known[speaker]

    def learn(self, speaker: str, record: dict) -> None:
        pace = self[speaker]
        pace.learn(record)
        try:
            self._put(f"voice_pace:{speaker}", json.dumps({k: round(v, 3) for k, v in asdict(pace).items()}))
        except Exception as e:
            log.warning("couldn't save %s's pace: %s", speaker, e)


class Pacing:
    """The call's speakers' paces, following whoever is speaking."""

    def __init__(self, paces: Paces | None = None) -> None:
        self.paces = paces or Paces()
        self.speaker = OWNER

    def use(self, speaker: str) -> None:
        """`speaker` said the latest turn."""
        self.speaker = speaker

    def learn(self, record: dict) -> None:
        """Learn from a finished timing record."""
        self.paces.learn(record.get("speaker") or OWNER, record)

    def hints(self, logprob: float | None, goes_on: str | None) -> list[str]:
        """Notes for the brain on how this turn was heard."""
        out = []
        if goes_on:
            out.append(f"[This goes on from their last message (\"{goes_on}\"): they carried on before hearing your "
                       f"reply to it, so answer the two together as one thought.]")
        if logprob is not None and logprob < WHISPER_UNSURE_LOGPROB:
            out.append("[Speech recognition was unsure of these words.]")
        if self.paces[self.speaker].effortful:
            out.append(f"[Keep it plain: talking to you seems to take {profile.name() if self.speaker == OWNER else 'them'} "
                       f"some effort.]")
        return out


def clips_heard(clips: list[dict]) -> tuple[str, float | None]:
    """(majority speaker or OWNER, word-weighted mean logprob or None) from WhisperSTT.take_clips()."""
    speakers = [c["speaker"] for c in clips if c.get("speaker")]
    speaker = max(set(speakers), key=speakers.count) if speakers else OWNER
    scored = [(c["logprob"], max(c.get("words", 1), 1)) for c in clips if c.get("logprob") is not None]
    logprob = sum(lp * n for lp, n in scored) / sum(n for _, n in scored) if scored else None
    return speaker, logprob
