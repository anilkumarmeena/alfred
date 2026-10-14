"""Tests for jarvis.state.memory: recent.md, the daily conversation logs and searching them; long-term memory limits."""

import asyncio
import subprocess
from datetime import datetime, timedelta

import pytest

from conftest import use_profile
from jarvis import tools
from jarvis.brain import cli, persona, side
from jarvis.core import profile
from jarvis.core.config import DATA_DIR, ROOT
from jarvis.safety import commands
from jarvis.state import memory


@pytest.fixture
def logs(tmp_path, monkeypatch):
    use_profile(monkeypatch, tmp_path)
    use_profile(monkeypatch, name="Alex")
    return tmp_path / "conversations"


def today() -> str:
    return f"{datetime.now(profile.tz()):%Y-%m-%d}"


def test_each_exchange_goes_into_the_day_s_file_as_well_as_recent(logs):
    memory.log_exchange(" what's on today? ", "Two meetings.\n")
    memory.log_exchange("delete the old renders", "Clearing them out.")
    day = (logs / f"{today()}.md").read_text()
    assert day.startswith(f"# Conversations, {datetime.now(profile.tz()):%A %-d %B %Y}\n\n[")
    assert "] Alex: what's on today?\nJARVIS: Two meetings.\n\n[" in day
    assert day.endswith("] Alex: delete the old renders\nJARVIS: Clearing them out.\n\n")
    assert day.count("# Conversations") == 1
    assert "Alex: what's on today?" in memory.read_recent()


def test_the_day_s_file_keeps_everything_while_recent_is_trimmed(logs, monkeypatch):
    monkeypatch.setattr(memory, "RECENT_MAX_CHARS", 200)
    for n in range(20):
        memory.log_exchange(f"question {n}", f"answer {n}")
    assert len(memory.read_recent()) <= 200 and "question 0\n" not in memory.read_recent()
    day = (logs / f"{today()}.md").read_text()
    assert all(f"Alex: question {n}\nJARVIS: answer {n}" in day for n in range(20))


def test_approvals_and_announcements_are_logged(logs):
    memory.log_announcement("Task 12, fix the build, is done. ")
    memory.log_exchange("yes go ahead", "Done.", ["rm -rf ~/Desktop/renders/old"])
    day = (logs / f"{today()}.md").read_text()
    assert "] JARVIS (announced): Task 12, fix the build, is done.\n\n" in day
    assert "] Alex: yes go ahead\n(Approved: rm -rf ~/Desktop/renders/old)\nJARVIS: Done.\n\n" in day
    assert "Approved" not in memory.read_recent()


def test_a_reply_cut_off_is_logged_as_interrupted_rather_than_blank(logs):
    memory.log_exchange("I think", "", interrupted=True)
    memory.log_exchange("is the first turn slow?", "The first turn starts the", interrupted=True)
    memory.log_exchange("thanks", "")  # an empty reply is logged as is
    day = (logs / f"{today()}.md").read_text()
    assert "] Alex: I think\nJARVIS: (interrupted before replying)\n\n" in day
    assert "] Alex: is the first turn slow?\nJARVIS: The first turn starts the (interrupted)\n\n" in day
    assert day.endswith("] Alex: thanks\nJARVIS: \n\n")
    assert "Alex: I think\nJARVIS: (interrupted before replying)" in memory.read_recent()


def test_the_prompt_says_a_stray_bye_mid_work_is_noise():
    assert "lone \"bye\", \"thank you\"" in persona.persona() and "Don't end the conversation" in persona.persona()
    assert "reads like the rest of the one before" in persona.persona()


def test_notes_are_logged_as_announcements(logs):
    notes = persona.Notes()
    notes.note("Task 3 needs your answer.")
    assert "JARVIS (announced): Task 3 needs your answer." in (logs / f"{today()}.md").read_text()
    assert "Task 3 needs your answer." in notes.turn("what is it?")


def test_the_voice_brain_logs_the_commands_a_yes_approved(logs, monkeypatch):
    from test_shell import COMMAND, FakeClient, spoken_reply

    b = cli.CliBrain()
    b.client = FakeClient(b.shell, "Clearing them out.", "")
    asyncio.run(spoken_reply(b, "clear out the old renders"))
    asyncio.run(spoken_reply(b, "Yes."))
    day = (logs / f"{today()}.md").read_text()
    assert f"Alex: Yes.\n(Approved: {COMMAND})\nJARVIS:" in day
    assert day.count("(Approved:") == 1


