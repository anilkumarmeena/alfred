"""Tests for task approval requests: asking, answering (CLI, phone, voice), what the task hears, lapsing."""

import asyncio
import hashlib
import json
import time

import pytest
from test_in_place import env  # noqa: F401  (a fixture)
from test_nudges import fake_run, finishes, settle, until

from jarvis import server, tools
from jarvis.brain.persona import Notes
from jarvis.brain.sdk import jarvis_server
from jarvis.gates import shell
from jarvis.notify import apns
from jarvis.safety import remembered
from jarvis.safety.approvals import Heard, VoiceApprovals, answered
from jarvis.safety.commands import describe, tells_approve, tells_deny
from jarvis.state import conversation
from jarvis.storage import profile
from jarvis.tasks import api, approvals, client, events as task_events, runner, worker
from jarvis.tasks.store import (
    APPROVED,
    CANCELLED,
    DENIED,
    EXPIRED,
    PENDING,
    SPENT,
    WAIT_APPROVAL,
    WAIT_QUESTION,
    nudges,
)
from jarvis.tasks.worker import Outcome
from jarvis.tools import tasks as task_tools

BREW = "brew install xcodegen"


def item(command: str = BREW, folder: str = "/repo", tool: str = "host_run") -> dict:
    """Host command in the shape host_tools.approval_item gives the runner."""
    return {"tool": tool, "command": command, "folder": folder, "why": describe(command),
            "remember": remembered.pieces(command)}


@pytest.fixture
def pushes(monkeypatch):
    """Pushes the runner sends, with approval batching cut to 0.05 s."""
    sent: list[dict] = []

    async def send_push(title, body, url="/", tag="", **extra):
        sent.append({"title": title, "body": body, "url": url, "tag": tag, **extra})
        return 1

    monkeypatch.setattr(task_events, "PUSH_ENABLED", True)
    monkeypatch.setattr(task_events, "send_push", send_push)
    monkeypatch.setattr(approvals, "APPROVAL_BATCH_SECONDS", 0.05)
    return sent


def events(r: runner.Runner) -> asyncio.Queue:
    q: asyncio.Queue = asyncio.Queue()
    r.listeners.add(q)
    return q


async def announced(r: runner.Runner, approval: dict) -> None:
    """Wait until request `approval` has been put to the user (or settled first), its push sent."""
    await until(lambda: (a := r.store.approval(approval["id"]))["announced"] or a["state"] != PENDING)
    await asyncio.sleep(0.01)  # the push it starts in the background goes out


def drained(q: asyncio.Queue) -> list[dict]:
    out = []
    while not q.empty():
        out.append(q.get_nowait())
    return out


# ── Asking ──────────────────────────────────────────────────────


def test_what_a_task_asks_at_once_is_one_request_announced_once(env, pushes):
    async def main():
        r = runner.Runner()
        q = events(r)
        task = r.store.create("code_change", "the iPhone build", "Build it", "other")
        first = r.request_approval(task["id"], item())
        second = r.request_approval(task["id"], item("rm -rf build"))
        again = r.request_approval(task["id"], item())  # the same command twice is asked about once
        await announced(r, first)
        return r, task, first, second, again, drained(q)

    r, task, first, second, again, sent = asyncio.run(main())
    assert first["id"] == second["id"] == again["id"]
    approval = r.store.approval(first["id"])
    assert [c["command"] for c in approval["commands"]] == [BREW, "rm -rf build"] and approval["announced"] == 1
    [event] = [e for e in sent if e["type"] == "approval"]
    assert event["text"] == ("Your task, the iPhone build, wants to run 2 commands: one that installs software "
                             "(brew install), one that deletes files (rm). Okay to go ahead?")
    assert "nonce_hash" not in event["approval"]
    [push] = pushes
    assert push["category"] == "JARVIS_APPROVAL" and push["tag"] == f"approval-{first['id']}"
    assert push["url"] == f"/?task={task['id']}&approval={first['id']}"
    assert push["body"].startswith(BREW) and "(and 1 more)" in push["body"] and push["subtitle"] == "installs software (brew install)"
    nonce = push["data"]["approval"]["nonce"]
    assert approval["nonce_hash"] == hashlib.sha256(nonce.encode()).hexdigest() and nonce not in json.dumps(approval)


