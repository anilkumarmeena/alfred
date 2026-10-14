"""Tests for jarvis.tasks.learning: when the review runs, reading its suggestions, and how the runner keeps them."""

import asyncio
import json
import os
import time
from datetime import date

import pytest
from test_in_place import env  # noqa: F401  (a fixture)

from conftest import use_profile
from jarvis import tools
from jarvis.brain import retry
from jarvis.core import profile
from jarvis.core.database import edits
from jarvis.skills import home
from jarvis.state import activity, memory, todo
from jarvis.tasks import announce, api, data_edits, learning, runner

TODAY = f"{date.today():%Y-%m-%d}"
MEMORY = "## User\n- Alex prefers short answers (2026-09-01)\n\n## Environment\n- Alex's laptop is a MacBook Air (2026-09-02)\n"
CHATTY = ("I've switched from VS Code to Zed for all my editing now, and I like the metric system in every answer, "
          "so please keep that in mind going forward")


@pytest.fixture
def data(env, monkeypatch):
    """Own memory, conversation log and review state on the env fixture's task database."""
    use_profile(monkeypatch, name="Alex")
    use_profile(monkeypatch, name="Alex")
    profile.current().memory_file.write_text(MEMORY)
    return env


class Clock:
    def __init__(self) -> None:
        self.now = time.time()

    def __call__(self) -> float:
        return self.now


