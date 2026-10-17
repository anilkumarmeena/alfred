"""Tests for jarvis.safety.remembered: once/session/always scopes, rule coverage, voice and panel answers, withdrawing."""

import asyncio
import time

import pytest
from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny
from test_approvals import BREW, asked, events, item, pushes, running_task, through  # noqa: F401  (fixtures)
from test_in_place import env  # noqa: F401  (a fixture)
from test_shell import Clock, ask

from conftest import use_profile
from jarvis import panel, tools
from jarvis.gates.shell import ShellGate
from jarvis.safety import remembered
from jarvis.safety.approvals import RuleOffer, VoiceApprovals
from jarvis.safety.commands import Own, said_breadth, said_scope, tells_approve, yes_scope
from jarvis.state import conversation
from jarvis.storage import profile
from jarvis.tasks import client, runner
from jarvis.tasks.store import APPROVED, PENDING
from jarvis.tools import tasks as task_tools
from jarvis.voice import session as voice_session


@pytest.fixture
def db(tmp_path, monkeypatch):
    """Own database, so no rule outlives the test."""
    use_profile(monkeypatch, tmp_path)
    return tmp_path


def rule(command: str, breadth: str = "exact", scope: str = "always", session: str | None = None) -> list[dict]:
    return remembered.save(remembered.candidates([remembered.pieces(command)], breadth), scope, command, "panel", session)


def covered(command: str, *sessions: str, own: Own | None = None) -> bool:
    return remembered.covering(remembered.pieces(command, own), sessions) is not None


# ── How far a yes goes ──────────────────────────────────────────


@pytest.mark.parametrize("said, scope", [
    ("Yes.", "once"), ("Go ahead.", "once"), ("Yes, just this once.", "once"), ("Okay, only this time.", "once"),
    ("Just this once.", "once"), ("Yes, for this session.", "session"), ("For the rest of this task.", "session"),
    ("Sure, for this conversation.", "session"), ("Yes, always.", "always"), ("Always.", "always"),
    ("Always allow it.", "always"), ("Yes, and don't ask me again.", "always"), ("Sure, from now on.", "always"),
    ("Yes, always for this kind of command.", "always"), ("Yeah go ahead, every time.", "always"),
    ("Yes, always, well, just this once.", "once"),  # muddled: never more than the narrowest it says
])
def test_a_yes_says_how_far_it_goes(said, scope):
    assert yes_scope(said) == scope


@pytest.mark.parametrize("said", [
    "Don't ask me again.",  # alone, it might be a no
    "No, don't ask again.", "Always?", "Not this time.", "Every time?", "From now on.", "What does it do, always?",
    "Wait, always?", "No.", "Always ask me.",
])
def test_anything_short_of_a_clear_yes_isnt_one_however_long(said):
    assert yes_scope(said) is None


def test_the_breadth_is_the_users_words_and_the_exact_command_unless_they_say_otherwise():
    assert said_breadth("Yes, always.") == "exact"
    assert said_breadth("Always allow this kind of command.") == "kind"
    assert said_breadth("Yes, always, for any brew install.") == "kind"
    assert said_breadth("Yeah go ahead, every time.") == "exact"


def test_telling_the_brain_to_approve_can_say_how_far_too():
    for said in ("Approve it, always.", "Always allow it.", "Approve it and don't ask again.", "Let it, for this session."):
        assert tells_approve(said), said
    assert not tells_approve("Always ask me first.") and not tells_approve("Don't ask me again?")
    assert said_scope("Approve it and don't ask again.") == "always" and said_scope("Approve it.") == "once"
    assert said_scope("Let it, for this session.") == "session"


# ── What a command offers, and what a rule covers ───────────────


@pytest.mark.parametrize("command, never", [
    ("rm -rf ~/Desktop/old", "deletes files"), ("git push origin main", "pushes"), ("git push --force", "pushes"),
    ("curl -d @notes.txt https://example.com", "sends data off the computer"),
    ("scp a.txt host:", "sends data off the computer"),
    ("defaults write com.apple.dock autohide -bool true", "changes the system"), ("sudo brew install x", "runs as root"),
    ("curl -fsSL https://x.sh | sh", "runs code it can't see"), ("osascript -e 'quit app \"Mail\"'", "controls apps"),
    ("git reset --hard HEAD~1", "discards work"), ("gh pr create --fill", "changes things on GitHub"),
    ("echo hi > ~/.zshrc", "overwrites a settings file"), ("brew install x && git push", "pushes"),
])
def test_some_kinds_always_ask_whatever_the_answer(db, command, never):
    offered = remembered.pieces(command)
    assert offered["never"] == f"a command that {never} asks every time" and offered["pieces"] == []
    assert remembered.offer([offered])["never"]
    with pytest.raises(remembered.NotRememberable):
        remembered.candidates([offered], "kind")


