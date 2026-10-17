"""Tests for jarvis.gates.mcp_gate: what's read out for an MCP write call, and holding it for a spoken yes."""

import asyncio
import json

import pytest
from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

from jarvis.config import APPROVAL_MINUTES
from jarvis.gates import mcp_gate
from jarvis.integrations import mcp_servers
from jarvis.integrations.mcp_servers import Server, Tool
from jarvis.storage import profile

# GitHub and GitLab with hand-written `says` phrases, as plain server rows
GITHUB = Server("github", "GitHub", command="github-mcp-server", tools={
    "get_me": Tool("read"),
    "add_issue_comment": Tool("write", "comment on {owner}/{repo} number {issue_number}, saying {body}"),
    "pull_request_review_write": Tool("write", "{method} a review of {owner}/{repo} pull request {pullNumber}"),
    "create_pull_request": Tool("write", "open a pull request in {owner}/{repo} from {head} into {base}, titled {title}"),
    "create_or_update_file": Tool("write", "commit {path} to {owner}/{repo} on branch {branch}, with the message "
                                           "{message}"),
    "push_files": Tool("write", "push {files} to {owner}/{repo} on branch {branch}, with the message {message}"),
    "delete_repository": Tool("blocked")})
GITLAB = Server("gitlab", "GitLab", command="npx", args=["-y", "@zereight/mcp-gitlab@2.1.69"], tools={
    "create_merge_request_note": Tool("write", "comment on {project_id} merge request {merge_request_iid}, saying "
                                               "{body}"),
    "merge_merge_request": Tool("write", "merge {project_id} merge request {merge_request_iid}"),
    "create_branch": Tool("write", "create the branch {branch} in {project_id}[ from {ref}]"),
    "push_files": Tool("write", "push {files} to {project_id} on branch {branch}, with the message {commit_message}"),
    "delete_branch": Tool("blocked")})
ACME = Server("acme", "Acme", command="acme-mcp",
              tools={"find": Tool("read"), "post": Tool("write", "post to {channel}, saying {text}"),
                     "wipe": Tool("blocked"), "mark": Tool("own")})


# ── What JARVIS reads out ───────────────────────────────────────

def test_a_comment_is_read_out_in_full_with_where_it_goes():
    said = mcp_gate.describe(GITHUB, "add_issue_comment",
                             {"owner": "acme","repo": "jarvis", "issue_number": 12, "body": "Looks good,\n ship it"})
    assert said == "On GitHub, comment on acme/jarvis number 12, saying 'Looks good, ship it'."


def test_a_pull_request_names_its_branches_and_title():
    said = mcp_gate.describe(GITHUB, "create_pull_request", {"owner": "a", "repo": "b", "title": "Fix login",
                                                             "head": "fix-login", "base": "main", "draft": False})
    assert said == "On GitHub, open a pull request in a/b from fix-login into main, titled 'Fix login'."


def test_arguments_the_phrase_doesnt_name_are_said_after_it():
    said = mcp_gate.describe(GITLAB, "merge_merge_request", {"project_id": "web/app", "merge_request_iid": 7,
                                                             "squash": True, "should_remove_source_branch": True,
                                                             "jmespath": "id"})
    assert said == "On GitLab, merge web/app merge request 7, squash, should remove source branch."


def test_an_optional_part_drops_out_when_its_argument_is_missing():
    assert (mcp_gate.describe(GITLAB, "create_branch", {"project_id": "web/app", "branch": "fix-x", "ref": "main"})
            == "On GitLab, create the branch fix-x in web/app from main.")
    assert (mcp_gate.describe(GITLAB, "create_branch", {"project_id": "web/app", "branch": "fix-x"})
            == "On GitLab, create the branch fix-x in web/app.")