def test_a_later_ask_is_a_new_request(env, pushes):
    async def main():
        r = runner.Runner()
        task = r.store.create("code_change", "build", "Build it", "other")
        first = r.request_approval(task["id"], item())
        await announced(r, first)
        return first, r.request_approval(task["id"], item("rm -rf build"))

    first, later = asyncio.run(main())
    assert later["id"] != first["id"]


# ── Answering, and what the task hears ──────────────────────────


def asks_then_ends(command: str = BREW):
    async def body(task, prompt, reporter) -> Outcome:
        reporter.session(task["id"], "s1")
        assert not reporter.cleared(task["id"], item(command))
        reporter.request_approval(task["id"], item(command))
        return Outcome("Waiting for the go-ahead to install xcodegen.", "", 0.01)

    return body


def test_a_task_waits_quietly_then_carries_on_with_a_yes(env, monkeypatch, pushes):
    ran: list[bool] = []

    async def runs_it(task, prompt, reporter) -> Outcome:
        ran.append(reporter.cleared(task["id"], item()))
        ran.append(reporter.cleared(task["id"], item()))  # same run again (e.g. after a failure): not re-asked
        return Outcome("Installed xcodegen and built the app.", "", 0.01)

    prompts = fake_run(monkeypatch, asks_then_ends(), runs_it)

    async def main():
        r = runner.Runner()
        q = events(r)
        task = r.start("code_change", "the iPhone build", "Build it", "other")
        await settle(r)
        waiting = r.store.get(task["id"])
        [pending] = r.approvals()
        await announced(r, pending)
        statuses = [e for e in drained(q) if e["type"] == "status" and e["task"]["status"] == "waiting"]
        answer = r.answer(pending["id"], "approve", "cli")
        await settle(r)
        return r, task, waiting, statuses, pending, answer

    r, task, waiting, statuses, pending, answer = asyncio.run(main())
    assert waiting["status"] == "waiting" and waiting["question"] == "may it run a command that installs software (brew install)?"
    assert waiting["wait_kind"] == WAIT_APPROVAL
    assert statuses == []  # only the request is announced, not the wait
    assert answer["outcome"] == f"Request {pending['id']} approved." and answer["state"] == APPROVED
    assert prompts[1] == (f"{profile.name()} approved request {pending['id']} on the command line. Run exactly this, once, with "
                          f"host_run, in /repo: {BREW}")
    assert ran == [True, True]
    assert r.store.get(task["id"])["status"] == "done"
    assert r.store.approval(pending["id"])["state"] == SPENT  # the yes covers that run only
    assert not r.cleared(task["id"], item())  # so a later run asks again
    asked, answered_push = [p for p in pushes if p["tag"] == f"approval-{pending['id']}"]
    assert asked["category"] == "JARVIS_APPROVAL" and "category" not in answered_push  # the answer push drops the buttons
    assert answered_push["title"] == f"Task {task['id']}: request approved"


def test_a_no_tells_the_task_not_to_run_it(env, monkeypatch, pushes):
    prompts = fake_run(monkeypatch, asks_then_ends(), finishes("Left the install to Alex."))

    async def main():
        r = runner.Runner()
        task = r.start("code_change", "build", "Build it", "other")
        await settle(r)
        [pending] = r.approvals()
        r.answer(pending["id"], "deny", "voice")
        await settle(r)
        return r, task

    r, task = asyncio.run(main())
    assert "declined request" in prompts[1] and "by voice" in prompts[1] and "Don't run it" in prompts[1]
    assert r.store.get(task["id"])["status"] == "done"


def test_a_running_task_hears_the_answer_at_its_next_step(env, monkeypatch, pushes):
    async def main():
        r = runner.Runner()
        task = r.store.create("code_change", "build", "Build it", "other")
        r.store.set(task["id"], status="running")
        approval = r.request_approval(task["id"], item())
        r.answer(approval["id"], "approve", "cli")
        return r.store.take_nudges(task["id"])

    [heard] = asyncio.run(main())
    assert heard.startswith(f"{profile.name()} approved request") and BREW in heard


def test_a_question_of_its_own_keeps_the_answer_for_its_reply(env, monkeypatch, pushes):
    async def main():
        r = runner.Runner()
        task = r.store.create("code_change", "build", "Build it", "other")
        r.store.set(task["id"], status="waiting", question="Which scheme?", wait_kind=WAIT_QUESTION)
        approval = r.request_approval(task["id"], item())
        r.answer(approval["id"], "approve", "cli")
        return r, task

    r, task = asyncio.run(main())
    assert r.store.get(task["id"])["status"] == "waiting" and "approved request" in r.store.take_nudges(task["id"])[0]


