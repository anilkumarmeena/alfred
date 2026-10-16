"""Tests for jarvis.brain.budget: a spoken turn does only a little work itself and hands the rest off."""

import asyncio

from claude_agent_sdk import ResultMessage, StreamEvent

from jarvis.brain import budget, cli
from jarvis.safety import mcp_gate, shell
from jarvis.state import memory

HEALTH_CHECK = ("uptime; echo ---; memory_pressure | tail -3; echo ---; sysctl vm.swapusage; echo ---; df -h / | "
                "tail -1; echo ---; ps -Ao rss,comm -m | head -6; echo ---; softwareupdate -l 2>&1 | tail -4")


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def decision(b: budget.Budget, name: str, args: dict | None = None) -> str:
    """'deny' with its reason, or '' when the call may run."""
    out = asyncio.run(b.hook[0].hooks[0]({"tool_name": name, "tool_input": args or {}}, "t", None))
    return f"deny: {out['hookSpecificOutput']['permissionDecisionReason']}" if out else ""


def test_past_a_few_calls_a_turn_s_own_work_is_refused_and_handed_off():
    b = budget.Budget(tools=3, seconds=60, clock=Clock())
    for name in ("mcp__jarvis__calendar_list", "mcp__jarvis__gmail_search", "WebSearch"):
        assert decision(b, name) == "", name
    refused = decision(b, "WebFetch")
    assert refused.startswith("deny") and "computer_lookup" in refused and "task_start" in refused


def test_a_turn_that_has_run_long_hands_the_rest_off():
    clock = Clock()
    b = budget.Budget(tools=3, seconds=15, clock=clock)
    assert decision(b, "WebSearch") == ""
    clock.now = 16
    assert "already taken 15 seconds" in decision(b, "mcp__jarvis__task_list")


def test_hand_offs_always_run_and_don_t_spend_the_turn():
    clock = Clock()
    b = budget.Budget(tools=1, seconds=15, clock=clock)
    clock.now = 99
    for name in budget.HAND_OFFS:
        assert decision(b, name) == "", name
    assert b.used == 0


def test_each_turn_starts_with_a_fresh_budget():
    clock = Clock()
    b = budget.Budget(tools=1, seconds=15, clock=clock)
    assert decision(b, "WebSearch") == ""
    clock.now = 20
    assert decision(b, "WebSearch").startswith("deny")
    b.new_turn()
    assert decision(b, "WebSearch") == "" and decision(b, "WebSearch").startswith("deny")


def test_a_shell_line_chaining_several_checks_goes_to_computer_lookup():
    b = budget.Budget(tools=3, seconds=60, clock=Clock())
    refused = decision(b, "Bash", {"command": HEALTH_CHECK})
    assert "computer_lookup" in refused and "runs 6 programs" in refused and b.used == 0
    for one in ("git add -A && git commit -m 'Fix the port'", "cd ~/app && git status | head -5",
                "ps aux | grep -i livekit | wc -l"):
        assert budget.chained(one) == [], one
    assert decision(b, "Bash", {"command": "git push"}) == ""


class OneTurnClient:
    async def query(self, prompt) -> None: ...

    async def receive_response(self):
        yield StreamEvent(uuid="1", session_id="s",
                          event={"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Sure."}})
        yield ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False, num_turns=1,
                            session_id="s")


def test_the_voice_brain_checks_every_call_against_a_budget_reset_each_turn(monkeypatch):
    monkeypatch.setattr(memory, "log_exchange", lambda *a, **k: None)
    monkeypatch.setattr(memory, "log_announcement", lambda *a, **k: None)
    brain = cli.CliBrain()
    hooks = cli._options(mcp_gate.Gate(), shell.Jobs(), budget=brain.budget).hooks["PreToolUse"]
    brain.budget.used = brain.budget.tools
    answers = [asyncio.run(h.hooks[0]({"tool_name": "WebSearch", "tool_input": {}}, "t", None)) for h in hooks]
    assert any("this reply has already made" in str(a) for a in answers)  # the brain's own budget is checked

    async def main():
        brain.client = OneTurnClient()
        said = "".join([c async for c in brain.respond("is the web server up?")])
        fresh = brain.budget.used
        brain.budget.used = brain.budget.tools
        followed = "".join([c async for c in brain.follow_up("[The merge finished.]")])
        return said, fresh, followed

    said, fresh, followed = asyncio.run(main())
    assert said == followed == "Sure." and fresh == 0 and brain.budget.used == 0