def test_files_are_named_and_counted_never_their_content():
    files = [{"file_path": f"src/{n}.py", "content": "x" * 50} for n in "abcdefg"]
    said = mcp_gate.describe(GITLAB, "push_files", {"project_id": 3, "branch": "fix-x", "files": files,
                                                    "commit_message": "Tidy up"})
    assert said == ("On GitLab, push 7 files (src/a.py, src/b.py, src/c.py, src/d.py, src/e.py and 2 more) to 3 on "
                    "branch fix-x, with the message 'Tidy up'.")
    one = mcp_gate.describe(GITHUB, "create_or_update_file", {"owner": "a", "repo": "b", "path": "README.md",
                                                              "branch": "main", "message": "Typo", "content": "x" * 900})
    assert one.endswith("content 900 characters.") and "xxx" not in one


def test_review_verdicts_and_methods_are_said_plainly():
    said = mcp_gate.describe(GITHUB, "pull_request_review_write", {"method": "create", "owner": "a", "repo": "b",
                                                                   "pullNumber": 5, "event": "REQUEST_CHANGES"})
    assert said == "On GitHub, create a review of a/b pull request 5, event request changes."


def test_a_long_body_is_read_in_part_and_the_rest_counted():
    said = mcp_gate.describe(GITHUB, "add_issue_comment", {"owner": "a", "repo": "b", "issue_number": 1,
                                                           "body": "word " * 200})
    assert "…', and 140 more words." in said and len(said) < 400


def test_a_tool_without_a_phrase_still_says_everything_it_carries():
    server = Server("acme", "Acme", command="x", tools={"send_it": Tool("write")})
    assert mcp_gate.describe(server, "send_it", {"to": "Bob", "text": "hi there"}) == \
        "On Acme, send it, to Bob, text 'hi there'."


def test_a_tool_sorted_from_its_markings_is_read_out_by_its_own_title_with_every_argument():
    tools = mcp_servers.sort([{"name": "create_page", "title": "Create a page", "annotations": {"readOnlyHint": False}}])
    server = Server("notion", "Notion", transport="http", url="https://mcp.notion.example/mcp", tools=tools)
    assert mcp_gate.describe(server, "create_page", {"parent": "Projects", "title": "Q4 plan"}) == \
        "On Notion, create a page, parent Projects, title 'Q4 plan'."


# ── The gate ────────────────────────────────────────────────────

class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


COMMENT = GITLAB.tool("create_merge_request_note")
ARGS = {"project_id": "web/app", "merge_request_iid": 7, "body": "LGTM"}


@pytest.fixture
def gate():
    g = mcp_gate.Gate(Clock())
    g.use([GITHUB, GITLAB, ACME])
    return g


def call(gate, name=COMMENT, args=ARGS):
    return asyncio.run(gate(name, dict(args), None))


def asked(gate, spoken="On it."):
    gate.new_turn("say LGTM on merge request seven")
    assert isinstance(call(gate), PermissionResultDeny)
    return gate.question(spoken)


def test_a_write_is_held_and_read_out_from_its_own_arguments(gate):
    denied = call(gate)
    assert isinstance(denied, PermissionResultDeny) and "go-ahead" in denied.message
    assert gate.question("On it.") == " On GitLab, comment on web/app merge request 7, saying LGTM. Okay to go ahead?"


def test_the_question_is_always_read_even_when_the_reply_asked_one(gate):
    call(gate)
    assert gate.question("Shall I? ").startswith("On GitLab, comment on")


def test_a_clear_yes_lets_the_identical_call_through_once(gate):
    asked(gate)
    assert len(gate.new_turn("Yes, go ahead.")) == 1
    assert isinstance(call(gate), PermissionResultAllow)
    assert isinstance(call(gate), PermissionResultDeny)  # a yes covers one call


def test_a_yes_covers_only_those_exact_arguments(gate):
    asked(gate)
    gate.new_turn("Yes.")
    assert isinstance(call(gate, args=ARGS | {"body": "LGTM, merging now"}), PermissionResultDeny)
    assert isinstance(call(gate, args=dict(reversed(ARGS.items()))), PermissionResultAllow)  # key order doesn't matter