def test_what_a_task_waits_for_is_its_wait_kind_not_its_wording(env, monkeypatch, pushes):
    launched: list[int] = []
    monkeypatch.setattr(runner.Runner, "_launch", lambda self, task_id, prompt: launched.append(task_id))

    async def main():
        r = runner.Runner()
        asks = r.store.create("code_change", "build", "Build it", "other")  # its own question, worded like a request
        r.store.set(asks["id"], status="waiting", question="may it run the migration first?", wait_kind=WAIT_QUESTION)
        waits = r.store.create("code_change", "build", "Build it", "other")  # a request, worded like a question
        r.store.set(waits["id"], status="waiting", question="Shall I go on?", wait_kind=WAIT_APPROVAL)
        for task in (asks, waits):
            r.answer(r.request_approval(task["id"], item())["id"], "approve", "cli")
        return r, asks, waits

    r, asks, waits = asyncio.run(main())
    assert "approved request" in r.store.take_nudges(asks["id"])[0]  # kept for its reply to the question
    assert launched == [waits["id"]] and not r.store.take_nudges(waits["id"])  # carries on at once


def test_the_first_answer_wins_and_the_phone_needs_the_nonce(env, pushes):
    async def main():
        r = runner.Runner()
        task = r.store.create("code_change", "build", "Build it", "other")
        r.store.set(task["id"], status="running")
        approval = r.request_approval(task["id"], item())
        await announced(r, approval)
        nonce = pushes[0]["data"]["approval"]["nonce"]
        with pytest.raises(runner.NotAllowed):
            r.answer(approval["id"], "approve", "push", nonce="guess")
        with pytest.raises(runner.NotAllowed):
            r.answer(approval["id"], "approve", "push", nonce="")
        with pytest.raises(runner.NotAllowed):
            r.answer(approval["id"], "approve", "push")  # from the phone without a nonce
        first = r.answer(approval["id"], "deny", "push", nonce=nonce)
        second = r.answer(approval["id"], "approve", "cli")
        return first, second

    first, second = asyncio.run(main())
    assert first["state"] == DENIED and first["via"] == "push"
    assert second["state"] == DENIED and "already answered" in second["outcome"]


def test_stopping_a_task_drops_its_requests(env, monkeypatch, pushes):
    fake_run(monkeypatch, asks_then_ends())

    async def main():
        r = runner.Runner()
        task = r.start("code_change", "build", "Build it", "other")
        await settle(r)
        [pending] = r.approvals()
        r.stop(task["id"])
        return r, pending, r.answer(pending["id"], "approve", "cli")

    r, pending, late = asyncio.run(main())
    assert r.store.approval(pending["id"])["state"] == CANCELLED and "no longer needed" in late["outcome"]
    assert r.approvals() == []


def test_new_instructions_withdraw_what_it_was_waiting_for_and_it_isnt_asked_again(env, monkeypatch, pushes):
    prompts = fake_run(monkeypatch, asks_then_ends("rm -f /tmp/claude/mockshots/s1366.png"),
                       finishes("Overwrote the screenshots instead."))

    async def main():
        r = runner.Runner()
        q = events(r)
        task = r.start("code_change", "clip bar mockup", "Build it", "other")
        await settle(r)
        [pending] = r.approvals()
        await announced(r, pending)
        r.reply(task["id"], "Don't use rm for the temporary screenshots. Just overwrite them.")
        await settle(r)
        return r, task, pending, drained(q), r.answer(pending["id"], "approve", "cli")

    r, task, pending, sent, late = asyncio.run(main())
    withdrawn = r.store.approval(pending["id"])
    assert withdrawn["state"] == CANCELLED and withdrawn["via"] == "re-queued"
    assert r.approvals() == [] and "no longer needed" in late["outcome"] and not late["answered"]
    assert f"Request {pending['id']} was withdrawn unanswered: {profile.name()} sent the task new instructions" in prompts[1]
    assert "Don't use rm" in prompts[1]
    assert [e["approval"]["id"] for e in sent if e["type"] == "approval_answered"] == [pending["id"]]  # tells the voice side to drop it
    asks = VoiceApprovals()
    asks.add([e for e in sent if e["type"] == "approval"][0]["approval"])
    asks.drop(pending["id"])
    assert asks.due() is None
    buttons, replaced = [p for p in pushes if p["tag"] == f"approval-{pending['id']}"]
    assert "category" not in replaced and replaced["title"].endswith("request no longer needed")
    assert r.store.get(task["id"])["status"] == "done"


