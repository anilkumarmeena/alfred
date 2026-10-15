"""Tests for jarvis.voice.cutoff: where a talked-over reply was cut off, remark or point, and what the brain is told."""

import pytest

from jarvis.voice import cutoff
from jarvis.voice.cutoff import POINT, REMARK, Cut, Reply

STORY = "The meeting moved to Friday. Priya can't make it, so I've asked Sam to cover. The room is booked too."


def reply(*sent: str, heard: str | None = None, interrupted: bool = True, finished: bool = True) -> Reply:
    r = Reply(list(sent), finished)
    if heard is not None:
        r.spoke(heard, interrupted)
    return r


# ── Where it was cut off ──────────────────────────────────────


def test_the_unheard_part_starts_after_the_last_word_heard():
    assert cutoff.unheard(STORY, "The meeting moved to Friday. Priya can't") == (
        "make it, so I've asked Sam to cover. The room is booked too.")


def test_spacing_punctuation_and_case_in_livekits_transcript_dont_throw_the_match_off():
    assert cutoff.unheard(STORY, "the meeting moved to friday priya cant make it so") == (
        "I've asked Sam to cover. The room is booked too.")


def test_a_word_cut_partway_counts_as_unheard():
    assert cutoff.unheard(STORY, "The meeting moved to Fri") == "Friday. Priya can't make it, so I've asked Sam to " \
                                                                  "cover. The room is booked too."


def test_nothing_heard_leaves_the_whole_reply_unheard():
    assert cutoff.unheard(STORY, "") == STORY


def test_a_transcript_that_strays_from_the_reply_keeps_what_matched():
    assert cutoff.unheard("Rain at noon, then sun.", "Rain at night") == "noon, then sun."


def test_unspaced_scripts_are_cut_where_the_transcript_ends():
    assert cutoff.unheard("会議は金曜日に移りました。部屋も予約済みです。", "会議は金曜日に移りました") == "部屋も予約済みです。"


def test_a_reply_heard_to_its_end_has_no_cut():
    assert reply(STORY, heard=STORY, interrupted=False).cut() is None


def test_a_reply_talked_over_partway_knows_what_was_and_wasnt_heard():
    cut = reply("The meeting moved to Friday. ", "Priya can't make it.", heard="The meeting moved to Friday.").cut()
    assert cut == Cut("The meeting moved to Friday.", "Priya can't make it.", unfinished=False)


def test_a_reply_stopped_before_a_word_played_was_heard_not_at_all():
    assert reply(STORY, finished=False).cut() == Cut("", STORY, unfinished=True)  # LiveKit reports nothing then


def test_a_reply_that_ran_to_its_end_with_no_report_is_taken_as_heard():
    assert reply(STORY).cut() is None


def test_a_reply_cut_off_on_its_last_word_counts_as_heard():
    assert reply(STORY, heard=STORY + " ").cut() is None


def test_a_reply_stopped_before_the_brain_finished_it_says_so_even_with_nothing_left_unheard():
    assert reply("Let me check.", heard="Let me check.", finished=False).cut() == Cut("Let me check.", "", True)


def test_private_notes_never_count_as_unheard():
    cut = reply("Done. <private>task 12 merged</private>It's on main now.", heard="Done.").cut()
    assert cut is not None and cut.unheard == "It's on main now."


def test_only_livekits_first_report_is_the_replys():
    r = reply(STORY, heard="The meeting")
    r.spoke("Hello again.", False)  # something said after it
    assert r.cut() is not None and r.cut().heard == "The meeting"


def test_a_report_of_speech_that_began_before_the_reply_isnt_its():
    r = Reply([STORY], finished=False, began=100.0)
    r.spoke("Hello Anil.", True, started=99.0)  # the greeting, cut off
    assert r.cut() == Cut("", STORY, unfinished=True)


def test_once_the_users_next_turn_has_ended_later_reports_arent_the_replys():
    r = Reply([STORY], finished=False)
    r.over = True
    r.spoke("Task 12 is done.", True)  # an announcement after it
    assert r.cut() == Cut("", STORY, unfinished=True)


def test_a_reply_with_nothing_to_say_has_no_cut():
    assert reply(" ", "<private>noted</private>").cut() is None


# ── Remark or point ───────────────────────────────────────────

CUT = Cut("The meeting moved to Friday.", "Priya can't make it.", False)
ASKED = Cut("Shall I tell Sam? He's", "free all day.", False)


@pytest.mark.parametrize("words", ["Thank you.", "thanks", "Okay.", "Hmm.", "Right, right.", "Yes yes", "Got it.",
                                   "Oh wow!", "Thank you so much, JARVIS.", "Okay, thanks.", "That's great.", "uh-huh"])
def test_plain_acknowledgements_are_remarks(words):
    assert cutoff.obvious(CUT, words) == REMARK


@pytest.mark.parametrize("words", ["What about Monday?", "Thanks, can you email Sam?", "Really?"])
def test_questions_are_points(words):
    assert cutoff.obvious(CUT, words) == POINT


@pytest.mark.parametrize("words", ["Stop.", "No.", "Wait, wrong meeting", "Thanks, now book a taxi", "Merci", ""])
def test_anything_less_plain_is_left_to_the_brain(words):
    assert cutoff.obvious(CUT, words) is None


def test_a_yes_after_jarvis_asked_something_may_answer_it_so_the_brain_decides():
    assert cutoff.obvious(ASKED, "Yes.") is None


# ── What the brain is told ────────────────────────────────────


def test_after_a_remark_the_brain_acknowledges_and_recaps_the_part_not_heard():
    where, todo = cutoff.framing(CUT, REMARK)
    assert where == ('[They spoke over your last reply and heard up to "The meeting moved to Friday.". '
                     'They didn\'t hear: "Priya can\'t make it.".]')
    assert "passing remark" in todo and "word or two" in todo and "one short sentence" in todo


def test_after_a_new_point_the_brain_answers_it_and_drops_the_rest():
    where, todo = cutoff.framing(CUT, POINT)
    assert "Priya can't make it." in where
    assert "new point: answer that, and drop the rest of your last reply unless they ask for it" in todo
    assert "offer it in a few words after" in todo


def test_with_no_verdict_the_brain_is_given_both_ways():
    _, todo = cutoff.framing(CUT, None)
    assert todo.startswith("[If what they said is only a passing remark") and "Otherwise answer it" in todo


def test_the_brain_hears_of_a_reply_stopped_unfinished_or_not_heard_at_all():
    where, _ = cutoff.framing(Cut("", "", True), REMARK)
    assert where == "[They spoke over your last reply and heard none of it. It was stopped before you'd finished it.]"


def test_long_replies_are_quoted_from_the_cut():
    heard = " ".join(f"w{i}" for i in range(40))
    where, _ = cutoff.framing(Cut(heard, "x " * 1000, False), POINT)
    assert 'heard up to "…w15 w16' in where and "w14 " not in where
    assert len(where) < 1400 and '…".' in where
