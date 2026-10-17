"""Tests for jarvis.gates.draft_gate: Google sends/deletes (Chat, invites, Doc, Sheet tab) wait for a spoken yes."""

import asyncio

import pytest
from ai_armory.toolsets.google import common, drafts, pending
from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny, ResultMessage, StreamEvent

from jarvis import tools
from jarvis.brain import cli
from jarvis.config import APPROVAL_MINUTES
from jarvis.gates import draft_gate
from jarvis.state import memory


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def minutes(self, n):
        self.now += n * 60


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(drafts, "_clock", c)
    monkeypatch.setattr(drafts, "_DRAFTS", {})
    return c


@pytest.fixture
def gate(clock):
    return draft_gate.DraftGate(clock)


def chat_draft(text="I'll join at four", to="Alice Carter") -> str:
    """Chat draft as chat_draft keeps it: summary is recipient plus exact text."""
    return drafts.keep("chat_send", "work", f"To {to}: {text}.", f"chat to {to}: {text}", {"text": text}).id


def doc_draft() -> str:
    return drafts.keep("docs_delete", "work", "Delete 1 table from 'Plan': table 2.", "delete 1 table from Plan",
                       {}).id


def send(gate, draft_id, tool="chat_send"):
    return asyncio.run(gate(f"mcp__jarvis__{tool}", {"draft_id": draft_id}, None))


def asked(gate, draft_id, spoken="Here's the draft.", tool="chat_send"):
    """Try `draft_id` in a turn up to the question; returns what was said."""
    gate.new_turn("tell alice I'll join at four")
    assert isinstance(send(gate, draft_id, tool), PermissionResultDeny)
    return gate.question(spoken)


def test_the_question_reads_the_exact_draft_and_who_it_goes_to(gate):
    draft_id = chat_draft("I'll join at four, not five")
    assert asked(gate, draft_id) == " To Alice Carter: I'll join at four, not five. Shall I send it?"
    assert pending(draft_id).approved is False
    space = chat_draft("Deploying tonight", "Platform Dev, 40 people")
    assert asked(gate, space, "Okay?") == " To Platform Dev, 40 people: Deploying tonight. Shall I send it?"


def test_a_deletion_is_read_out_and_asked_about_too(gate):
    draft_id = doc_draft()
    assert asked(gate, draft_id, "", "docs_delete") == "Delete 1 table from 'Plan': table 2. Shall I go ahead?"
    assert gate.new_turn("Yes.") == [draft_gate.PREFIX + draft_id]
    assert isinstance(send(gate, draft_id, "docs_delete"), PermissionResultAllow) and pending(draft_id).approved


def test_an_email_is_read_out_with_everyone_it_goes_to_and_sent_only_after_a_yes(gate):
    from ai_armory.toolsets.google.gmail import gmail_draft_send

    drafted = gmail_draft_send("work", "Ana Rao <ana@acme.example>", "Lunch", "Tomorrow at one?",
                               bcc="bo@acme.example")
    assert asked(gate, drafted["draft_id"], "", "gmail_send") == (
        "Email to Ana Rao (ana@acme.example), bcc bo@acme.example, subject 'Lunch', from the work account: Tomorrow "
        "at one? Shall I send it?")
    assert gate.new_turn("Yes, send it.") == [draft_gate.PREFIX + drafted["draft_id"]]
    assert isinstance(send(gate, drafted["draft_id"], "gmail_send"), PermissionResultAllow)
    assert pending(drafted["draft_id"]).approved


@pytest.mark.parametrize("tool, ask", [("calendar_send_update", "Shall I send it?"),
                                       ("calendar_send_response", "Shall I send it?"),
                                       ("calendar_delete_event", "Shall I go ahead?")])
def test_calendar_changes_that_email_people_are_sends_and_a_deletion_asks_to_go_ahead(gate, tool, ask):
    draft_id = drafts.keep(tool, "work", "About 'Sync'.", "sync", {}).id
    assert asked(gate, draft_id, "", tool) == f"About 'Sync'. {ask}"


def test_a_clear_yes_lets_the_identical_send_through_once(gate):
    draft_id = chat_draft()
    asked(gate, draft_id)
    assert gate.new_turn("Yes, send it.") == [draft_gate.PREFIX + draft_id]
    assert isinstance(send(gate, draft_id), PermissionResultAllow)
    assert pending(draft_id).approved
    assert isinstance(send(gate, draft_id), PermissionResultDeny)  # a yes covers one send


@pytest.mark.parametrize("reply", ["No.", "Don't send it.", "What did Bob say?", "Make it half past four."])
def test_nothing_is_sent_without_a_yes(gate, reply):
    draft_id = chat_draft()
    asked(gate, draft_id)
    assert gate.new_turn(reply) == []
    assert isinstance(send(gate, draft_id), PermissionResultDeny) and not pending(draft_id).approved


def test_a_no_drops_the_question_and_talk_in_between_leaves_it_open(gate):
    draft_id = chat_draft()
    asked(gate, draft_id)
    gate.new_turn("What time is it?")
    gate.question("")
    assert gate.new_turn("Okay.") == []  # after other talk, a bare okay is ambiguous: not enough
    gate.question("")
    assert gate.new_turn("Yes, send it.") == [draft_gate.PREFIX + draft_id]
    other = chat_draft("Running late")
    asked(gate, other)
    gate.new_turn("No.")
    gate.question("")
    assert gate.new_turn("Yes.") == []