def test_an_answer_to_one_request_keeps_the_others_waiting(env, monkeypatch, pushes):
    async def asks_twice(task, prompt, reporter) -> Outcome:
        reporter.session(task["id"], "s1")
        await announced(reporter, reporter.request_approval(task["id"], item()))  # a later ask is a separate request
        reporter.request_approval(task["id"], item("rm -rf ~/old"))
        return Outcome("Waiting for both.", "", 0.01)

    async def waits_again(task, prompt, reporter) -> Outcome:
        return Outcome("Still waiting for the other.", "", 0.01)

    fake_run(monkeypatch, asks_twice, waits_again)

    async def main():
        r = runner.Runner()
        task = r.start("code_change", "build", "Build it", "other")
        await settle(r)
        first, second = r.approvals()
        r.answer(first["id"], "approve", "cli")
        await settle(r)
        return r, task, second

    r, task, second = asyncio.run(main())
    assert r.store.approval(second["id"])["state"] == PENDING and r.store.get(task["id"])["status"] == "waiting"


def test_a_withdrawn_request_says_why():
    approval = {"id": 9, "state": CANCELLED, "via": "done", "commands": [item()]}
    assert runner.approval_result(approval).startswith("Request 9 was withdrawn unanswered: the task finished.")


def test_a_request_nobody_answers_lapses_and_the_task_is_told(env, monkeypatch, pushes):
    prompts = fake_run(monkeypatch, asks_then_ends(), finishes("Here's the command to run yourself."))

    async def main():
        r = runner.Runner()
        r.start("code_change", "build", "Build it", "other")
        await settle(r)
        [pending] = r.approvals()
        r.store.set_approval(pending["id"], expires=time.time() - 1)
        expiry = asyncio.create_task(r._expire_approvals(every=0.01))
        await until(lambda: len(prompts) == 2)
        await settle(r)
        expiry.cancel()
        return r, pending

    r, pending = asyncio.run(main())
    assert r.store.approval(pending["id"])["state"] == EXPIRED
    assert "No answer to request" in prompts[1] and "exact command" in prompts[1]


def test_requests_a_restart_cut_short_are_announced_when_it_starts(env, pushes):
    async def main():
        r = runner.Runner()
        task = r.store.create("code_change", "build", "Build it", "other")
        r.store.ask_approval(task["id"], item(), time.time() + 60, 5)  # asked, but the runner stopped before announcing
        again = runner.Runner()
        again.start_background()
        await asyncio.sleep(0.1)
        for t in list(again._bg):
            t.cancel()

    asyncio.run(main())
    assert [p["category"] for p in pushes] == ["JARVIS_APPROVAL"]


def test_a_task_job_ending_goes_in_its_log(env):
    r = runner.Runner()
    task = r.store.create("code_change", "build", "Build it", "other")
    r.host_jobs.on_end({"id": 3, "title": "the release build", "task": task["id"]}, 65)
    assert r.store.history(task["id"])[-1]["text"] == "job 3, the release build, failed, with exit code 65"


# ── The API and the command line ────────────────────────────────


class Request:
    def __init__(self, approval_id: int | None = None, body: dict | None = None) -> None:
        self.match_info = {"id": str(approval_id)} if approval_id is not None else {}
        self.body, self.query = body or {}, {}

    async def json(self) -> dict:
        return self.body


def route(r: runner.Runner, method: str, path: str):
    return next(d.handler for d in api.routes(r) if d.method == method and d.path == path)


def test_the_runner_lists_and_answers_requests(env, pushes):
    async def main():
        r = runner.Runner()
        task = r.store.create("code_change", "build", "Build it", "other")
        r.store.set(task["id"], status="running")
        approval = r.request_approval(task["id"], item())
        listed = json.loads((await route(r, "GET", "/approvals")(Request())).text)
        denied = await api.errors(Request(approval["id"], {"decision": "approve", "via": "push", "nonce": "x"}),
                                      route(r, "POST", "/approvals/{id:\\d+}"))
        done = json.loads((await route(r, "POST", "/approvals/{id:\\d+}")(Request(approval["id"], {"decision": "approve"}))).text)
        return listed, denied, done

    listed, denied, done = asyncio.run(main())
    assert [a["summary"] for a in listed] == ["a command that installs software (brew install)"] and "spoken" in listed[0]
    assert denied.status == 403
    assert done["state"] == APPROVED and done["via"] == "cli"