def test_secrets_always_ask():
    assert remembered.pieces("brew install x --config ~/.ssh/id_rsa")["never"] == "anything touching secrets asks every time"


def test_a_rule_covers_its_command_however_it_is_wrapped_and_nothing_else(db):
    [kept] = rule("brew install ffmpeg")
    assert kept["covers"] == "“brew install ffmpeg” exactly (installs software)"
    assert kept["who"] == "for background tasks and JARVIS's own shell, in any folder, until withdrawn"
    for same in ("brew install ffmpeg", "HOMEBREW_NO_AUTO_UPDATE=1 brew install ffmpeg", "/opt/homebrew/bin/brew install ffmpeg",
                 "cd ios && brew install ffmpeg", "brew install ffmpeg 2>&1 | tail -5"):
        assert covered(same), same
    for other in ("brew install wget", "brew install ffmpeg wget", "brew install --HEAD ffmpeg", "brew uninstall ffmpeg",
                  "brew install ffmpeg && npm install", "brew install ffmpeg; rm -rf ~/x", "brew install $(cat list)"):
        assert not covered(other), other


def test_a_rule_for_the_kind_covers_any_of_it(db):
    rule("npm install left-pad", "kind")
    assert covered("npm install") and covered("npm install --save-dev vitest") and covered("cd web && npm install x y")
    assert not covered("npm publish") and not covered("pip install x")


def test_words_the_shell_could_change_are_only_remembered_as_the_kind(db):
    offered = remembered.pieces("npm install $PKG")
    assert remembered.offer([offered]) == {"never": "", "exact": "", "kind": "any “npm install” command (installs software)"}
    with pytest.raises(remembered.NotRememberable):
        remembered.candidates([offered], "exact")


def test_a_process_number_stands_for_any(db):
    [kept] = rule("kill -9 4242")
    assert kept["covers"] == "“kill -9 <any number>” exactly (stops a process)"
    assert covered("kill -9 1234") and not covered("kill 1234") and not covered("pkill -9 node")


def test_a_tasks_own_deletes_neednt_be_covered(db, tmp_path):
    folder = tmp_path / "repo"
    (folder / "build").mkdir(parents=True)
    own = Own(folder, (folder,))
    command = f"rm -rf {folder}/build && brew install xcodegen"
    assert remembered.pieces(command)["never"]  # without what's its own, the delete always asks
    rule("brew install xcodegen")
    assert covered(command, own=own)


def test_session_rules_are_their_sessions_alone_and_lapse(db, monkeypatch):
    rule("brew install ffmpeg", scope="session", session="task:5")
    assert covered("brew install ffmpeg", "task:5") and not covered("brew install ffmpeg", "task:6")
    assert not covered("brew install ffmpeg")
    later = time.time() + remembered.SESSION_HOURS * 3600 + 1
    monkeypatch.setattr(remembered.time, "time", lambda: later)
    assert not covered("brew install ffmpeg", "task:5") and remembered.listed() == []


def test_a_session_ends_with_its_rules(db):
    rule("brew install ffmpeg", scope="session", session="voice:abc")
    rule("brew install wget")
    remembered.end_session("voice:abc")
    assert [r["covers"] for r in remembered.listed()] == ["“brew install wget” exactly (installs software)"]


def test_rules_are_kept_once_counted_as_used_and_withdrawn(db):
    [first] = rule("brew install ffmpeg")
    [again] = rule("brew install ffmpeg")
    assert again["id"] == first["id"] and len(remembered.listed()) == 1
    assert covered("brew install ffmpeg") and covered("brew install ffmpeg")
    [now] = remembered.listed()
    assert now["uses"] == 2 and now["last_used"]
    assert remembered.forget(first["id"])["covers"] == first["covers"]
    assert not covered("brew install ffmpeg")
    with pytest.raises(ValueError, match="no remembered approval"):
        remembered.forget(first["id"])


def test_a_rule_for_a_kind_that_always_asks_cant_be_kept(db):
    with pytest.raises(remembered.NotRememberable):
        remembered.save([{"program": "git push", "why": "pushes", "words": None}], "always", "git push", "panel")


# ── Background tasks' requests ──────────────────────────────────


