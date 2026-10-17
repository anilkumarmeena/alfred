"""Tests for claude.ask_json/ask_flag: schema-checked JSON verdicts; anything else is None, never keyword-matched."""

import asyncio
import logging
from types import SimpleNamespace

import pytest

from jarvis.llm import claude

SCHEMA = {"type": "object", "properties": {"met": {"type": "boolean"}, "said": {"type": "string"}},
          "required": ["met"]}


@pytest.fixture
def replies(monkeypatch):
    """Fake claude.ask: each call returns (or raises, or awaits) the next item of `replies.queue`.

    Records (prompt, system, model, options) in `replies.asked` and timeouts in `replies.timeouts`.
    """
    fake = SimpleNamespace(queue=[], asked=[], timeouts=[])

    async def ask(prompt, system_prompt, model, timeout=claude.ASK_SECONDS, **options):
        fake.asked.append((prompt, system_prompt, model, options))
        fake.timeouts.append(timeout)
        reply = fake.queue.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        if isinstance(reply, asyncio.Event):
            async with asyncio.timeout(timeout):
                await reply.wait()
        return reply, 0.01

    monkeypatch.setattr(claude, "ask", ask)
    return fake


@pytest.mark.parametrize("reply, found", [
    ('{"met": true, "said": "It shipped."}', {"met": True, "said": "It shipped."}),
    ('```json\n{"met": false}\n```', {"met": False}),  # in a code fence
    ('Here is my verdict: {"met": false, "extra": 1} Hope that helps.', {"met": False, "extra": 1}),
    ('{"outer": {"met": true}}', None),  # nested: the object itself must match
    ("MET: it shipped.", None),  # keywords are no verdict
    ('{"met": "yes"}', None),  # wrong type
    ('{"said": "It shipped."}', None),  # required field missing
    ('[{"met": true}]', {"met": True}),  # the object inside a list
    ('[true]', None),  # no object
    ("{not json}", None),
    ("", None),
])
def test_a_reply_is_json_matching_the_schema_or_none(reply, found):
    assert claude.json_reply(reply, SCHEMA) == found


def test_ask_json_puts_the_schema_to_the_model_and_returns_what_it_says(replies):
    replies.queue.append('{"met": true, "said": "It shipped."}')
    found, cost = asyncio.run(claude.ask_json("Did it ship?", SCHEMA, "You judge launches.", "claude-x", effort="low"))
    assert (found, cost) == ({"met": True, "said": "It shipped."}, 0.01)
    [(prompt, system, model, options)] = replies.asked
    assert (prompt, model, options) == ("Did it ship?", "claude-x", {"effort": "low"})
    assert system.startswith("You judge launches.\n\nReply with only a JSON object")
    assert '"required": ["met"]' in system  # the schema itself


def test_a_malformed_reply_is_none_and_logged_without_its_words(replies, caplog):
    replies.queue.append("Yes, the secret project shipped.")
    with caplog.at_level(logging.WARNING, logger="jarvis.llm.claude"):
        found, cost = asyncio.run(claude.ask_json("Did it ship?", SCHEMA, "s", "claude-x"))
    assert (found, cost) == (None, 0.01)
    assert "wasn't JSON matching its schema" in caplog.text and "secret project" not in caplog.text


def test_a_failed_call_raises_for_the_caller(replies):
    replies.queue.append(RuntimeError("the model call failed: error_during_execution"))
    with pytest.raises(RuntimeError):
        asyncio.run(claude.ask_json("Did it ship?", SCHEMA, "s", "claude-x"))


def test_ask_json_on_the_real_sdk_path(monkeypatch):
    import claude_agent_sdk
    from claude_agent_sdk import ResultMessage

    async def query(prompt, options):
        assert options.tools == [] and options.max_turns == 1 and "JSON schema" in options.system_prompt
        yield ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False, num_turns=1,
                            session_id="s", total_cost_usd=0.02, result='{"met": false}')

    monkeypatch.setattr(claude_agent_sdk, "query", query)
    assert asyncio.run(claude.ask_json("Did it ship?", SCHEMA, "s", "claude-x")) == ({"met": False}, 0.02)


@pytest.mark.parametrize("reply, answer", [
    ('{"rerun": true}', True),
    ('{"rerun": false}', False),
    ("RERUN: yes", None),  # not JSON: no answer, whatever the words
    ('{"rerun": "yes"}', None),
    (RuntimeError("overloaded"), None),  # the call failed
])
def test_ask_flag_answers_yes_no_or_none(replies, reply, answer):
    replies.queue.append(reply)
    assert asyncio.run(claude.ask_flag("Fixed it.", "rerun", "s", "claude-x", effort="low")) is answer
    [(_, system, _, options)] = replies.asked
    assert '"rerun": {"type": "boolean"}' in system and options == {"effort": "low"}


def test_ask_flag_gives_up_after_its_timeout(replies):
    replies.queue.append(asyncio.Event())  # never set: the call hangs
    assert asyncio.run(claude.ask_flag("Fixed it.", "rerun", "s", "claude-x", timeout=0.05)) is None
    assert replies.timeouts == [0.05]  # the call itself is bounded