def test_the_web_app_counts_wrong_nonces_towards_the_lockout(monkeypatch):
    calls = []

    async def answer(approval_id, decision, via, nonce=None):
        calls.append((approval_id, decision, via, nonce))
        if nonce != "right":
            raise client.NotAllowed("no")
        return {"state": APPROVED, "answered": True, "outcome": f"Request {approval_id} approved."}

    monkeypatch.setattr(client, "answer", answer)
    monkeypatch.setattr(server, "_failures", [])

    class WebRequest(Request):
        remote = "100.64.0.2"

    wrong = asyncio.run(server.approval(WebRequest(4, {"nonce": "guess", "decision": "approve"})))
    right = asyncio.run(server.approval(WebRequest(4, {"nonce": "right", "decision": "approve", "device": "iPhone"})))
    missing = asyncio.run(server.approval(WebRequest(4, {"decision": "approve"})))
    assert (wrong.status, right.status, missing.status) == (403, 200, 400)
    assert len(server._failures) == 1 and calls[-1] == (4, "approve", "push", "right")
    assert ("POST", "/api/approvals/{id}") in {(r.method, r.resource.canonical) for r in server.app().router.routes()}


def test_app_pushes_carry_the_approval_buttons_and_data():
    data = json.loads(apns.payload("Task 5 needs your go-ahead", BREW, "/?task=5&approval=12", "approval-12",
                                   subtitle="installs software (brew install)", category="JARVIS_APPROVAL",
                                   data={"approval": {"id": 12, "nonce": "n"}}))
    assert data["aps"]["category"] == "JARVIS_APPROVAL" and data["aps"]["alert"]["subtitle"].startswith("installs")
    assert data["approval"] == {"id": 12, "nonce": "n"} and data["url"] == "/?task=5&approval=12"
    plain = json.loads(apns.payload("t", "b", "/", "task-1"))
    assert "category" not in plain["aps"] and "subtitle" not in plain["aps"]["alert"]


# ── By voice ────────────────────────────────────────────────────


def request(n: int, task: int = 5) -> dict:
    return {"id": n, "task": task, "state": PENDING, "summary": "a command that installs software (brew install)",
            "spoken": f"Task {task} wants to run request {n}. Okay to go ahead?"}


def test_requests_are_asked_one_at_a_time_oldest_first():
    asks = VoiceApprovals()
    asks.add(request(8, task=6))
    asks.add(request(3))
    assert asks.due()["id"] == 3
    assert asks.ask(asks.due()) == "Task 5 wants to run request 3. Okay to go ahead?"
    assert asks.due() is None  # nothing more while one is open
    assert asks.hear("Yes, go ahead.")[1] == "approve"
    assert asks.due()["id"] == 8


def test_a_tasks_requests_waiting_are_asked_in_one_question():
    asks = VoiceApprovals()
    asks.add(request(3) | {"title": "the iPhone build"})
    asks.add(request(7, task=6))
    asks.add(request(4) | {"title": "the iPhone build", "summary": "a command that stops a process (kill)"})
    assert asks.ask(asks.due()) == ("Your task, the iPhone build, wants to run a command that installs software (brew "
                                    "install) and a command that stops a process (kill). One yes covers them all. Okay "
                                    "to go ahead?")
    approval, decision, scope = asks.hear("Yeah, all three accepted. We can proceed.")
    assert (approval["id"], approval["group"], decision) == (3, [3, 4], "approve")
    assert asks.due()["id"] == 7  # another task's is asked on its own
    asks.add(request(4))  # re-listed by the runner: already asked this call
    assert asks.due()["id"] == 7


def test_only_the_next_words_answer_and_each_request_is_asked_once_a_call():
    asks = VoiceApprovals()
    asks.add(request(3))
    asks.ask(asks.due())
    assert asks.hear("What's on my calendar?") is None
    assert asks.hear("Yes.") is None  # question withdrawn: this yes belongs to the next thing
    assert asks.due() is None
    asks.add(request(3))  # re-listed by the runner: not asked again this call
    assert asks.due() is None
    later = VoiceApprovals()  # the next call may ask once more
    later.add(request(3))
    later.ask(later.due())
    assert later.hear("No, don't.")[1] == "deny"