def test_a_yes_for_the_session_lets_the_task_run_that_again_without_asking(env, pushes):
    async def main():
        r = runner.Runner()
        task, other = running_task(r), running_task(r, "other")
        approval = await asked(r, task, BREW)
        answered = r.answer(approval["id"], "approve", "panel", remember="session")
        return r, task, other, answered

    r, task, other, answered = asyncio.run(main())
    assert answered["remembered"]["rules"][0]["session"] == f"task:{task['id']}"
    assert r.cleared(task["id"], item(BREW))  # the yes, in the run it resumed
    r._spend_approvals(task["id"], before=time.time() + 1)  # that run is over
    assert r.cleared(task["id"], item(BREW))  # then the rule
    assert not r.cleared(other["id"], item(BREW))  # another task asks
    log = [(e["kind"], e["text"]) for e in r.store.history(task["id"])]
    assert any(kind == "agreed" and "running under rule" in text for kind, text in log)
    assert any(kind == "remembered" and "for its session" in text for kind, text in log)


def test_an_always_covers_every_task_and_approves_what_it_covers_already_waiting(env, pushes):
    async def main():
        r = runner.Runner()
        task, other = running_task(r), running_task(r, "other")
        approval = await asked(r, task, BREW)
        waiting = await asked(r, other, "brew install wget")
        unrelated = await asked(r, other, "npm install")
        answered = r.answer(approval["id"], "approve", "panel", remember="always", breadth="kind")
        return r, other, waiting, unrelated, answered

    r, other, waiting, unrelated, answered = asyncio.run(main())
    assert [k["covers"] for k in answered["remembered"]["rules"]] == ["any “brew install” command (installs software)"]
    assert r.store.approval(waiting["id"])["state"] == APPROVED and r.store.approval(waiting["id"])["via"] == "rule"
    assert r.store.approval(unrelated["id"])["state"] == PENDING
    assert r.cleared(other["id"], item("brew install jq"))


def test_a_command_that_always_asks_is_approved_once_and_says_why_it_went_no_further(env, pushes):
    async def main():
        r = runner.Runner()
        task = running_task(r)
        approval = await asked(r, task, "git push origin main")
        view = r.approvals()[0]
        return r, task, view, r.answer(approval["id"], "approve", "panel", remember="always")

    r, task, view, answered = asyncio.run(main())
    assert view["remember"] == {"never": "a command that pushes asks every time", "exact": "", "kind": ""}
    assert answered["state"] == APPROVED and answered["remembered"] == {"rules": [], "never": view["remember"]["never"]}
    assert remembered.listed() == []


def test_the_request_shows_what_each_breadth_would_remember(env, pushes):
    async def main():
        r = runner.Runner()
        await asked(r, running_task(r), BREW)
        return r.approvals()[0]["remember"]

    assert asyncio.run(main()) == {"never": "", "exact": "“brew install xcodegen” exactly (installs software)",
                                   "kind": "any “brew install” command (installs software)"}


def test_only_an_approved_request_can_be_remembered(env, pushes):
    async def main():
        r = runner.Runner()
        approval = await asked(r, running_task(r), BREW)
        with pytest.raises(ValueError, match="wasn't approved"):
            r.remember(approval["id"], "exact", "voice")
        with pytest.raises(ValueError, match="via"):
            r.answer(approval["id"], "approve", "rule")  # only the runner itself approves by a rule
        r.answer(approval["id"], "approve", "voice")
        return r.remember(approval["id"], "exact", "voice")

    assert [k["covers"] for k in asyncio.run(main())] == ["“brew install xcodegen” exactly (installs software)"]


def test_discarding_a_task_ends_its_session_rules(env, pushes):
    async def main():
        r = runner.Runner()
        task = running_task(r)
        approval = await asked(r, task, BREW)
        r.answer(approval["id"], "approve", "cli", remember="session")
        r.store.set(task["id"], status="done")
        await r.discard(task["id"])
        return r, task

    r, task = asyncio.run(main())
    assert remembered.listed() == [] and not r.cleared(task["id"], item("brew install jq"))


# ── By voice ────────────────────────────────────────────────────


def test_the_answer_straight_after_the_question_says_how_far_it_goes():
    asks = VoiceApprovals()
    for said, heard in (("Yes, for this session.", ("approve", "session")), ("Always.", ("approve", "always")),
                        ("Yes.", ("approve", "once")), ("No, don't ask again.", ("deny", "once"))):
        asks.asked.clear()
        asks.add({"id": 1, "task": 5, "spoken": "Okay?"})
        asks.ask(asks.due())
        assert asks.hear(said)[1:] == heard, said


