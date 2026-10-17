"""Tests for state.conversation: fresh state per conversation, visible to its threads, and its profile."""

import asyncio
import dataclasses
import threading

from jarvis.state import conversation
from jarvis.storage import profile


def test_each_conversation_has_its_own_state_and_runs_as_its_profile():
    conversation.current().heard.turn("Approve it.")  # an earlier conversation's words

    async def call() -> tuple[conversation.Conversation, list]:
        talk = conversation.begin(dataclasses.replace(profile.current(), name="Robin"))
        assert conversation.current() is talk and talk.heard.said == "" and talk.offer.open is None
        assert profile.name() == "Robin"
        seen: list = []
        elsewhere = threading.Thread(target=lambda: seen.append(conversation.current()))  # a callback from elsewhere
        elsewhere.start()
        elsewhere.join()
        assert await asyncio.create_task(asyncio.sleep(0, conversation.current())) is talk
        conversation.end(talk)
        return talk, seen

    talk, seen = asyncio.run(call())
    assert seen == [talk]  # a voice process holds one conversation at a time
    assert conversation.current() is not talk and profile.name() == "Sam"  # over, and its profile with it