@pytest.mark.parametrize("said", ["Mm-hmm.", "Start going.", "Yeah, let's close the hidden Chrome. That's fine.",
                                  "I'm sure.", "Uh-huh", "Sure.", "Yeah, go ahead.", "That's fine.", "Okay, do it.",
                                  "Yes, let's proceed.", "Yeah, all three accepted. We can proceed.", "Sure, proceed.",
                                  "Yes please.", "You can go ahead."])
def test_a_natural_yes_answers_the_question(said):
    asks = VoiceApprovals()
    asks.ask(request(3))
    assert asks.hear(said)[1] == "approve"


@pytest.mark.parametrize("said", ["Thank you.", "Bye.", "Thanks, bye.", "Hmm."])
def test_a_thank_you_or_a_bye_isnt_a_yes(said):
    asks = VoiceApprovals()
    asks.ask(request(3))
    assert asks.hear(said) is None


@pytest.mark.parametrize("said", ["Uh-uh.", "No thanks.", "I'd rather not.", "Better not."])
def test_a_natural_no_answers_it_too(said):
    asks = VoiceApprovals()
    asks.ask(request(3))
    assert asks.hear(said)[1] == "deny"


def test_a_request_answered_elsewhere_isnt_asked_or_answered_here():
    asks = VoiceApprovals()
    asks.add(request(3))
    asks.drop(3)
    assert asks.due() is None
    asks.ask(request(4))
    asks.drop(4)
    assert asks.hear("Yes.") is None


def test_the_brains_own_question_comes_last():
    asks = VoiceApprovals()
    asks.ask(request(3))
    asks.close()
    assert asks.hear("Yes.") is None and asks.due() is None  # asked once already: the buttons have it now


def test_a_task_request_waits_while_the_voice_shell_asks():
    gate = shell.ShellGate()
    assert not gate.question_open()
    gate.new_turn("clear out the old renders")
    asyncio.run(gate("Bash", {"command": "rm -rf old", "dangerouslyDisableSandbox": True}, None))
    assert gate.question_open()
    gate.question("")
    gate.new_turn("hmm")
    assert gate.question_open()  # still open for a yes
    gate.new_turn("No.")
    assert not gate.question_open()


def test_what_jarvis_says_once_answered():
    assert answered(request(3), "approve", {"answered": True, "outcome": "Request 3 approved."}) == "Okay, the task carries on."
    assert answered(request(3), "deny", {"answered": True, "outcome": "Request 3 denied."}) == "Okay, the task won't run it."
    assert "already dealt with" in answered(request(3), "approve", {"answered": False, "outcome": "Request 3 was already answered."})


# ── Through the assistant ───────────────────────────────────────

CHROME = "pkill -f 'Google Chrome Helper'"


def test_what_counts_as_telling_jarvis_to_approve_or_deny():
    for said in ("Approve it.", "Let it close the hidden Chrome.", "Yes, go ahead with that delete.",
                 "OK, allow task 5 to run it", "Jarvis, give task seven the green light please"):
        assert tells_approve(said) and not tells_deny(said), said
    for said in ("Deny it.", "No, don't let it delete that.", "Decline task 5's request", "No.", "Don't approve it"):
        assert tells_deny(said) and not tells_approve(said), said
    for said in ("What will it delete?", "Approve it if it's safe", "Read me my latest email", "Wait, approve it later",
                 "Why does task 5 want to kill Chrome?", "yes " * 21):
        assert not tells_approve(said), said
    for said in ("Why does it want that?", "Read me my latest email", "Let it close the hidden Chrome."):
        assert not tells_deny(said), said


def test_the_users_words_answer_one_request_at_most():
    heard = Heard()
    assert heard.take("approve") is None  # nothing said yet
    heard.turn("  Approve it. ")
    assert heard.take("deny") is None  # not what they said, so not spent
    assert heard.take("approve") == "Approve it."
    assert heard.take("approve") is None  # one utterance, one request
    heard.turn("Read me my latest email.")
    assert heard.take("approve") is None and heard.take("deny") is None


def test_the_brain_keeps_the_users_words_as_each_turn_starts(monkeypatch):
    notes = Notes()
    notes.notes.append("Task 5, the Chrome cleanup, wants to run a command that stops processes (pkill).")
    prompt = notes.turn("Let it close the hidden Chrome.")
    assert conversation.current().heard.said == "Let it close the hidden Chrome."  # the user's words only, not the announcement
    assert "stops processes" in prompt