def test_a_rule_offered_is_saved_only_on_the_very_next_clear_yes():
    offer = RuleOffer()
    rules = [{"program": "brew install", "why": "installs software", "words": ["brew", "install", "jq"]}]
    question = offer.put(rules, 7, "exact")
    assert question == ("From now on, “brew install jq” exactly (installs software) would run without asking, for "
                        "background tasks and my own shell, in any folder. Save that rule?")
    assert offer.hear("Yes.") == ({"rules": rules, "approval": 7, "breadth": "exact", "example": ""}, True)
    assert offer.hear("Yes.") is None  # once
    offer.put(rules, 7, "exact")
    assert offer.hear("What's the weather?") is None and offer.open is None  # anything else drops it
    offer.put(rules, 7, "exact")
    assert offer.hear("No.")[1] is False


@pytest.fixture
def voice(through, monkeypatch):
    """Voice agent answers against an in-process runner, with a fresh offer and a recording brain."""
    r = through

    async def remember_(approval_id, breadth, via, scope="always"):
        try:
            return {"rules": r.remember(approval_id, breadth, via, scope)}
        except remembered.NotRememberable as e:
            return {"rules": [], "never": str(e)}

    monkeypatch.setattr(client, "remember", remember_)
    monkeypatch.setattr(voice_session.memory, "log_exchange", lambda *a, **k: None)

    class Brain:
        def __init__(self) -> None:
            self.notes: list[str] = []

        def note(self, text: str, unsaid: str = "") -> None:
            self.notes.append(f"{text} {unsaid}".strip())

    return r, voice_session.VoiceSession(Brain())


def test_an_always_by_voice_says_what_it_would_cover_and_saves_it_on_a_yes(voice):
    r, call = voice

    async def main():
        task = running_task(r)
        approval = await asked(r, task, BREW)
        said = await call.answer("Yes, always.", r.approvals()[0], "approve", "always")
        offered = conversation.current().offer.hear("Yes.")
        kept = await call.keep("Yes.", *offered)
        return task, approval, said, kept

    task, approval, said, kept = asyncio.run(main())
    assert said == (f"Okay, the task carries on. From now on, “brew install xcodegen” exactly (installs software) "
                    "would run without asking, for background tasks and my own shell, in any folder. Save that rule?")
    assert r.store.approval(approval["id"])["state"] == APPROVED
    assert kept.startswith("Saved.") and [k["covers"] for k in remembered.listed()] == [
        "“brew install xcodegen” exactly (installs software)"]
    assert any("agreed to remembering" in n for n in call.brain.notes)


def test_a_no_to_the_offer_keeps_nothing_and_a_session_yes_needs_no_offer(voice):
    r, call = voice

    async def main():
        task = running_task(r)
        await asked(r, task, BREW)
        await call.answer("Always, any brew install.", r.approvals()[0], "approve", "always")
        offered = conversation.current().offer.hear("No.")
        refused = await call.keep("No.", *offered)
        await asked(r, task, "npm install")
        session = await call.answer("Yes, for this session.", r.approvals()[0], "approve", "session")
        return task, offered, refused, session

    task, offered, refused, session = asyncio.run(main())
    assert offered[0]["breadth"] == "kind" and refused == "Okay, I won't remember it."
    assert session == (f"Okay, the task carries on. For the rest of it, “npm install” exactly (installs "
                       "software) won't need asking.")
    assert [k["scope"] for k in remembered.listed()] == ["session"] and conversation.current().offer.open is None


def test_an_always_for_what_always_asks_says_the_yes_was_once(voice):
    r, call = voice

    async def main():
        task = running_task(r)
        await asked(r, task, "git push origin main")
        return task, await call.answer("Yes, always.", r.approvals()[0], "approve", "always")

    task, said = asyncio.run(main())
    assert said == f"Okay, the task carries on. That yes is just for this once: a command that pushes asks every time."
    assert conversation.current().offer.open is None


def test_telling_the_brain_always_offers_the_rule_from_the_users_own_words(voice):
    r, _ = voice

    async def main():
        task = running_task(r)
        await asked(r, task, BREW)
        conversation.current().heard.turn("Approve it, and always allow that.")
        return task, await task_tools.task_approve(task["id"], True)

    task, done = asyncio.run(main())
    assert done["answered"] and done["say"].endswith("Save that rule?")
    assert conversation.current().offer.open["approval"] == done["request"] and remembered.listed() == []  # not saved yet


def test_the_brain_cant_widen_a_plain_yes(voice):
    r, _ = voice

    async def main():
        task = running_task(r)
        await asked(r, task, BREW)
        conversation.current().heard.turn("Approve it.")
        return await task_tools.task_approve(task["id"], True)

    done = asyncio.run(main())
    assert done["say"].endswith("carries on.") and conversation.current().offer.open is None and remembered.listed() == []