class Reviewer:
    """Fake model call: answers `reply` (or raises it) and records each prompt."""

    def __init__(self, reply="") -> None:
        self.reply, self.prompts = reply, []

    async def __call__(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


def suggesting(*suggestions: dict) -> Reviewer:
    return Reviewer("```json\n" + json.dumps({"suggestions": list(suggestions)}) + "\n```")


def watcher(data, reviewer: Reviewer, saved: list | None = None) -> tuple[learning.Learning, Clock]:
    """A Learning on a test clock; what it would save goes in `saved`."""
    clock = Clock()

    async def save(found: list[dict], about: str) -> None:
        saved.append((found, about))

    return learning.Learning(save, reviewer, data["tmp"] / "learning.json", quiet_minutes=10, clock=clock), clock


def talk(*exchanges: tuple[str, str], at: float | None = None) -> None:
    """Log exchanges; the log was last written `at`."""
    for user, reply in exchanges:
        memory.log_exchange(user, reply)
    if at is not None:
        for f in profile.current().conversations.glob("*.md"):
            os.utime(f, (at, at))


def check(watch: learning.Learning) -> bool:
    return asyncio.run(watch.check())


def reviewed(prompt: str) -> str:
    """The conversation part of a prompt, minus earlier requests shown for spotting repeated workflows."""
    return prompt.split("<conversation>", 1)[1]


# ── When it runs ────────────────────────────────────────────────


def test_first_look_only_marks_what_is_already_logged(data):
    talk((CHATTY, "Noted."))
    reviewer = Reviewer()
    watch, clock = watcher(data, reviewer, [])
    clock.now += 3600
    assert not check(watch)  # no backlog: earlier history is only marked as read
    assert not check(watch) and reviewer.prompts == []


def test_reviews_once_the_conversation_has_been_quiet_long_enough_and_only_once(data):
    talk(("hello", "Hi."))
    reviewer, saved = Reviewer('{"suggestions": []}'), []
    watch, clock = watcher(data, reviewer, saved)
    check(watch)  # the mark
    talk((CHATTY, "Got it."), ("what time is it", "Ten past three."), at=clock.now)
    clock.now += 9 * 60
    assert not check(watch)  # still in the conversation's quiet minutes
    clock.now += 60
    assert check(watch)
    assert len(reviewer.prompts) == 1 and CHATTY in reviewer.prompts[0] and "Ten past three." in reviewer.prompts[0]
    assert "hello" not in reviewed(reviewer.prompts[0])  # reviewed before: the mark is past it
    assert "<earlier_requests>\n" in reviewer.prompts[0] and ": hello\n</earlier_requests>" in reviewer.prompts[0]
    clock.now += 3600
    assert not check(watch) and len(reviewer.prompts) == 1  # nothing new: never twice
    assert saved == []  # nothing worth keeping: nothing saved


def test_a_hang_up_reviews_soon_unless_the_user_carries_on(data):
    talk(("hello", "Hi."))
    reviewer = Reviewer('{"suggestions": []}')
    watch, clock = watcher(data, reviewer, [])
    check(watch)
    talk((CHATTY, "Got it."), at=clock.now)
    watch.ended()
    assert not check(watch)  # a dropped call gets a moment to reconnect
    clock.now += learning.END_GRACE_SECONDS
    assert check(watch) and len(reviewer.prompts) == 1

    talk((CHATTY + " again", "Sure."), at=clock.now)
    watch.ended()
    talk(("and one more thing about my setup that matters to me", "Yes?"), at=clock.now + 30)  # reconnected
    clock.now += learning.END_GRACE_SECONDS
    assert not check(watch)  # the conversation carried on: it waits for the quiet instead
    clock.now += 10 * 60
    assert check(watch) and len(reviewer.prompts) == 2
    assert "one more thing" in reviewer.prompts[1] and "Got it." not in reviewer.prompts[1]


def test_a_conversation_with_too_few_words_is_passed_over_without_a_call(data):
    talk(("hello", "Hi."))
    reviewer = Reviewer()
    watch, clock = watcher(data, reviewer, [])
    check(watch)
    talk(("what time is it", "Three."), at=clock.now)
    memory.log_announcement("Task 2, a long sentence about what the task did and found in the end, is done.")
    clock.now += 11 * 60
    assert not check(watch) and reviewer.prompts == []
    talk((CHATTY, "Got it."), at=clock.now)
    clock.now += 11 * 60
    check(watch)
    assert len(reviewer.prompts) == 1 and "what time is it" not in reviewed(reviewer.prompts[0])  # passed over for good


def test_a_call_failing_on_a_passing_error_is_tried_again_15_minutes_later_twice_then_given_up(data):
    talk(("hello", "Hi."))
    reviewer, saved = Reviewer(TimeoutError()), []
    watch, clock = watcher(data, reviewer, saved)
    check(watch)
    talk((CHATTY, "Got it."), at=clock.now)
    clock.now += 11 * 60
    assert not check(watch)
    assert not check(watch) and len(reviewer.prompts) == 1  # waits before trying again
    clock.now += 14 * 60
    assert not check(watch) and len(reviewer.prompts) == 1
    for _ in range(2):
        clock.now += 15 * 60
        check(watch)
    assert len(reviewer.prompts) == 3
    clock.now += 15 * 60
    assert not check(watch) and len(reviewer.prompts) == 3 and saved == []  # passed over


def test_a_call_failing_on_any_other_error_is_given_up_at_once(data):
    talk(("hello", "Hi."))
    reviewer, saved = Reviewer(retry.CallFailed("the model call failed: error_during_execution", retry.ERROR)), []
    watch, clock = watcher(data, reviewer, saved)
    check(watch)
    talk((CHATTY, "Got it."), at=clock.now)
    clock.now += 11 * 60
    assert check(watch) and saved == []  # passed over
    clock.now += 15 * 60
    assert not check(watch) and len(reviewer.prompts) == 1


def test_the_prompt_holds_memory_numbered_with_its_room_and_the_conversation(data):
    talk(("hello", "Hi."))
    reviewer = Reviewer()
    watch, clock = watcher(data, reviewer, [])
    check(watch)
    talk((CHATTY, "Got it."), at=clock.now)
    clock.now += 11 * 60
    check(watch)
    prompt = reviewer.prompts[0]
    assert "1. - Alex prefers short answers (2026-09-01)" in prompt
    assert "2. - Alex's laptop is a MacBook Air (2026-09-02)" in prompt
    assert "of 2,000 characters used" in prompt and "of 3,000 characters used" in prompt
    assert f"<conversation>\n## {TODAY}\n" in prompt and f"Alex: {CHATTY}" in prompt


def test_the_review_call_has_no_tools_and_untrusted_content_is_named_as_such(data, monkeypatch):
    import claude_agent_sdk

    seen = {}

    async def query(prompt, options):
        seen["options"], seen["prompt"] = options, prompt
        yield claude_agent_sdk.ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
                                             num_turns=1, session_id="s", result='{"suggestions": []}')

    monkeypatch.setattr(claude_agent_sdk, "query", query)
    assert asyncio.run(learning.model_review("the conversation")) == '{"suggestions": []}'
    options = seen["options"]
    assert options.tools == [] and options.allowed_tools == [] and options.max_turns == 1
    assert options.model == learning.LEARNING_MODEL
    assert "untrusted" in options.system_prompt and "to-do list, never in memory" in options.system_prompt