@pytest.fixture
def through(env, pushes, monkeypatch):
    """Runner wired in-process to task_approve and task_list."""
    r = runner.Runner()

    async def approvals_():
        return r.approvals()

    async def answer(approval_id, decision, via, nonce=None, task=None, commands=None, said=None, remember="once",
                     breadth="exact", whole_task=False):
        return r.answer(approval_id, decision, via, nonce, task, commands, said, remember, breadth, whole_task)

    monkeypatch.setattr(client, "approvals", approvals_)
    monkeypatch.setattr(client, "answer", answer)
    return r


def running_task(r: runner.Runner, title: str = "the Chrome cleanup") -> dict:
    task = r.store.create("code_change", title, "Tidy up", "other")
    r.store.set(task["id"], status="running")
    return task


async def asked(r: runner.Runner, task: dict, command: str) -> dict:
    """Request `command` and wait until it has been put to the user."""
    approval = r.request_approval(task["id"], item(command))
    await announced(r, approval)
    return approval


def test_the_assistant_passes_on_an_explicit_yes_for_that_one_request(through):
    r = through

    async def main():
        task = running_task(r)
        first = await asked(r, task, CHROME)
        conversation.current().heard.turn("Let it close the hidden Chrome.")
        done = await task_tools.task_approve(task["id"], True)
        second = await asked(r, task, "rm -rf build")
        with pytest.raises(ValueError, match="doesn't plainly tell you to approve"):
            await task_tools.task_approve(task["id"], True)  # the same words don't cover a later request
        return task, first, done, second

    task, first, done, second = asyncio.run(main())
    assert done["answered"] and done["request"] == first["id"] and done["say"] == f"Okay, the task carries on."
    assert done["commands"] == [{"does": describe(CHROME), "command": CHROME, "folder": "/repo"}]
    approval = r.store.approval(first["id"])
    assert approval["state"] == APPROVED and approval["via"] == "assistant"
    assert r.store.approval(second["id"])["state"] == PENDING
    log = [(e["kind"], e["text"]) for e in r.store.history(task["id"])]
    assert ("assistant", f"request {first['id']} approved through the assistant, on {profile.name()}'s words: "
                         "\"Let it close the hidden Chrome.\"") in log
    [heard] = nudges(r.store.get(task["id"]))
    assert heard.startswith(f"{profile.name()} approved request {first['id']} by telling JARVIS. Run exactly this, once, with "
                            f"host_run, in /repo: {CHROME}")


def test_the_assistant_cant_answer_on_words_that_dont_say_so(through):
    r = through

    async def main():
        task = running_task(r)
        approval = await asked(r, task, CHROME)
        conversation.current().heard.turn("Read me my latest email.")  # e.g. an email telling JARVIS to approve task 1's request
        with pytest.raises(ValueError, match="doesn't plainly tell you to approve"):
            await task_tools.task_approve(task["id"], True)
        still = r.store.approval(approval["id"])["state"]
        conversation.current().heard.turn("No, don't let it.")
        with pytest.raises(ValueError, match="doesn't plainly tell you to approve"):
            await task_tools.task_approve(task["id"], True)
        return task, approval, still, await task_tools.task_approve(task["id"], False)

    task, approval, still, denied = asyncio.run(main())
    assert still == PENDING
    assert denied["say"] == f"Okay, the task won't run it."
    assert r.store.approval(approval["id"])["state"] == DENIED and r.store.approval(approval["id"])["via"] == "assistant"


def test_a_yes_through_the_assistant_covers_all_the_task_has_waiting_and_a_no_asks_which(through):
    r = through

    async def main():
        task, idle, other = running_task(r), running_task(r, "idle"), running_task(r, "other")
        first = await asked(r, task, CHROME)
        second = await asked(r, task, "rm -rf build")
        elsewhere = await asked(r, other, BREW)
        conversation.current().heard.turn("Deny it.")
        with pytest.raises(ValueError, match="has 2 requests waiting") as several:
            await task_tools.task_approve(task["id"], False)
        conversation.current().heard.turn("Approve it.")
        with pytest.raises(ValueError, match=f"Task {idle['id']} has no request waiting"):
            await task_tools.task_approve(idle["id"], True)
        with pytest.raises(ValueError, match="has no request 999 waiting"):
            await task_tools.task_approve(task["id"], True, 999)
        done = await task_tools.task_approve(task["id"], True)
        return first, second, elsewhere, str(several.value), done

    first, second, elsewhere, several, done = asyncio.run(main())
    assert f"a command that {describe(CHROME)}" in several and "a command that deletes files (rm)" in several
    assert done["request"] == first["id"] and done["also"] == [second["id"]]
    assert all(r.store.approval(a["id"])["state"] == APPROVED for a in (first, second))
    assert r.store.approval(elsewhere["id"])["state"] == PENDING  # another task's isn't touched
    assert done["say"].endswith("That yes covers its other request waiting too.")