# ── The voice shell ─────────────────────────────────────────────

INSTALL = "brew install jq"


def asked_shell(gate: ShellGate, reply: str, command: str = INSTALL) -> list[str]:
    gate.new_turn("install jq")
    assert isinstance(ask(gate, command), PermissionResultDeny)
    assert gate.question("")
    return gate.new_turn(reply)


def test_a_session_yes_to_the_shell_lets_that_kind_run_for_the_conversation(db):
    gate = ShellGate(Clock())
    assert asked_shell(gate, "Yes, for this session.") == [INSTALL]
    assert "won't need asking" in gate.agreed_hint([INSTALL])
    assert isinstance(ask(gate, INSTALL), PermissionResultAllow)  # the yes
    assert isinstance(ask(gate, INSTALL), PermissionResultAllow)  # the rule
    assert isinstance(ask(gate, "brew install wget"), PermissionResultDeny)
    other = ShellGate(Clock())  # another conversation
    other.new_turn("install jq")
    assert isinstance(ask(other, INSTALL), PermissionResultDeny)


def test_an_always_to_the_shell_is_offered_at_the_end_of_the_turn(db, monkeypatch):
    gate = ShellGate(Clock())
    assert asked_shell(gate, "Yeah, always.") == [INSTALL]
    assert isinstance(ask(gate, INSTALL), PermissionResultAllow)
    said = gate.question("Installed it.")
    assert said == (" From now on, “brew install jq” exactly (installs software) would run without asking, for background "
                    "tasks and my own shell, in any folder. Save that rule?")
    assert conversation.current().offer.open["approval"] is None and conversation.current().offer.open["example"] == INSTALL
    assert remembered.listed() == []  # only the next words save it


def test_an_always_to_the_shell_for_what_always_asks_says_so(db):
    gate = ShellGate(Clock())
    assert asked_shell(gate, "Yes, always.", "rm -rf ~/Desktop/old") == ["rm -rf ~/Desktop/old"]
    assert gate.question("Deleted.") == " That yes was just for this once: a command that deletes files asks every time."


def test_dont_ask_again_alone_isnt_a_yes_to_the_shell(db):
    gate = ShellGate(Clock())
    assert asked_shell(gate, "Don't ask me again.") == []


def test_an_always_rule_lets_the_shell_run_it_straight_away(db):
    rule(INSTALL)
    gate = ShellGate(Clock())
    gate.new_turn("install jq")
    assert isinstance(ask(gate, INSTALL), PermissionResultAllow) and gate.asked == []


# ── Listing and withdrawing ─────────────────────────────────────


def test_the_brain_lists_and_withdraws_rules(db):
    assert task_tools.approval_rules() == "No remembered approvals: every risky command asks."
    [kept] = rule(INSTALL)
    [listed] = task_tools.approval_rules()
    assert listed == {"rule": kept["id"], "covers": kept["covers"], "who": kept["who"], "made_from": INSTALL, "uses": 0}
    assert task_tools.approval_rule_forget(kept["id"])["withdrawn"] and remembered.listed() == []
    forget = tools.get("approval_rule_forget")
    assert not forget.gated and forget.changes  # withdrawing only takes permission away; the bell lists it


def test_the_panel_lists_withdraws_and_answers_for_longer(db, monkeypatch):
    [kept] = rule(INSTALL)
    [shown] = asyncio.run(panel.call("approval_rules", {}))
    assert shown["covers"] == kept["covers"] and shown["scope"] == "always" and "words" not in shown
    assert asyncio.run(panel.call("approval_rule_forget", {"id": kept["id"]})) == []
    sent = {}

    async def answer(approval_id, decision, via, **kwargs):
        sent.update(kwargs, id=approval_id, via=via)
        return {"state": APPROVED, "outcome": "ok", "answered": True, "remembered": {"rules": []}}

    monkeypatch.setattr(client, "answer", answer)
    out = asyncio.run(panel.call("answer", {"id": 3, "decision": "approve", "remember": "always", "breadth": "kind"}))
    assert sent == {"id": 3, "via": "panel", "remember": "always", "breadth": "kind"} and out["remembered"] == {"rules": []}


def test_the_prompt_says_how_answers_go_further_and_what_always_asks():
    from jarvis.brain import persona
    assert "always ask, every time" in persona.persona() and "Never suggest an \"always\" yourself" in persona.persona()
    assert f"{profile.name()}" in persona.persona()
    assert "so just do it" in tools.get("approval_rule_forget").description