def test_the_review_saves_plans_as_plans_until_they_are_confirmed_done():
    text = learning.system()
    assert f'"{profile.name()} plans to' in text and "never as if it's already done" in text
    assert f"only when {profile.name()} confirms it's done: JARVIS saying it did it" in text
    assert "concrete job for later belongs on the to-do list" in text


# ── What it makes of the reply ──────────────────────────────────


@pytest.mark.parametrize("reply, n", [
    ('{"suggestions": [{"action": "remember"}]}', 1),
    ('Here you go:\n```json\n{"suggestions": [{"action": "remember"}, 3]}\n```', 1),
    ('{"suggestions": []}', 0),
    ("Nothing to save.", 0),
    ('{"suggestions": "none"}', 0),
    ('{"other": []}', 0),
    ("{not json}", 0),
    ("", 0),
])
def test_parse_reads_only_the_json_asked_for(reply, n):
    assert len(learning.parse(reply)) == n


def test_parse_keeps_at_most_a_few():
    many = json.dumps({"suggestions": [{"action": "remember"}] * 9})
    assert len(learning.parse(many)) == learning.MAX_SUGGESTIONS


def test_suggestions_become_data_edits_with_memory_lines_by_their_exact_text(data):
    lines = learning.numbered()
    found = learning.to_edits([
        {"action": "remember", "section": "user", "fact": "Alex uses Zed for editing", "reason": "Said so."},
        {"action": "remember", "section": "user", "fact": "alex prefers short answers.", "reason": "Dupe."},
        {"action": "update", "line": 2, "fact": "Alex's laptop is a 2022 MacBook Air", "reason": "More detail."},
        {"action": "update", "line": "1", "fact": "Alex prefers short answers", "reason": "Same."},
        {"action": "forget", "line": 1, "reason": "Said it's wrong."},
        {"action": "forget", "line": 9, "reason": "No such line."},
        {"action": "update", "line": 0, "fact": "x", "reason": "No such line."},
        {"action": "todo_add", "item": "Try Zed's vim mode"},
        {"action": "project_update", "name": "jarvis", "about": "x", "reason": "Not the review's to suggest."},
    ], lines, TODAY)
    assert found == [
        {"action": "remember", "section": "user", "fact": "Alex uses Zed for editing", "reason": "Said so."},
        {"action": "edit", "file": "memory.md", "old": "- Alex's laptop is a MacBook Air (2026-09-02)",
         "new": f"- Alex's laptop is a 2022 MacBook Air ({TODAY})", "reason": "More detail."},
        {"action": "forget", "text": "Alex prefers short answers (2026-09-01)", "reason": "Said it's wrong."},
        {"action": "todo_add", "item": "Try Zed's vim mode", "reason": "From the conversation."},
    ]
    for edit in found:
        data_edits.check(edit)  # each one the runner can take


