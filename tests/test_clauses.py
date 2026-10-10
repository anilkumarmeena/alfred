"""Tests for voice.clauses: long sentences split at pauses; short ones, numbers, abbreviations kept whole."""

import asyncio
import json

from jarvis.voice import clauses, speech
from jarvis.voice.clauses import ClauseTokenizer

WEATHER = ("Sure, the weather in London today is mild and cloudy, "
           "with highs around eighteen degrees and a light breeze from the west.")


def pieces(text: str) -> list[str]:
    return ClauseTokenizer().tokenize(text)


def test_a_short_sentence_stays_whole():
    assert pieces("Sure, I'll remind you at five, after the call.") == ["Sure, I'll remind you at five, after the call."]
    assert pieces("Okay. Done.") == ["Okay. Done."]


def test_a_long_sentence_is_cut_at_its_pauses():
    assert pieces(WEATHER) == ["Sure, the weather in London today is mild and cloudy,",
                               "with highs around eighteen degrees and a light breeze from the west."]
    assert pieces("The build passed; all forty two tests are green, and the branch is ready to merge whenever you "
                  "want it.") == ["The build passed; all forty two tests are green,",
                                  "and the branch is ready to merge whenever you want it."]
    assert pieces("There's one thing left to do before the release goes out: update the changelog and tag the "
                  "commit.") == ["There's one thing left to do before the release goes out:",
                                 "update the changelog and tag the commit."]


def test_dashes_are_pauses_but_hyphens_are_not():
    assert pieces("The build passed — all forty two tests are green and the branch is ready to merge.") == [
        "The build passed — all forty two tests are green and the branch is ready to merge."]  # first piece too short
    assert pieces("Your well-known colleague from the London office called twice — she wants to move the review to "
                  "Friday.") == ["Your well-known colleague from the London office called twice —",
                                 "she wants to move the review to Friday."]
    assert pieces("Your colleague from the big London office called twice this morning - she wants to move the "
                  "review.") == ["Your colleague from the big London office called twice this morning -",
                                 "she wants to move the review."]


def test_pieces_are_joined_up_to_the_limit_and_never_left_tiny():
    text = ("I checked your calendar, and you have three meetings today, the first at nine with Priya, the second "
            "at noon, and the last one at four.")
    assert pieces(text) == ["I checked your calendar, and you have three meetings today,",
                            "the first at nine with Priya, the second at noon,", "and the last one at four."]
    for piece in pieces(text) + pieces(WEATHER):
        assert clauses.MIN_WORDS <= len(piece.split()) <= clauses.MAX_WORDS
    # a short opening goes with what follows; a short ending with what comes before
    assert pieces("Well, the meeting with the design team about the new onboarding flow ran long, as usual.") == [
        "Well, the meeting with the design team about the new onboarding flow ran long, as usual."]


def test_no_cut_inside_numbers_dates_or_abbreviations():
    assert pieces("On October 6, 2026, we shipped 1,000 units at 3:30 in the afternoon to stores in about 10–20 cities "
                  "across the country.") == [
        "On October 6, 2026,",
        "we shipped 1,000 units at 3:30 in the afternoon to stores in about 10–20 cities across the country."]
    assert pieces("Bring something small for the long trip tomorrow, e.g., apples, and ask Martin Luther King, Jr. "
                  "about it.") == ["Bring something small for the long trip tomorrow, e.g., apples,",
                                   "and ask Martin Luther King, Jr. about it."]
    assert pieces("Dr. Smith said the U.S. results look good overall, and the European ones should land next week "
                  "too.") == ["Dr. Smith said the U.S. results look good overall,",
                              "and the European ones should land next week too."]


def test_a_long_stretch_without_pauses_stays_whole():
    text = "The quick brown fox jumps over the lazy dog and then runs all the way back home again."
    assert pieces(text) == [text]


def test_the_pieces_cover_the_text_end_to_end():
    text = "Hello there. " + WEATHER + "  And one more short thing."
    split = clauses.split(text)
    assert "".join(piece for piece, _, _ in split) == text
    assert all(text[start:end] == piece for piece, start, end in split)


async def _stream(chunks: list[str]) -> list[str]:
    stream = ClauseTokenizer().stream()
    for chunk in chunks:
        stream.push_text(chunk)
    stream.end_input()
    return [ev.token.strip() async for ev in stream]


def test_streamed_word_by_word_it_gives_the_same_pieces():
    text = WEATHER + " " + ("I checked your calendar, and you have three meetings today, the first at nine with "
                            "Priya, the second at noon, and the last one at four.") + " Anything else?"
    words = [w + " " for w in text.split()]
    assert asyncio.run(_stream(words)) == pieces(text)


class FakeExecutor:
    def __init__(self) -> None:
        self.texts: list[str] = []

    async def do_inference(self, method: str, data: bytes) -> bytes:
        self.texts.append(json.loads(data)["text"])
        return b"\x00\x00" * 240


def test_the_session_tts_renders_each_piece_on_its_own():
    async def speak() -> list[str]:
        executor = FakeExecutor()
        adapter = speech.streaming(speech.KokoroTTS(executor=executor))
        async with adapter.stream() as stream:
            for word in WEATHER.split():
                stream.push_text(word + " ")
            stream.end_input()
            async for _ in stream:
                pass
        await adapter.aclose()
        return executor.texts

    assert asyncio.run(speak()) == pieces(WEATHER)
