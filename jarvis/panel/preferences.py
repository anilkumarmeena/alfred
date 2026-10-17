"""Control panel actions: Preferences (name, time zone, voice, task limit) and voiceprints."""

import asyncio
import base64

from jarvis import config
from jarvis.panel.base import GIT_TIMEOUT, HIDDEN, LOG_LINES, SERVICE_NAMES, _id, _text, action, log  # noqa: F401
from jarvis.state import settings, voiceprints
from jarvis.voice import speaker

# ── Preferences: name, time zone, voice, task limit ─────────────


@action
async def preferences(_: dict) -> dict:
    return await asyncio.to_thread(settings.preferences)


@action
async def preference_save(body: dict) -> dict:
    """Set one preference {"key", "value"}; empty resets to default."""
    settings.set_preference(_text(body, "key", 40), _text(body, "value", 100))
    return await asyncio.to_thread(settings.preferences)


# ── Voiceprints (speaker check) ─────────────────────────────────

MAX_ENROLL_SECONDS = 90  # max recording length accepted


def _voiceprints() -> dict:
    """Owner voiceprint (or None), model presence, and the enrollment passage and duration."""
    owner = next((p.described() for p in voiceprints.every() if p.name == voiceprints.OWNER), None)
    return {"owner": owner, "model": config.SPEAKER_MODEL.is_file(), "passage": speaker.PASSAGE,
            "seconds": speaker.RECORD_SECONDS}


@action
async def voiceprint(_: dict) -> dict:
    return await asyncio.to_thread(_voiceprints)


@action
async def voiceprint_enroll(body: dict) -> dict:
    """Enroll from {"audio": base64 16 kHz mono s16 PCM} (~30 s of reading). Replaces the voiceprint; raises
    ValueError if one can't be made."""
    try:
        pcm = base64.b64decode(str(body.get("audio", "")), validate=True)
    except ValueError:
        raise ValueError("The recording didn't arrive whole. Try again.") from None
    if len(pcm) > MAX_ENROLL_SECONDS * speaker.RATE * 2:
        raise ValueError(f"That recording is too long: keep it under {MAX_ENROLL_SECONDS} seconds.")
    await asyncio.to_thread(speaker.enroll, speaker.to_float(pcm), via="panel")
    return await asyncio.to_thread(_voiceprints)


@action
async def voiceprint_remove(body: dict) -> dict:
    """Forget a voiceprint {"name"}; defaults to the owner's."""
    await asyncio.to_thread(voiceprints.remove, _text(body, "name", 40) or voiceprints.OWNER)
    return await asyncio.to_thread(_voiceprints)