def test_helpers_count_the_user_s_words_and_cut_a_long_conversation_at_an_entry(data):
    text = "[09:00] Alex: one two three\nJARVIS: four five\n\n[09:05] JARVIS (announced): six seven\n\n[09:06] Alex: eight\n"
    assert learning.user_words(text) == 4
    assert learning.span([("2026-10-06", "[23:50] Alex: hi\n"), ("2026-10-07", text)]) == "2026-10-06 23:50 to 2026-10-07 09:06"
    assert learning.span([("2026-10-07", text)]) == "2026-10-07 09:00 to 09:06"
    long = learning.transcript([("2026-10-07", "".join(f"[09:{i:02}] Alex: line {i}\nJARVIS: ok\n\n" for i in range(60)))], 300)
    assert long.startswith("(earlier exchanges left out)\n[09:") and "line 59" in long and "line 1\n" not in long


# ── How the runner keeps it ─────────────────────────────────────


def run_learned(suggested: list[dict]) -> tuple[runner.Runner, dict, list[dict], list[tuple]]:
    """Run Runner.learned on these suggestions; return (review task, events sent, alerts raised)."""
    alerts = []

    async def main():
        r = runner.Runner()

        async def alert(*args, **kwargs):
            alerts.append(args)

        r._notify, r._push = alert, alert
        q: asyncio.Queue = asyncio.Queue()
        r.listeners.add(q)
        task = await r.learned(suggested, "Review the conversation of today for lasting facts.")
        await asyncio.sleep(0)
        return r, task, [q.get_nowait() for _ in range(q.qsize())]

    r, task, events = asyncio.run(main())
    return r, task, events, alerts


REMEMBER = {"action": "remember", "section": "user", "fact": "Alex uses Zed for editing", "reason": "Said so."}
TODO = {"action": "todo_add", "item": "Try Zed's vim mode", "reason": "Wants to, later."}


def test_routine_suggestions_are_applied_at_once_and_quietly(data):
    before = activity.latest_id()
    r, task, events, alerts = run_learned([REMEMBER])
    assert f"- Alex uses Zed for editing ({TODAY})" in profile.current().memory_file.read_text()
    assert (task["kind"], task["status"], task["hidden"], task["seen"]) == ("review", "done", 1, 1)
    assert [e["status"] for e in edits(task)] == ["applied"]
    assert events == [] and alerts == []  # nothing to ask about, so nothing said
    assert r.store.recent() == [] and r.store.unseen() == []
    bell = [a["text"] for a in activity.recent() if a["id"] > before]
    assert bell == ["Learned from our conversation: save to memory, under About Alex: Alex uses Zed for editing"]


def test_riskier_suggestions_wait_for_a_yes_and_are_said_without_a_push(data):
    update = {"action": "edit", "file": "memory.md", "old": "- Alex prefers short answers (2026-09-01)",
              "new": f"- Alex prefers short, plain answers ({TODAY})", "reason": "Said so."}
    r, task, events, alerts = run_learned([REMEMBER, update, TODO, {"action": "remember", "fact": "x", "section": "nowhere",
                                                                    "reason": "Bad."}])
    kept = edits(task)
    assert [(e["action"], e["status"], bool(e.get("needs_yes"))) for e in kept] == [
        ("remember", "applied", False), ("edit", "pending", True), ("todo_add", "pending", True)]
    assert "short answers (2026-09-01)" in profile.current().memory_file.read_text()  # the rewrite waits
    assert "Zed" not in todo.read()
    assert (task["status"], task["hidden"], task["seen"]) == ("done", 0, 0)
    assert [e["type"] for e in events] == ["status"]
    assert events[0]["text"].startswith("Your task, learning review, is done. It read our last conversation for what's "
                                        "worth keeping in memory, the to-do list or skills. It'd also like to in memory.md")
    assert events[0]["text"].endswith("and add a to-do: Try Zed's vim mode.")
    assert alerts == []  # said in a live session or at the next greeting, never pushed
    assert "needs a yes" in announce.context(task) and r.store.unseen()[0]["id"] == task["id"]
    assert "left out a suggestion: section must be user or environment" in str(r.store.history(task["id"]))

    async def answer():
        return await r.edits(task["id"], "apply", [2])

    asyncio.run(answer())
    assert f"- Alex prefers short, plain answers ({TODAY})" in profile.current().memory_file.read_text()
    with pytest.raises(ValueError, match="learning review"):
        r.reply(task["id"], "carry on")