def test_the_question_lapses(gate, clock):
    draft_id = chat_draft()
    asked(gate, draft_id)
    gate.new_turn("What's the weather?")
    gate.question("")
    clock.minutes(APPROVAL_MINUTES + 1)
    assert gate.new_turn("Yes.") == []


def test_a_yes_after_an_edited_draft_sends_only_the_new_one(gate):
    old = chat_draft()
    asked(gate, old)
    new = chat_draft("I'll join at half past four")
    said = asked(gate, new)  # "make it half past four": a new draft, read out again
    assert "half past four" in said and "join at four" not in said
    assert gate.new_turn("Yes.") == [draft_gate.PREFIX + new]
    assert isinstance(send(gate, new), PermissionResultAllow)
    assert isinstance(send(gate, old), PermissionResultDeny) and not pending(old).approved


def test_an_unknown_or_mismatched_draft_is_refused_and_other_tools_meet_the_shell_gate(gate):
    assert "no draft nope for chat_send" in send(gate, "nope").message
    assert "Make it again with docs_draft_delete" in send(gate, chat_draft(), "docs_delete").message
    gate.new_turn("clear out the old renders")
    command = {"command": "rm -rf ~/Desktop/renders/old", "dangerouslyDisableSandbox": True}
    assert isinstance(asyncio.run(gate("Bash", command, None)), PermissionResultDeny)
    assert gate.question("") == "Okay to go ahead?"


def test_a_draft_and_a_command_are_asked_about_together(gate):
    draft_id = chat_draft()
    asked(gate, draft_id)
    gate.new_turn("send it and clear out the old renders")
    send(gate, draft_id)
    asyncio.run(gate("Bash", {"command": "rm -rf ~/Desktop/renders/old", "dangerouslyDisableSandbox": True}, None))
    assert gate.question("") == "To Alice Carter: I'll join at four. Shall I send it, and go ahead with the command?"


def test_a_lapsed_draft_isnt_asked_about(gate, clock):
    draft_id = chat_draft()
    gate.new_turn("tell alice")
    send(gate, draft_id)
    clock.minutes(2 * APPROVAL_MINUTES + 1)
    assert gate.question("") == "" and pending(draft_id) is None


def test_the_model_is_told_what_was_agreed_and_the_log_says_what_was_done(gate):
    draft_id = chat_draft()
    asked(gate, draft_id)
    agreed = gate.new_turn("Go ahead.")
    assert f"call chat_send with draft_id {draft_id}" in gate.agreed_hint(agreed)
    assert gate.described(agreed) == ["chat to Alice Carter: I'll join at four"]
    assert gate.described(["rm -rf x"]) == ["rm -rf x"]


def test_without_the_gates_approval_ai_armory_does_nothing(clock, monkeypatch):
    """Defence in depth: an unapproved draft isn't acted on even past the gate."""
    monkeypatch.setattr(common, "service", lambda *args, **kwargs: pytest.fail("no Google call without a yes"))
    for tool, draft_id in (("docs_delete", doc_draft()), ("chat_send", chat_draft())):
        content, is_error = asyncio.run(tools.run(tool, {"draft_id": draft_id}))
        assert is_error and content.startswith("Not done: this goes ahead only after"), tool


def text_event(text):
    return StreamEvent(uuid="1", session_id="s", event={"type": "content_block_delta",
                                                        "delta": {"type": "text_delta", "text": text}})


class SendingClient:
    """Fake ClaudeSDKClient: the model has drafted, then calls chat_send and meets the gate."""

    def __init__(self, gate, draft_id):
        self.gate, self.draft_id = gate, draft_id
        self.prompts, self.results = [], []

    async def query(self, prompt):
        self.prompts.append(prompt)

    async def receive_response(self):
        yield text_event("Here's the reply.")
        self.results.append(await self.gate("mcp__jarvis__chat_send", {"draft_id": self.draft_id}, None))
        yield ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False, num_turns=2,
                            session_id="s")


def test_the_voice_brain_reads_the_draft_aloud_and_sends_after_yes(clock, monkeypatch):
    logged = []
    monkeypatch.setattr(memory, "log_exchange",
                        lambda user, assistant, approved=(), interrupted=False: logged.append(list(approved)))
    brain = cli.CliBrain()
    draft_id = chat_draft()
    brain.client = client = SendingClient(brain.shell, draft_id)

    async def talk(text):
        return "".join([chunk async for chunk in brain.respond(text)])

    assert asyncio.run(talk("tell alice I'll join at four")) == \
        "Here's the reply. To Alice Carter: I'll join at four. Shall I send it?"
    assert isinstance(client.results[0], PermissionResultDeny)
    asyncio.run(talk("Yes."))
    assert f"draft_id {draft_id}" in client.prompts[1]
    assert isinstance(client.results[1], PermissionResultAllow) and pending(draft_id).approved
    assert logged == [[], ["chat to Alice Carter: I'll join at four"]]
