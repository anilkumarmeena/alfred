"""Speaker off: Alfred still listens but replies only as text in the transcript (for meetings, calls, crowds).

With audio output off, LiveKit skips TTS and sends reply text straight to captions. The switch is the user's
participant attribute SPEAKER ("off"/"on"), set via /api/token so a session can start silent (greeting included) and
changed mid-session. Announcements and task questions follow it too.
"""

import logging

log = logging.getLogger("alfred.voice.quiet")

SPEAKER = "alfred.speaker"
# What the phone apps' native sessions set (alfred-app, from before the rename and since, so one build works with
# either server). The token sets SPEAKER at the start; the app's switch sets this one after, so it wins.
APP_SPEAKER = "jarvis.speaker"


def speaker_on(attributes: dict[str, str]) -> bool:
    return attributes.get(APP_SPEAKER, attributes.get(SPEAKER)) != "off"


def apply(session, attributes: dict[str, str]) -> None:
    """Turn Alfred's voice on or off per the user's attributes."""
    on = speaker_on(attributes)
    if on != session.output.audio_enabled:
        session.output.set_audio_enabled(on)
        log.info("speaker %s: replies are %s", "on" if on else "off", "spoken" if on else "text only")


def follow(session, room) -> None:
    """Track the user's speaker switch for the room's lifetime. The initial value is applied in agent.entrypoint."""

    @room.on("participant_attributes_changed")
    def changed(attributes: dict[str, str], participant) -> None:
        if SPEAKER in attributes or APP_SPEAKER in attributes:  # only the user sets them; the agent's are lk.agent.*
            apply(session, participant.attributes)