def test_a_suggested_skill_is_never_saved_or_switched_on_without_a_yes(data, monkeypatch):
    skill = {"action": "skill", "name": "standup-notes", "description": "Writes Alex's standup. Use when asked.",
             "instructions": "1. Read yesterday's commits.\n2. Say three bullet points.", "reason": "Asked three days running."}
    r, task, events, _ = run_learned([skill])
    [kept] = edits(task)
    assert (kept["action"], kept["status"]) == ("skill", "pending") and "saves a skill" in kept["needs_yes"]
    assert home.all_skills() == [] and events[0]["text"].endswith(
        "It'd also like to save a new skill standup-notes: Writes Alex's standup. Use when asked.")
    assert "1. save a new skill standup-notes: Writes Alex's standup." in announce.context(task)

    async def answer():
        return await r.edits(task["id"], "apply", [1])

    asyncio.run(answer())
    saved = home.get("standup-notes")
    assert saved.source == home.LEARNED and saved.usable


def test_a_full_section_fails_its_edit_without_a_word(data):
    profile.current().memory_file.write_text("## User\n" + "".join(f"- Fact number {i} about Alex, padded out a bit {'.' * 60}\n"
                                                         for i in range(20)))
    _, task, events, _ = run_learned([REMEMBER])
    assert [e["status"] for e in edits(task)] == ["failed"] and events == [] and task["hidden"] == 1


def test_review_to_dos_need_a_yes_where_other_tasks_do_not(data):
    edit = data_edits.check(TODO)
    assert data_edits.needs_yes(edit, "review") and data_edits.needs_yes(edit, "code_change") is None
    assert data_edits.needs_yes(data_edits.check(REMEMBER), "review") is None


def test_end_to_end_a_finished_conversation_lands_in_memory(data):
    reviewer = suggesting({"action": "remember", "section": "environment", "fact": "Alex edits code in Zed",
                           "reason": "Alex said they switched from VS Code."})

    async def main():
        r = runner.Runner()
        watch = learning.Learning(r.learned, reviewer, data["tmp"] / "learning.json", quiet_minutes=10)
        talk(("hello", "Hi."))
        await watch.check()
        talk((CHATTY, "Got it."), at=time.time() - 2 * learning.END_GRACE_SECONDS)
        watch.ended()
        watch.ended_at -= learning.END_GRACE_SECONDS  # hung up a while ago
        return await watch.check()

    reviewed = asyncio.run(main())
    assert reviewed and f"- Alex edits code in Zed ({TODAY})" in profile.current().memory_file.read_text()
    assert memory.parse(profile.current().memory_file.read_text())["environment"][-1].startswith("- Alex edits code in Zed")


def test_runner_hears_a_session_end_over_its_api(data):
    from aiohttp.test_utils import make_mocked_request

    async def main():
        r = runner.Runner()
        r.learning = learning.Learning(r.learned, Reviewer(), data["tmp"] / "learning.json")
        route = next(x for x in api.routes(r) if x.path == "/learn")
        await route.handler(make_mocked_request("POST", "/learn"))
        return r.learning.ended_at

    assert asyncio.run(main()) is not None


def test_task_edits_says_the_review_s_pending_edits_need_a_yes():
    description = tools.get("task_edits").description
    assert "learning review" in description and "needs the user's yes" in description
