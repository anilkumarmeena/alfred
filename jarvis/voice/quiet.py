"""Speaker off (jarvis.voice.agent): for a crowd, a meeting or a call, JARVIS still hears the user, or reads what they
type, and replies only in the chat transcript. No speech is made at all: with the session's audio output off, LiveKit
skips text-to-speech and sends the reply's text straight to the captions.

The switch is the user's participant attribute SPEAKER, "off" or "on". The page or the app asks for it in /api/token
(jarvis.server), so a session can start silent, greeting included, and changes it during the session; announcements and
tasks' questions follow it like replies.
"""

import logging

log = logging.getLogger("jarvis.voice.quiet")

SPEAKER = "jarvis.speaker"


def speaker_on(attributes: dict[str, str]) -> bool:
    return attributes.get(SPEAKER) != "off"


def apply(session, attributes: dict[str, str]) -> None:
    """Turn JARVIS's voice on or off as the user's attributes ask."""
    on = speaker_on(attributes)
    if on != session.output.audio_enabled:
        session.output.set_audio_enabled(on)
        log.info("speaker %s: replies are %s", "on" if on else "off", "spoken" if on else "text only")


def follow(session, room) -> None:
    """Keep the session's voice following the user's speaker switch for as long as the room lasts. The switch they
    joined with is applied once they're in (agent.entrypoint), before the greeting."""

    @room.on("participant_attributes_changed")
    def changed(attributes: dict[str, str], participant) -> None:
        if SPEAKER in attributes:  # only the user sets it: the agent's own attributes are lk.agent.*
            apply(session, participant.attributes)