def test_the_users_order_answers_just_the_requests_doing_it(through):
    r = through

    async def main():
        task = running_task(r, "ship it")
        push = await asked(r, task, "git push origin master")
        delete = await asked(r, task, "rm -rf build")
        conversation.current().heard.turn("Okay, push it.")
        done = await task_tools.task_approve(task["id"], True)
        conversation.current().heard.turn("Restart now.")  # names nothing the task is waiting for
        with pytest.raises(ValueError, match="doesn't plainly tell you to approve"):
            await task_tools.task_approve(task["id"], True)
        return push, delete, done

    push, delete, done = asyncio.run(main())
    assert done["request"] == push["id"] and done["answered"] and done["also"] == []
    assert r.store.approval(push["id"])["state"] == APPROVED and r.store.approval(delete["id"])["state"] == PENDING


def test_the_runner_takes_an_answer_through_the_assistant_only_as_the_user_was_told(env, pushes):
    async def main():
        r = runner.Runner()
        task, other = running_task(r), running_task(r, "other")
        fresh = r.request_approval(task["id"], item(CHROME))
        unannounced = await api.errors(Request(fresh["id"], {"decision": "approve", "via": "assistant",
                                                                 "task": task["id"], "commands": fresh["commands"],
                                                                 "said": "approve it"}),
                                           route(r, "POST", "/approvals/{id:\\d+}"))
        await announced(r, fresh)
        approval = r.store.approval(fresh["id"])
        tries = [(other["id"], approval["commands"], "approve it"),
                 (task["id"], [item("rm -rf /")], "approve it"),
                 (task["id"], approval["commands"], "  "),
                 (task["id"], approval["commands"], None)]
        refused = []
        for task_id, commands, said in tries:
            with pytest.raises(runner.NotAllowed) as e:
                r.answer(approval["id"], "approve", "assistant", None, task_id, commands, said)
            refused.append(str(e.value))
        still = r.store.approval(approval["id"])["state"]
        body = {"decision": "deny", "via": "assistant", "task": task["id"], "commands": approval["commands"],
                "said": "Deny it."}
        done = json.loads((await route(r, "POST", "/approvals/{id:\\d+}")(Request(approval["id"], body))).text)
        return r, task, unannounced, refused, still, done

    r, task, unannounced, refused, still, done = asyncio.run(main())
    assert unannounced.status == 403 and f"hasn't been put to {profile.name()} yet" in json.loads(unannounced.text)["error"]
    assert "isn't task" in refused[0] and "isn't for those commands" in refused[1]
    assert f"carries {profile.name()}'s words" in refused[2] and f"carries {profile.name()}'s words" in refused[3]
    assert still == PENDING
    assert done["answered"] and done["state"] == DENIED and done["via"] == "assistant"
    assert any(e["kind"] == "assistant" and e["text"].endswith(f'on {profile.name()}\'s words: "Deny it."')
               for e in r.store.history(task["id"]))


def test_task_list_shows_each_tasks_waiting_requests():
    waiting = [{"id": 12, "task": 5, "announced": 1, "summary": "a command that stops processes (pkill)",
                "commands": [item(CHROME)]},
               {"id": 13, "task": 6, "announced": 1, "summary": "...", "commands": [item("rm -rf build")]}]
    task = {"id": 5, "title": "the Chrome cleanup", "kind": "code_change", "status": "waiting", "updated": time.time()}
    out = task_tools._brief(task, waiting)
    assert out["waiting_for_approval"] == [{"request": 12, "commands": [
        {"does": describe(CHROME), "command": CHROME, "folder": "/repo"}]}]
    assert "waiting_for_approval" not in task_tools._brief(task)


def test_the_approve_tool_is_the_voice_brains_alone():
    approve = next(t for t in tools.all_tools() if t.name == "task_approve")
    assert not approve.gated and "mcp__jarvis__task_approve" in jarvis_server()[1]  # allowed outright
    assert "task_approve" not in worker.READ_ONLY_TOOLS  # never available to tasks
    assert approve.required == ["id", "approve"]