def write_day(logs, day: str, *entries: str) -> None:
    logs.mkdir(exist_ok=True)
    (logs / f"{day}.md").write_text(f"# Conversations, {day}\n\n" + "".join(f"{e}\n\n" for e in entries))


def test_search_finds_exchanges_with_every_word_newest_first(logs):
    write_day(logs, "2026-09-20", "[09:00] Alex: where are the logs kept?\nJARVIS: In data.",
              "[09:05] Alex: thanks\nJARVIS: Any time.")
    write_day(logs, "2026-09-21", "[10:00] Alex: move the logs\nJARVIS: Moved the\nconversation logs.")
    found = memory.search_conversations("LOGS")
    assert found.index("2026-09-21 [10:00]") < found.index("2026-09-20 [09:00]")
    assert "Moved the\nconversation logs." in found and "Any time" not in found
    assert memory.search_conversations("where logs") == "2026-09-20 [09:00] Alex: where are the logs kept?\nJARVIS: In data."
    assert memory.search_conversations("renders") == "No conversations mention 'renders'."


def test_search_by_date_or_recent_days(logs):
    old = f"{datetime.now(profile.tz()).date() - timedelta(days=10):%Y-%m-%d}"
    write_day(logs, old, "[09:00] Alex: old one\nJARVIS: Yes.")
    write_day(logs, today(), "[11:00] Alex: new one\nJARVIS: Yes.", "[11:30] JARVIS (announced): Task 4 is done.")
    assert memory.search_conversations(date=old) == f"{old} [09:00] Alex: old one\nJARVIS: Yes."
    recent = memory.search_conversations(days=3)
    assert "new one" in recent and "Task 4 is done" in recent and "old one" not in recent
    assert memory.search_conversations("one", days=30).count("Alex:") == 2
    assert memory.search_conversations(date="2020-01-01") == "No conversations on 2020-01-01 logged."
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        memory.search_conversations(date="yesterday")


def test_search_keeps_its_answer_short(logs, monkeypatch):
    monkeypatch.setattr(memory, "SEARCH_MAX_CHARS", 300)
    write_day(logs, today(), *(f"[10:{n:02}] Alex: note {n}\nJARVIS: {'x' * 50}" for n in range(20)))
    found = memory.search_conversations("note")
    assert len(found) < 500 and found.startswith(f"{today()} [10:19]")
    assert found.endswith("older matches not shown; narrow the search.)")


def test_search_with_no_logs_yet(logs):
    assert memory.search_conversations() == "No conversations logged."


def test_conversation_search_is_a_voice_tool(logs):
    write_day(logs, today(), "[10:00] Alex: hello\nJARVIS: Hello.")
    assert "conversation_search" in {t.name for t in tools.all_tools()}
    content, is_error = asyncio.run(tools.run("conversation_search", {"text": "hello", "days": 1}))
    assert not is_error and "Alex: hello" in content
    content, is_error = asyncio.run(tools.run("conversation_search", {"date": "last week"}))
    assert is_error and "YYYY-MM-DD" in content


def test_the_logs_stay_out_of_git_and_within_reach_of_the_shell_and_lookup():
    assert profile.current().conversations == DATA_DIR / "conversations"
    ignored = subprocess.run(["git", "check-ignore", "-q", "data/conversations/2026-09-27.md"], cwd=ROOT)
    assert ignored.returncode == 0
    path = str(profile.current().conversations / "2026-09-27.md")
    assert not any(path.startswith(str(p)) for p in commands.JARVIS_SECRETS) and not commands.names_secret(path)


def test_the_file_lookup_knows_the_layout_and_starts_from_the_repo():
    prompt = side.lookup_system()
    assert f"JARVIS's folder, {ROOT}" in prompt
    for where in ("memory.md", "conversations/YYYY-MM-DD.md", "tasks.db", "secrets/"):
        assert where in prompt
    assert ("the project list and the to-do list (a database you can't read: say to use task_list or task_status, "
            "projects_list for the projects and their folders, or todo_list") in prompt
    assert "projects.toml" not in prompt and "improvements.md" not in prompt
    for path in ("jarvis/", "server.py", "web/", "tests/", "docs/architecture.md", "deploy/", "README.md", "Makefile"):
        assert path in prompt, path
        assert (ROOT / path).exists() or (ROOT / "jarvis" / path).exists(), path  # the map names only what exists
    assert side.FILE_READS == ["Read", "Glob", "Grep"] and side.LOOKUP_READS < side.LOOKUP_TURNS


