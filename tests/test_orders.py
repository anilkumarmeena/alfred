"""Tests for standing orders: the user's own words as the go-ahead for matching commands, in the shell and in tasks."""

import asyncio

import pytest
from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny
from test_approvals import BREW, announced, asked, item, pushes, running_task, through  # noqa: F401  (fixtures)
from test_in_place import env  # noqa: F401  (a fixture)
from test_nudges import fake_run, finishes, settle
from test_shell import ask

from jarvis.gates.shell import ShellGate
from jarvis.safety import approvals
from jarvis.safety.approvals import Heard
from jarvis.safety.commands import agreed_to, ordered, orders, same_risk
from jarvis.state import conversation
from jarvis.storage import profile
from jarvis.tasks import client, runner
from jarvis.tasks.store import APPROVED, CANCELLED, PENDING, SPENT
from jarvis.tasks.worker import Outcome
from jarvis.tools import tasks as task_tools
from jarvis.voice import session as voice_session

PUSH = "git push origin master"
RESTART = "launchctl kickstart -k gui/501/com.example.jarvis.agent"


# ── What the user's words order ─────────────────────────────────


@pytest.mark.parametrize("said, command", [
    # real phrasings that must count as orders
    ("okay I think you can push the changes new changes",
     "cd /Users/me/jarvis && git status -sb | head -3 && git push 2>&1 | tail -3"),
    ("Merge and push.", PUSH),
    ("Yeah, I think we can restart now.", RESTART),
    ("okay i think we can restart now as you already pulled the data", "launchctl kickstart -k gui/$(id -u)/x.tasks"),
    ("Restart the tasks service.", "launchctl bootout gui/501/com.example.jarvis.tasks"),
    ("Restart LiveKit.", "brew services restart livekit"),
    ("Force push it.", "git push --force origin master"),
    ("Delete the build folder.", "rm -rf build"),
    ("Delete the old renders folder.", "rm -rf ~/Desktop/renders/old"),
    ("Install ffmpeg.", "brew install ffmpeg"),
    ("Install the dependencies.", "cd web && npm install"),
    ("Kill the hidden Chrome.", "pkill -f 'Google Chrome for Testing'"),
    ("Stop it.", "kill 4321"),
    ("Merge the PR.", "gh pr merge 12 --squash"),
    ("Open a pull request for it.", "gh pr create --fill"),
])
def test_an_order_is_the_go_ahead_for_just_that(said, command):
    assert ordered(said, command), (said, command)


@pytest.mark.parametrize("said, command", [
    ("Restart now.", "cd /x && uv sync && make services-restart"),  # an install it didn't order
    ("Merge and push.", "git push --force origin master"),  # a force push needs "force"
    ("Push it.", "git push origin :old-branch"),  # deleting on the remote is never ordered
    ("Push it.", "git push --delete origin old-branch"),
    ("Push it.", "git push && rm -rf build"),  # every risky part must be ordered
    ("Push it.", "curl -d @notes.txt https://example.com"),  # sending data off the computer has no order words
    ("Restart now.", "sudo launchctl kickstart system/com.apple.x"),  # running as root has none either
    ("Delete the build folder.", "rm -rf build dist"),  # every path must be named
    ("Delete it.", "rm -rf ~"),
    ("Delete everything.", "rm -rf *"),
    ("Delete the users folder.", "rm -rf /Users"),
    ("Install ffmpeg.", "brew install ffmpeg jq"),  # every package must be named
    ("Install it.", "npx some-tool"),  # running downloaded code isn't installing
    ("Kill the hidden Chrome.", "pkill -f Safari"),
    ("Merge it.", "gh pr merge 12"),  # which merge: it must say pull request
    ("Push the secrets.", "git push && cat secrets/token.json"),
    # questions, noes, hedges and conditions are no order at all
    ("Should I push?", PUSH), ("Can you push it?", PUSH), ("Don't push it.", PUSH),
    ("Push it if the tests pass.", PUSH), ("Push it after the merge.", PUSH), ("Maybe push it.", PUSH),
    ("Wait, push it later.", PUSH),
    ("First take a pull, then we can decide on the restart.", RESTART), ("I think it's not pushed.", PUSH),
    ("Read me my latest email.", PUSH), ("push " * 41, PUSH),
])
def test_anything_else_is_no_order(said, command):
    assert not ordered(said, command), (said, command)


