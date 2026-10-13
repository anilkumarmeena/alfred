"""Tests for tools.google: Armory tools and names, settings handed over, gated tools, and Google sign-in."""

import asyncio

import pytest
from ai_armory.toolsets.google import GoogleSettings, current

from conftest import use_profile
from jarvis import tools
from jarvis.brain import cli
from jarvis.core import config, profile
from jarvis.integrations import accounts
from jarvis.safety import draft_gate
from jarvis.safety.commands import JARVIS_SECRETS
from jarvis.tasks import worker
from jarvis.tools import google

TOOLS = {
    "gmail": {"gmail_search", "gmail_read", "gmail_create_draft", "gmail_modify", "gmail_draft_send",
              "gmail_draft_reply", "gmail_send"},
    "calendar": {"calendar_events", "calendar_event", "calendar_free_time", "calendar_create_event",
                 "calendar_draft_invite", "calendar_send_invite", "calendar_update_event", "calendar_draft_update",
                 "calendar_send_update", "calendar_draft_respond", "calendar_send_response", "calendar_draft_delete",
                 "calendar_delete_event"},
    "chat": {"chat_unread", "chat_spaces", "chat_read", "chat_search", "chat_mark_read", "chat_draft", "chat_send"},
    "docs": {"docs_search", "docs_read", "docs_beautify", "docs_format", "docs_replace_text", "docs_insert",
             "docs_insert_image", "docs_draft_delete", "docs_delete"},
    "sheets": {"sheets_search", "sheets_read", "sheets_write", "sheets_append", "sheets_add_tab", "sheets_format",
               "sheets_draft_delete_tab", "sheets_delete_tab"},
    "slides": {"slides_search", "slides_read"},
    "drive": {"drive_search"},
}
GATED = {"gmail_send", "chat_send", "calendar_send_invite", "calendar_send_update", "calendar_send_response",
         "calendar_delete_event", "docs_delete", "sheets_delete_tab"}
# JARVIS's own attachment tools (jarvis.tools.files)
OWN = {"gmail_attachments", "gmail_attachment_read", "gmail_attachment_transcribe"}


def google_tools() -> dict[str, tools.Tool]:
    return {t.name: t for t in tools.all_tools() if t.armory and t.name.startswith(tuple(TOOLS))}


def test_every_google_tool_comes_from_ai_armory_under_its_own_name():
    assert set(google_tools()) == set().union(*TOOLS.values())
    assert not any(n.startswith(tuple(TOOLS)) for n, t in {t.name: t for t in tools.all_tools()}.items()
                   if not t.armory and n not in OWN)  # no copy of JARVIS's own is left


def test_only_sends_and_deletes_wait_for_a_yes_and_email_is_sent_only_that_way():
    assert {n for n, t in google_tools().items() if t.gated} == GATED
    for name in GATED:  # each takes only a draft's id, so what's done is what was read out
        assert list(google_tools()[name].properties) == ["draft_id"]
    assert TOOLS["gmail"] & GATED == {"gmail_send"}  # what sends email
    assert set(draft_gate.DRAFTED_BY) == GATED  # each says how to draft it again
    assert draft_gate.SENDS == GATED - {"calendar_delete_event", "docs_delete", "sheets_delete_tab"}


def test_the_settings_are_jarvis_accounts_sign_ins_and_wording():
    s = current()
    assert isinstance(s, GoogleSettings) and s == google.settings(accounts.load())
    assert [(a.label, a.email, a.description, a.chat) for a in s.accounts] == [
        ("work", "me@acme.example", "for work", True), ("personal", "me@mail.example", "their own", False)]
    assert s.default_label == "work" and s.chat_labels == google.CHAT_LABELS == ["work"]
    assert s.token_dir == profile.current().secrets and s.timezone == profile.current().timezone and s.user_name == profile.name()
    assert s.sign_in("work") == "the Sign in button beside the work account on the control panel's Google accounts page"
    assert s.image_folder == "JARVIS doc images" and s.draft_minutes == 2 * config.APPROVAL_MINUTES
    assert s.host_approval  # a send or delete goes through only for a draft the gate approved
    assert {str(p) for p in JARVIS_SECRETS} <= set(s.blocked_paths)


def test_the_cli_brain_gets_every_google_tool_but_sends_and_deletes_only_through_its_gate():
    brain = cli.CliBrain()
    opts = cli._options(brain.shell, brain.jobs)
    for name in set().union(*TOOLS.values()) - GATED:
        assert f"mcp__jarvis__{name}" in opts.allowed_tools, name
    assert not {f"mcp__jarvis__{n}" for n in GATED} & set(opts.allowed_tools)  # so every call reaches the gate
    others = {"mcp__jarvis__skill_confirm", "mcp__jarvis__connection_confirm"}  # skills' and connections'
    assert draft_gate.gated() - others == {f"mcp__jarvis__{n}" for n in GATED}
    assert isinstance(brain.shell, draft_gate.DraftGate) and opts.can_use_tool is brain.shell


def test_an_account_signed_in_mid_conversation_restarts_the_voice_cli_to_take_it(voice, own_profile):
    v = voice()

    async def main():
        await v.say("any new mail?")
        await v.say("and now?")
        with accounts.change() as found:
            found.add(accounts.Account("work", "me@acme.example"))
        await v.say("I've connected it, check again")
        await v.say("anything from Priya?")

    asyncio.run(main())
    assert len(v.clis) == 2 and v.clis[0].closed and not v.clis[1].closed
    assert len(v.clis[0].prompts) == 2 and len(v.clis[1].prompts) == 2
    assert google.LABELS == ["work"]


def test_the_brain_gets_ai_armorys_own_schemas():
    assert tools.input_schema(google_tools()["gmail_search"]) == google_tools()["gmail_search"].armory.input_schema


def test_research_tasks_only_read():
    assert set(worker.READ_ONLY_TOOLS) <= set(t.name for t in tools.all_tools())
    reads = {"calendar_events", "calendar_event", "calendar_free_time", "gmail_search", "gmail_read",
             "sheets_search", "sheets_read", "docs_search", "docs_read", "slides_search", "slides_read",
             "drive_search", "chat_unread", "chat_spaces", "chat_read", "chat_search"}
    assert reads <= set(worker.READ_ONLY_TOOLS)
    assert all(google_tools()[n].armory.read_only for n in worker.READ_ONLY_TOOLS if n in google_tools())
    assert not {n for n in worker.READ_ONLY_TOOLS if "draft" in n or n in GATED or n == "chat_mark_read"}


def test_bad_input_is_refused_before_anything_runs():
    content, is_error = asyncio.run(tools.run("gmail_read", {"message_id": 3}))
    assert is_error and "field 'message_id' must be string" in content
    content, is_error = asyncio.run(tools.run("gmail_read", ["not", "an", "object"]))
    assert is_error and "input must be an object" in content


# ── Signing in ──────────────────────────────────────────────────

def test_checking_chat_on_an_account_without_it_says_how_to_turn_it_on():
    with pytest.raises(SystemExit, match="uv run python -m jarvis.integrations.accounts set personal --chat"):
        accounts._check_chat("personal")