# ── Long-term memory: its two sections and their limits ─────────


@pytest.fixture
def mem(tmp_path, monkeypatch):
    use_profile(monkeypatch, tmp_path)
    use_profile(monkeypatch, name="Alex")
    return profile.current().memory_file


def dated(fact: str) -> str:
    return f"- {fact} ({datetime.now():%Y-%m-%d})"


def fill(section: str, size: int, n: int = 5) -> list[str]:
    """Write `section` as `n` lines taking `size` characters in all."""
    each, extra = divmod(size, n)
    lines = [f"- {str(i) * (each - 15 + (i == 0) * extra)} (2026-09-01)" for i in range(n)]
    found = memory.facts()
    found[section] = lines
    profile.current().memory_file.write_text(memory.render(found))
    assert memory.usage(lines) == size
    return lines


def test_each_fact_goes_under_its_section_s_heading(mem):
    assert memory.remember("  Likes   short answers ", "user") == "Saved to About Alex (34 of 2,000)."
    assert memory.remember("Runs JARVIS on an M2", "Environment") == "Saved to Alex's environment (35 of 3,000)."
    memory.remember("Speaks English only", "user")
    assert mem.read_text() == (f"## User\n{dated('Likes short answers')}\n{dated('Speaks English only')}\n\n"
                               f"## Environment\n{dated('Runs JARVIS on an M2')}\n")
    with pytest.raises(ValueError, match="section must be user or environment"):
        memory.remember("Likes tea", "home")
    with pytest.raises(ValueError, match="empty"):
        memory.remember("  ", "user")


def test_lines_under_no_heading_count_as_user(mem):
    mem.write_text("- Old fact (2026-01-01)\n\n# Notes\n- Under another heading\n## Environment\n- Has a Mac\n"
                   "## user\n- Late\n")
    assert memory.facts() == {"user": ["- Old fact (2026-01-01)", "- Under another heading", "- Late"],
                              "environment": ["- Has a Mac"]}
    memory.remember("New", "environment")  # reshaped, no fact lost
    assert mem.read_text() == ("## User\n- Old fact (2026-01-01)\n- Under another heading\n- Late\n\n"
                               f"## Environment\n- Has a Mac\n{dated('New')}\n")


def test_a_full_section_refuses_a_fact_and_lists_its_own(mem):
    lines = fill("user", 1990)
    fill("environment", 100, 2)
    before = mem.read_text()
    with pytest.raises(memory.Full) as e:
        memory.remember("Likes tea", "user")
    said = str(e.value)
    assert said.startswith("About Alex is full: this would make it 2,014 of 2,000 characters. "
                           "Its facts now (1,990 of 2,000, each with its length):\n")
    assert all(f"{n}. {line} [{len(line)}]" in said for n, line in enumerate(lines, 1))
    assert said.endswith("Merge, shorten or drop some of them, keeping what still matters, then save again.")
    assert e.value.short == "About Alex is full: this would make it 2,014 of 2,000 characters."
    assert mem.read_text() == before
    assert memory.remember("Likes tea", "environment").startswith("Saved to Alex's environment")  # the other section has room


def test_a_fact_over_400_characters_is_refused(mem):
    with pytest.raises(ValueError, match="at most 400 characters, its date included, and this one is 401"):
        memory.remember("x" * 386, "environment")
    memory.remember("x" * 385, "environment")
    assert len(memory.facts()["environment"][0]) == 400


def test_nearly_full_says_to_consolidate(mem):
    fill("environment", 2650)
    assert memory.remember("A short one", "environment") == "Saved to Alex's environment (2,676 of 3,000)."
    out = memory.remember("x" * 30, "environment")
    assert out == ("Saved to Alex's environment (2,721 of 3,000). It's nearly full, so consolidate soon: merge or "
                   "shorten facts with memory_replace.")