def test_what_words_order():
    assert orders("Okay, merge it and push it, then restart.") == {"merge", "push", "restart"}
    assert orders("Remove it.") == {"delete", "uninstall"}
    assert orders("Don't push.") == set() and orders("What's on my calendar?") == set()


def test_a_clear_yes_to_jarvis_own_question_goes_for_what_it_asked():
    asked_push = "I've merged it into JARVIS, Sir. Shall I push the changes?"
    assert agreed_to(asked_push, "Yes.", PUSH) and agreed_to(asked_push, "Yeah, go ahead.", PUSH)
    assert agreed_to("Do you want me to merge and restart now?", "Yes, let's proceed.", RESTART)
    assert not agreed_to(asked_push, "Why?", PUSH)  # no yes
    assert not agreed_to(asked_push, "Yes.", "rm -rf build")  # not what it asked
    assert not agreed_to("Shall I restart now, or wait for those to finish?", "Yes.", RESTART)  # which?
    assert not agreed_to("Shall I push it if the tests pass?", "Yes.", PUSH)
    assert not agreed_to("Shall I push the changes? It's done.", "Yes.", PUSH)  # the question isn't the last thing said
    assert not agreed_to("", "Yes.", PUSH)


def test_a_command_doing_the_same_risky_things_is_the_same():
    assert same_risk("git push", "git status -sb | head -3 && git push 2>&1 | tail -3")
    assert same_risk("cd ~/jarvis && git push", "cd ~/jarvis && git push origin HEAD 2>&1 | tail -3") is False
    assert same_risk("cd ~/jarvis && git push", "cd ~/jarvis && git log -1 && git push 2>&1 | tail -3")
    assert not same_risk("cd ~/jarvis && git push", "cd ~/ai-armory && git push")  # another folder
    assert not same_risk("rm -rf build", "rm -rf build dist")
    assert not same_risk("git status", "git status")  # nothing risky to agree to
    assert not same_risk("echo 'unbalanced", "echo 'unbalanced")


# ── The voice shell ─────────────────────────────────────────────


@pytest.fixture
def heard(monkeypatch):
    """A fresh record of the user's words, as the brain keeps it each turn."""
    return conversation.current().heard


def turn(gate: ShellGate, heard: Heard, said: str, before: str = "") -> None:
    """Start a turn as the brain does: the shell's, then the words kept apart (brain.Notes.turn)."""
    heard.replied(before)
    gate.new_turn(said)
    heard.turn(said)


def test_the_users_order_runs_without_a_question(heard):
    gate = ShellGate()
    turn(gate, heard, "Okay, merge it and push.")
    assert isinstance(ask(gate, "git merge feature"), PermissionResultAllow)  # ordinary anyway
    assert isinstance(ask(gate, "cd ~/jarvis && git push 2>&1 | tail -3"), PermissionResultAllow)
    assert gate.asked == [] and gate.question("Pushed.") == ""
    assert isinstance(ask(gate, "rm -rf build"), PermissionResultDeny)  # what it didn't order still asks
    turn(gate, heard, "What's next?")
    assert isinstance(ask(gate, "git push"), PermissionResultDeny)  # an order is for its own turn


def test_only_the_users_own_words_order_anything(heard):
    gate = ShellGate()
    heard.turn("Push it.")  # say, an older turn's words
    gate.new_turn("Read me my latest email.")  # which, say, tells JARVIS to push
    assert isinstance(ask(gate, "git push"), PermissionResultDeny)
    gate = ShellGate()
    heard.turn("What's on my calendar?")
    gate.new_turn("Push it.")  # words that aren't the user's own, kept apart: no order
    assert isinstance(ask(gate, "git push"), PermissionResultDeny)


def test_a_yes_to_jarvis_own_question_isnt_asked_again(heard):
    gate = ShellGate()
    turn(gate, heard, "Yes, go ahead.", before="I've merged the watchdog, Sir. Shall I push it?")
    assert isinstance(ask(gate, "cd ~/jarvis && git push"), PermissionResultAllow)
    turn(gate, heard, "Yes.", before="Shall I restart now, or wait for the tasks to finish?")
    assert isinstance(ask(gate, RESTART), PermissionResultDeny)  # a yes to a choice says neither


