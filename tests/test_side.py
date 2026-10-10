"""Tests for slow work in the background (jarvis.brain.side): reading the brain hands to a side job is announced when
it's done while the conversation carries on, side jobs can only read, and work cut off by the user speaking is
offered to the next turn to hand over. Run with: make test"""

import asyncio

import pytest
from claude_agent_sdk import AssistantMessage, ResultMessage, StreamEvent, ToolResultBlock, ToolUseBlock, UserMessage

from jarvis import tools
from jarvis.brain import cli, side
from jarvis.state import conversation, memory


def jobs(run) -> tuple[side.Jobs, list[tuple[str, str]]]:
    said: list[tuple[str, str]] = []
    return side.Jobs(lambda text, note: said.append((text, note)), run), said


def test_a_side_job_is_announced_when_its_answer_is_in():
    async def run(brief: str) -> str:
        await asyncio.sleep(0.01)
        return "Two flights leave Heathrow before nine, the cheaper at 7:40."

    j, said = jobs(run)

    async def main():
        told = j.start("Find morning flights from London to Lisbon tomorrow.", "flights to Lisbon")
        assert "background" in told and j.running
        await asyncio.sleep(0.05)
        return told

    asyncio.run(main())
    [(text, note)] = said
    assert text == "On flights to Lisbon: Two flights leave Heathrow before nine, the cheaper at 7:40."
    assert "background lookup on flights to Lisbon came back" in note and not j.running


def test_too_many_at_once_is_refused_and_failures_and_slow_ones_are_said(monkeypatch):
    gate = asyncio.Event()

    async def run(brief: str) -> str:
        if brief == "fail":
            raise RuntimeError("no")
        await gate.wait()
        return "done"

    j, said = jobs(run)
    monkeypatch.setattr(side, "SIDE_MINUTES", 0.001)

    async def main():
        j.start("slow", "one")
        j.start("slow", "two")
        refused = j.start("slow", "three")
        await asyncio.sleep(0.2)
        j.start("fail", "four")
        await asyncio.sleep(0.01)
        return refused

    refused = asyncio.run(main())
    assert refused.startswith("Not started") and "one, two" in refused
    assert [t for t, _ in said] == ["The lookup on one took too long, so I stopped it.",
                                    "The lookup on two took too long, so I stopped it.",
                                    "The lookup on four didn't work."]


def test_the_conversation_ending_stops_its_jobs():
    async def run(brief: str) -> str:
        await asyncio.Event().wait()

    j, said = jobs(run)

    async def main():
        j.start("forever", "it")
        await asyncio.sleep(0)
        await j.close()
        return j.running

    assert asyncio.run(main()) == {} and said == []


def test_the_tool_works_only_in_a_voice_conversation(monkeypatch):
    monkeypatch.setattr(conversation.current(), "lookups", None)
    content, error = asyncio.run(tools.run("background_lookup", {"brief": "x", "about": "y"}))
    assert error and "only in a voice conversation" in content
    j, _ = jobs(lambda brief: asyncio.sleep(0, "ok"))

    async def main():
        monkeypatch.setattr(conversation.current(), "lookups", j)
        out = await tools.run("background_lookup", {"brief": "Find X.", "about": "X"})
        await j.close()
        return out

    content, error = asyncio.run(main())
    assert not error and content.startswith("Started looking into X")


def test_side_jobs_only_read():
    reads = side.read_only()
    assert "conversation_search" in reads and "background_lookup" not in reads
    for name in ("task_start", "task_approve", "todo_add", "schedule_add", "memory_save", "connection_add"):
        assert name not in reads
    assert all(not (t.changes or t.gated) for t in tools.all_tools() if t.name in reads)


def test_a_side_job_runs_on_its_model_with_reads_only(monkeypatch):
    seen = {}

    async def query(prompt, options):
        seen["prompt"], seen["options"] = prompt, options
        yield ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False, num_turns=2,
                            session_id="s", result=" It's sunny. ")

    monkeypatch.setattr("claude_agent_sdk.query", query)
    assert asyncio.run(side.look("Weather in Lisbon tomorrow?")) == "It's sunny."
    options = seen["options"]
    assert options.permission_mode == "dontAsk" and options.tools == side.WEB
    assert set(options.allowed_tools) <= set(side.WEB) | {f"mcp__jarvis__{n}" for n in side.read_only()}
    assert "Bash" not in options.allowed_tools and options.model


# ── Work cut off by the user speaking ─────────────────────────────


def text_event(text: str) -> StreamEvent:
    return StreamEvent(uuid="1", session_id="s",
                       event={"type": "content_block_delta", "delta": {"type": "text_delta", "text": text}})


def result() -> ResultMessage:
    return ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False, num_turns=1,
                         session_id="s")


class SearchingClient:
    """The model starts a web search that runs until interrupted."""

    def __init__(self) -> None:
        self.running = asyncio.Event()
        self.stop = asyncio.Event()
        self.prompts: list[str] = []
        self.interrupted = False

    async def query(self, prompt) -> None:
        self.prompts.append(prompt)

    async def interrupt(self) -> None:
        self.interrupted = True
        self.stop.set()

    async def receive_response(self):
        if len(self.prompts) == 1:
            yield text_event("Searching a few sources.")
            yield AssistantMessage(content=[ToolUseBlock(id="w1", name="WebSearch", input={"query": "x"})], model="m")
            self.running.set()
            await self.stop.wait()
            yield UserMessage(content=[ToolResultBlock(tool_use_id="w1", content="cut")])
            yield result()
        else:
            yield text_event("Sure.")
            yield result()


def test_work_cut_off_by_the_user_is_offered_to_the_next_turn_to_hand_over(monkeypatch):
    monkeypatch.setattr(memory, "log_exchange", lambda *a, **k: None)

    async def main():
        brain = cli.CliBrain()
        brain.client = client = SearchingClient()

        async def listen():
            async for _ in brain.respond("compare the three cheapest flights to Lisbon"):
                pass

        turn = asyncio.create_task(listen())
        await asyncio.wait_for(client.running.wait(), 2)
        turn.cancel()
        with pytest.raises(asyncio.CancelledError):
            await turn
        first = "".join([c async for c in brain.respond("oh and what's the time?")])
        again = "".join([c async for c in brain.respond("thanks")])
        return client, first, again

    client, first, again = asyncio.run(main())
    assert client.interrupted and first == "Sure."
    assert 'still working on "compare the three cheapest flights to Lisbon"' in client.prompts[1]
    assert "background_lookup" in client.prompts[1] and "background_lookup" not in client.prompts[2]
