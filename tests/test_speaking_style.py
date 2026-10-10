"""Tests for how the voice prompt (jarvis.brain.PERSONA) tells JARVIS to speak."""

from jarvis.brain import persona

PERSONA = persona.persona()


def line(needle: str) -> str:
    return next(l for l in PERSONA.splitlines() if needle in l)


def test_one_filler_phrase_before_the_first_tool_only():
    filler = line("Checking your calendar")
    assert "first tool call" in filler and "only one" in filler and "later tool calls" in filler
    assert "Every time you are about to use a tool" not in PERSONA


def test_the_lead_in_fits_the_job():
    filler = line("Checking your calendar")
    assert "answer straight away needs no lead-in" in filler  # a quick question gets none
    assert "how long it will take" in filler and "never the same as your last one" in filler


def test_results_after_tools_dont_repeat_the_reply():
    assert "Don't repeat or rephrase what you already said" in line("After tools")


def test_status_answers_keep_to_three_items():
    status = line("how things stand")
    assert "at most the three most important" in status and "only if there is more" in status


def test_tasks_are_named_by_what_they_do_without_mechanics():
    tasks = line("Refer to tasks")
    assert "by what they do" in tasks and "never by number" in tasks
    for word in ("queues", "slots", "branches", "worktrees", "task numbers"):
        assert word in tasks
    assert "unless the user asks" in tasks
    assert "by number and title" not in PERSONA