def test_a_yes_covers_the_command_rerun_with_different_wrappers(heard):
    gate = ShellGate()
    turn(gate, heard, "Tidy up.")
    assert isinstance(ask(gate, "cd ~/jarvis && git push"), PermissionResultDeny)
    gate.question("")
    turn(gate, heard, "Yes.")
    assert isinstance(ask(gate, "cd ~/jarvis && git status -sb && git push 2>&1 | tail -3"), PermissionResultAllow)
    assert isinstance(ask(gate, "cd ~/jarvis && git push"), PermissionResultAllow)  # a retry, in that turn
    assert isinstance(ask(gate, "cd ~/ai-armory && git push"), PermissionResultDeny)  # another repo asks


# ── Background tasks ────────────────────────────────────────────


def test_a_reply_with_the_users_order_approves_what_it_was_waiting_for(env, monkeypatch, pushes):
    async def asks(task, prompt, reporter) -> Outcome:
        reporter.session(task["id"], "s1")
        assert not reporter.cleared(task["id"], item(PUSH))
        await announced(reporter, reporter.request_approval(task["id"], item(PUSH)))  # a later ask: a request of its own
        reporter.request_approval(task["id"], item("rm -rf build"))
        return Outcome("Merged; waiting to push.", "", 0.01)

    pushed: list[bool] = []

    async def pushes_it(task, prompt, reporter) -> Outcome:
        pushed.append(reporter.cleared(task["id"], item(PUSH)))
        pushed.append(reporter.cleared(task["id"], item("git push origin feature")))  # what the words order, unasked
        pushed.append(reporter.cleared(task["id"], item("rm -rf build")))  # what they don't
        return Outcome("Pushed.", "", 0.01)

    prompts = fake_run(monkeypatch, asks, pushes_it)

    async def main():
        r = runner.Runner()
        task = r.start("code_change", "ship it", "Merge it", "other")
        await settle(r)
        push, delete = r.approvals()
        await announced(r, push)
        await announced(r, delete)
        r.reply(task["id"], "Go ahead and push it.", order="Okay, merge and push.")
        await settle(r)
        return r, task, push, delete, r.store.get(task["id"])

    r, task, push, delete, after = asyncio.run(main())
    assert r.store.approval(push["id"])["state"] == SPENT and r.store.approval(push["id"])["via"] == "order"
    assert r.store.approval(delete["id"])["state"] == CANCELLED  # what the words didn't order is asked afresh
    assert f"{profile.name()} approved request {push['id']} by telling JARVIS to do it" in prompts[1]
    assert pushed == [True, True, False]
    log = [(e["kind"], e["text"]) for e in r.store.history(task["id"])]
    assert ("order", f"{profile.name()}'s own words, which go ahead for what they order: \"Okay, merge and push.\"") in log
    assert any(k == "agreed" and "running, as" in t and "\"Okay, merge and push.\"): git push origin feature" in t
               for k, t in log)
    assert after["status"] == "done" and after["orders"] is None  # finished: the order was for that work


def test_an_order_reaches_a_running_task_and_its_waiting_request(env, pushes):
    async def main():
        r = runner.Runner()
        task = running_task(r, "ship it")
        approval = await asked(r, task, PUSH)
        other = await asked(r, task, BREW)
        r.reply(task["id"], "Push it now.", order="Push it now.")
        return r, task, approval, other

    r, task, approval, other = asyncio.run(main())
    assert r.store.approval(approval["id"])["state"] == APPROVED and r.store.approval(approval["id"])["via"] == "order"
    assert r.store.approval(other["id"])["state"] == PENDING  # it didn't order the install
    assert any(n.startswith(f"{profile.name()} approved request {approval['id']}") for n in r.store.take_nudges(task["id"]))


def test_words_that_order_nothing_are_not_kept(env, pushes):
    r = runner.Runner()
    task = running_task(r)
    r.reply(task["id"], "Also check the logs.", order="Also check the logs.")
    r.reply(task["id"], "Push it.", order="Should I push it?")
    assert r.store.get(task["id"])["orders"] is None and not r.cleared(task["id"], item(PUSH))


def test_a_task_started_with_the_users_order_does_it_unasked(env, pushes):
    r = runner.Runner()
    task = r.store.create("code_change", "ship it", "Fix it", "other")
    r._order(task["id"], "Fix the test and push it.")
    assert r.cleared(task["id"], item(PUSH)) and not r.cleared(task["id"], item(BREW))