def test_replace_needs_exactly_one_line_and_keeps_to_the_limits(mem):
    memory.remember("Likes tea", "user")
    memory.remember("Likes tea, not coffee", "user")
    memory.remember("Has a Mac mini", "environment")
    with pytest.raises(ValueError, match="'likes tea' is in 2 memory lines"):
        memory.replace("likes tea", "Likes green tea")
    with pytest.raises(ValueError, match="No memory line contains 'cocoa'"):
        memory.replace("cocoa", "Likes cocoa")
    assert memory.replace("NOT   coffee", "Likes tea and coffee") == "Replaced in About Alex (59 of 2,000)."
    assert memory.replace("mac mini", "Has an M2 MacBook Air") == "Replaced in Alex's environment (36 of 3,000)."
    assert memory.facts() == {"user": [dated("Likes tea"), dated("Likes tea and coffee")],
                              "environment": [dated("Has an M2 MacBook Air")]}
    with pytest.raises(ValueError, match="at most 400"):
        memory.replace("MacBook", "x" * 390)
    fill("user", 1990, 10)
    with pytest.raises(memory.Full, match="About Alex is full"):
        memory.replace("0000", "0" * 300)  # longer than the line it replaces, in a full section
    assert memory.replace("0000", "Merged").startswith("Replaced in About Alex")  # shorter is fine


def test_a_rewritten_fact_keeps_one_date_today_s(mem):
    memory.remember("Eclipse is the web app (2026-10-10)", "environment")
    assert memory.facts()["environment"] == [dated("Eclipse is the web app")]
    memory.replace("eclipse", "Eclipse is the web and phone app (2026-10-10) (2026-10-10) ")
    assert memory.facts()["environment"] == [dated("Eclipse is the web and phone app")]
    memory.replace_fact(dated("Eclipse is the web and phone app"), f"Eclipse is the app ({datetime.now():%Y-%m-%d})")
    assert memory.facts()["environment"] == [dated("Eclipse is the app")]
    memory.remember("Born (1990-01-01) in Delhi", "user")  # only trailing dates go
    assert memory.facts()["user"] == [dated("Born (1990-01-01) in Delhi")]
    with pytest.raises(ValueError, match="empty"):
        memory.remember("(2026-10-10)", "user")


def test_a_file_over_its_limit_by_hand_can_still_be_brought_down(mem):
    lines = fill("user", 2400, 6)
    memory.replace(lines[1][2:20], "Shorter")  # still over, but less so
    with pytest.raises(memory.Full):
        memory.remember("More", "user")
    memory.check_text(mem.read_text(), mem.read_text())  # unchanged is fine
    with pytest.raises(memory.Full):
        memory.check_text(mem.read_text())  # with nothing to compare, over the limit fails


def test_the_file_still_parses_after_forget_and_replace_fact(mem):
    memory.remember("Likes tea", "user")
    memory.remember("Likes coffee", "user")
    memory.remember("Tea kettle on the desk", "environment")
    assert memory.forget("TEA") == "Removed 2 matching line(s)."
    assert memory.facts() == {"user": [dated("Likes coffee")], "environment": []}
    assert memory.replace_fact(dated("Likes coffee"), "Likes coffee, black") == "Changed: Likes coffee, black"
    assert memory.facts() == {"user": [dated("Likes coffee, black")], "environment": []}
    assert memory.replace_fact(dated("Likes coffee, black")) == "Forgotten."
    assert mem.read_text() == "## User\n\n## Environment\n"
    assert memory.facts() == {"user": [], "environment": []}
    memory.remember("Back", "environment")
    assert memory.facts() == {"user": [], "environment": [dated("Back")]}
    with pytest.raises(ValueError, match="changed or gone"):
        memory.replace_fact("## Environment", "x")


def test_the_prompt_shows_each_section_with_its_usage(mem):
    memory.remember("Likes short answers", "user")
    assert memory.prompt_block() == ("<long_term_memory>\nAbout Alex (34 of 2,000 characters):\n"
                                     f"{dated('Likes short answers')}\n"
                                     "Alex's environment (0 of 3,000 characters):\n(nothing yet)\n</long_term_memory>")
    assert memory.prompt_block() in persona.system_prompt()


def test_the_memory_tools_say_which_section_and_when_to_consolidate():
    assert {"remember", "memory_replace", "forget"} <= {t.name for t in tools.all_tools()}
    remember, replace = tools.get("remember"), tools.get("memory_replace").description
    sections = remember.properties["section"]["description"]
    assert "environment: their machine, setup, tools, projects and accounts" in sections
    assert "rather than adding a near-duplicate" in remember.description and "size limit" in remember.description
    for rule in ("as long as no information is lost",
                 "Dropping information, by forget or by a rewrite that leaves something out, needs the user's yes"):
        assert rule in replace, rule
    assert "Dropping information needs the user's yes" in tools.get("forget").description
    assert "dropping information from memory" in persona.persona()  # one of the persona's few confirmations
    assert tools.get("remember").required == ["fact", "section"]