@pytest.mark.parametrize("reply", ["No.", "Don't post that.", "What did Bob say?", "Make it say ship it."])
def test_nothing_goes_without_a_yes(gate, reply):
    asked(gate)
    assert gate.new_turn(reply) == []
    assert isinstance(call(gate), PermissionResultDeny)


def test_the_question_lapses(gate):
    asked(gate)
    gate.new_turn("What's the weather?")
    gate.question("")
    gate.clock.now += (APPROVAL_MINUTES + 1) * 60
    assert gate.new_turn("Yes.") == []


def test_blocked_unlisted_and_read_tools_are_refused_here(gate):
    for name in (GITHUB.tool("delete_repository"), GITLAB.tool("delete_branch"), ACME.tool("wipe"),
                 ACME.tool("something_new"), GITHUB.tool("get_me"), "mcp__unknown__thing"):
        assert "isn't available" in call(gate, name, {}).message
    assert gate.question("") == ""  # nothing to ask about


def test_a_server_added_later_is_gated_from_the_next_session(gate):
    gate.use([GITHUB])
    assert "isn't available" in call(gate, ACME.tool("post"), {"channel": "ops", "text": "hi"}).message
    gate.use([GITHUB, ACME])
    call(gate, ACME.tool("post"), {"channel": "ops", "text": "hi all"})
    assert gate.question("") == "On Acme, post to ops, saying 'hi all'. Okay to go ahead?"


def test_jarvis_own_tools_still_meet_the_draft_and_shell_gates(gate):
    gate.new_turn("clear out the renders")
    command = {"command": "rm -rf ~/Desktop/renders/old", "dangerouslyDisableSandbox": True}
    assert isinstance(call(gate, "Bash", command), PermissionResultDeny)
    assert gate.question("") == "Okay to go ahead?"
    assert "no draft nope for chat_send" in call(gate, "mcp__jarvis__chat_send", {"draft_id": "nope"}).message
    assert "no draft nope for docs_delete" in call(gate, "mcp__jarvis__docs_delete", {"draft_id": "nope"}).message


def test_a_write_and_a_risky_command_in_one_turn_are_both_asked_about(gate):
    gate.new_turn("comment and clear out the renders")
    call(gate)
    call(gate, "Bash", {"command": "rm -rf ~/Desktop/renders/old", "dangerouslyDisableSandbox": True})
    said = gate.question("")
    assert said.startswith("On GitLab, comment on web/app") and said.endswith("Okay to go ahead?")
    assert len(gate.new_turn("Yes.")) == 2


def test_the_model_is_told_the_exact_call_and_the_log_says_what_was_done(gate):
    asked(gate)
    agreed = gate.new_turn("Go ahead.")
    hint = gate.agreed_hint(agreed)
    assert hint.startswith(f"[{profile.name()} has said yes to this: On GitLab, comment on web/app")
    assert f"call {COMMENT} again with exactly these arguments: {json.dumps(ARGS, sort_keys=True)}" in hint
    assert gate.described(agreed) == ["On GitLab, comment on web/app merge request 7, saying LGTM."]
    assert gate.described(["rm -rf x"]) == ["rm -rf x"]


def test_long_arguments_arent_repeated_to_the_model(gate):
    push = {"owner": "a", "repo": "b", "branch": "main", "files": [{"path": "big.txt", "content": "x" * 5000}],
            "message": "Add"}
    gate.new_turn("push it")
    call(gate, GITHUB.tool("push_files"), push)
    gate.question("")
    hint = gate.agreed_hint(gate.new_turn("Yes."))
    assert "exactly the same arguments as before" in hint and "xxxx" not in hint
    assert isinstance(call(gate, GITHUB.tool("push_files"), push), PermissionResultAllow)
