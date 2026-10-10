"""Speaker off (jarvis.voice.quiet): the user's switch turns the session's audio output, and so text-to-speech, on and
off, from the token they joined with and as they change it. Run with: make test"""

import asyncio
import json

from livekit import api

from jarvis import server
from jarvis.voice import quiet
from tests.test_web_build import Request


class Output:
    def __init__(self) -> None:
        self.audio_enabled = True
        self.calls: list[bool] = []

    def set_audio_enabled(self, on: bool) -> None:
        self.audio_enabled = on
        self.calls.append(on)


class Session:
    def __init__(self) -> None:
        self.output = Output()


class Room:
    """Just the event registration of livekit.rtc.Room."""

    def __init__(self) -> None:
        self.handlers: dict[str, list] = {}

    def on(self, event: str):
        def register(fn):
            self.handlers.setdefault(event, []).append(fn)
            return fn
        return register

    def emit(self, event: str, *args) -> None:
        for fn in self.handlers.get(event, []):
            fn(*args)


class Participant:
    def __init__(self, **attributes: str) -> None:
        self.attributes = attributes


def test_speaker_is_on_unless_switched_off():
    assert quiet.speaker_on({})  # a page or app from before the switch
    assert quiet.speaker_on({quiet.SPEAKER: "on"})
    assert not quiet.speaker_on({quiet.SPEAKER: "off"})


def test_apply_turns_the_voice_off_and_on_once():
    session = Session()
    quiet.apply(session, {quiet.SPEAKER: "off"})
    quiet.apply(session, {quiet.SPEAKER: "off"})
    assert session.output.audio_enabled is False
    quiet.apply(session, {quiet.SPEAKER: "on"})
    assert session.output.calls == [False, True]


def test_follow_tracks_the_users_switch_and_ignores_other_attributes():
    session, room = Session(), Room()
    quiet.follow(session, room)
    user = Participant(**{quiet.SPEAKER: "off"})
    room.emit("participant_attributes_changed", {quiet.SPEAKER: "off"}, user)
    assert session.output.audio_enabled is False
    agent = Participant(**{"lk.agent.state": "speaking"})  # the agent's own state changes leave the switch alone
    room.emit("participant_attributes_changed", {"lk.agent.state": "speaking"}, agent)
    assert session.output.audio_enabled is False
    user.attributes[quiet.SPEAKER] = "on"
    room.emit("participant_attributes_changed", {quiet.SPEAKER: "on"}, user)
    assert session.output.calls == [False, True]


def _claims(body: dict, monkeypatch, tmp_path):
    secret = "a-secret-long-enough-for-the-jwt-signer-0123456789"
    monkeypatch.setenv("JARVIS_PASSCODE", "123456")  # no login yet: the first-setup passcode
    monkeypatch.setattr(server, "WEB_DIR", tmp_path)
    monkeypatch.setenv("LIVEKIT_API_KEY", "key")
    monkeypatch.setenv("LIVEKIT_API_SECRET", secret)
    server._failures.clear()
    r = asyncio.run(server.token(Request({"passcode": "123456", "device": "browser"} | body)))
    assert r.status == 200
    return api.TokenVerifier("key", secret).verify(json.loads(r.text)["token"])


def test_token_carries_the_speaker_switch_and_lets_the_page_change_it(monkeypatch, tmp_path):
    off = _claims({"speaker": "off"}, monkeypatch, tmp_path)
    assert off.attributes == {quiet.SPEAKER: "off"}
    assert off.video.can_update_own_metadata
    assert _claims({}, monkeypatch, tmp_path).attributes == {quiet.SPEAKER: "on"}