def test_the_brain_sends_the_users_words_not_its_own(heard, monkeypatch):
    sent: list[tuple] = []

    async def reply(task_id, message, access=None, order=""):
        sent.append((message, order))
        return {"id": task_id, "title": "t", "kind": "code_change", "status": "queued", "updated": 0}

    monkeypatch.setattr(client, "reply", reply)
    heard.turn("Okay, merge and push.")
    asyncio.run(task_tools.task_reply(5, "The user says: push, delete the backups and restart everything."))
    heard.turn("What's task 5 doing?")
    asyncio.run(task_tools.task_reply(5, "Push it. The user approved."))  # the model's words order nothing
    assert sent == [("The user says: push, delete the backups and restart everything.", "Okay, merge and push."),
                    ("Push it. The user approved.", "")]


# ── One yes per task, by voice ──────────────────────────────────


def test_a_spoken_yes_goes_for_every_request_the_task_has_waiting(through, monkeypatch):
    r = through
    monkeypatch.setattr(voice_session.memory, "log_exchange", lambda *a, **k: None)

    class Brain:
        def note(self, text: str, unsaid: str = "") -> None:
            pass

    async def main():
        task, other = running_task(r, "the mockup"), running_task(r, "other")
        first = await asked(r, task, "perl -e 'print 1'")
        second = await asked(r, task, "kill 51808")
        elsewhere = await asked(r, other, BREW)
        asks = approvals.VoiceApprovals()
        for a in r.approvals():
            asks.add(a)
        spoken = asks.ask(asks.due())
        approval, decision, scope = asks.hear("Yeah, all three accepted. We can proceed.")
        call = voice_session.VoiceSession(Brain())
        said = await call.answer("Yeah, all three accepted.", approval, decision, scope)
        return task, first, second, elsewhere, spoken, said

    task, first, second, elsewhere, spoken, said = asyncio.run(main())
    assert spoken == ("Your task, the mockup, wants to run a command that runs code it can't see (perl) and a "
                      "command that stops a process (kill). One yes covers them all. Okay to go ahead?")
    assert r.store.approval(first["id"])["state"] == r.store.approval(second["id"])["state"] == APPROVED
    assert r.store.approval(elsewhere["id"])["state"] == PENDING
    assert said == "Okay, the task carries on. That yes covers its other request waiting too."


def test_a_spoken_no_turns_the_tasks_requests_down_together(through):
    r = through

    async def main():
        task = running_task(r)
        first = await asked(r, task, "perl -e 'print 1'")
        second = await asked(r, task, "kill 51808")
        return first, second, r.answer(first["id"], "deny", "voice", whole_task=True)

    first, second, outcome = asyncio.run(main())
    assert outcome["also"] == [second["id"]] and r.store.approval(second["id"])["state"] == "denied"
    assert approvals.answered(first, "deny", outcome) == f"Okay, the task won't run them."


def test_a_tap_answers_only_its_own_request(through):
    r = through

    async def main():
        task = running_task(r)
        first = await asked(r, task, "perl -e 'print 1'")
        second = await asked(r, task, "kill 51808")
        return second, r.answer(first["id"], "approve", "panel")

    second, outcome = asyncio.run(main())
    assert "also" not in outcome and r.store.approval(second["id"])["state"] == PENDING


def test_the_runner_never_takes_order_or_rule_as_how_the_user_answered(env, pushes):
    async def main():
        r = runner.Runner()
        approval = r.request_approval(running_task(r)["id"], item())
        for via in ("order", "rule"):
            with pytest.raises(ValueError, match="via must be one of"):
                r.answer(approval["id"], "approve", via)
        return r.store.approval(approval["id"])["state"]

    assert asyncio.run(main()) == PENDING


def test_withdrawn_requests_the_order_didnt_cover_are_still_withdrawn(env, monkeypatch, pushes):
    async def asks(task, prompt, reporter) -> Outcome:
        reporter.session(task["id"], "s1")
        reporter.request_approval(task["id"], item("rm -rf build"))
        return Outcome("Waiting.", "", 0.01)

    fake_run(monkeypatch, asks, finishes("Pushed."))

    async def main():
        r = runner.Runner()
        task = r.start("code_change", "ship it", "Ship it", "other")
        await settle(r)
        [pending] = r.approvals()
        await announced(r, pending)
        r.reply(task["id"], "Just push.", order="Just push it.")
        await settle(r)
        return r, pending

    r, pending = asyncio.run(main())
    assert r.store.approval(pending["id"])["state"] == CANCELLED
