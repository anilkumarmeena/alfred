"""Tests for jarvis.brain.side: background reads announced when done, read-only side jobs, file lookups that hold a
turn only briefly, cut-off work handed over."""

import asyncio
from types import SimpleNamespace

import pytest
from claude_agent_sdk import (
    AssistantMessage,
    PermissionResultAllow,
    PermissionResultDeny,
    ResultMessage,
    StreamEvent,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from jarvis import tools
from jarvis.brain import cli, side
from jarvis.core.config import LOOKUP_MODEL, SECRETS_DIR
from jarvis.safety import shell
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
    assert note == "" and not j.running  # said once: nothing more for the brain


# ── Announced answers: short plain speech, said once ──────────────

MARKDOWN_ANSWER = """## Wake word

The wake word is **hey jarvis**.

It's set in `jarvis/core/config.py` at line 88.

- It can be changed with a setting.
- It's checked on the computer.

## Check source

- jarvis/core/config.py:88
- docs/configuration.md"""


def announced(answer: str) -> tuple[str, str]:
    """What a background lookup answering `answer` announces: (said, note)."""
    j, said = jobs(lambda brief: asyncio.sleep(0, answer))

    async def main():
        j.start("Find the wake word.", "the wake word")
        while j.running:
            await asyncio.sleep(0.01)

    asyncio.run(main())
    [(text, note)] = said
    return text, note


def unspeakable(text: str) -> bool:
    return any(mark in text for mark in ("#", "*", "`", "/", "- ", ":88", "line 88", "Check source"))


@pytest.fixture
def wording(monkeypatch):
    """The wording model, on and faked: replies with `wording.reply` (or raises it) and records each call."""
    from jarvis.brain import sdk, usage

    model = SimpleNamespace(reply="", calls=[])

    async def ask(prompt, system_prompt, _model, **options):
        model.calls.append((prompt, system_prompt))
        if isinstance(model.reply, Exception):
            raise model.reply
        return model.reply, 0.0

    monkeypatch.setattr(side, "ANNOUNCE_ENABLED", True)
    monkeypatch.setattr(usage, "out", lambda now=None: None)
    monkeypatch.setattr(sdk, "ask", ask)
    return model


def test_a_markdown_answer_is_announced_as_a_short_summary_once_with_the_detail_kept_for_the_brain(wording):
    wording.reply = ("**The wake word is hey jarvis.** It's set in the config file, at `jarvis/core/config.py`. "
                     "You can change it with a setting. Want me to?")
    text, note = announced(MARKDOWN_ANSWER)
    assert text == ("On the wake word: The wake word is hey jarvis. You can change it with a setting. "
                    "Want me to?")  # the model's markdown and its sentence with a path are left out too
    assert not unspeakable(text)
    [(prompt, system)] = wording.calls
    assert MARKDOWN_ANSWER in prompt and system == side.spoken_system()
    assert note.count(MARKDOWN_ANSWER) == 1 and "hey jarvis" not in note.replace(MARKDOWN_ANSWER, "")


@pytest.mark.parametrize("off", ["failed", "slow", "disabled"])
def test_without_the_wording_model_a_markdown_answer_is_trimmed_to_a_few_plain_sentences(wording, monkeypatch, off):
    if off == "disabled":
        monkeypatch.setattr(side, "ANNOUNCE_ENABLED", False)
    wording.reply = RuntimeError("no") if off == "failed" else TimeoutError()
    text, note = announced(MARKDOWN_ANSWER + "\n\nOne more thing.")
    assert text == ("On the wake word: The wake word is hey jarvis. It can be changed with a setting. "
                    "It's checked on the computer.")  # three sentences at most, none with a path
    assert not unspeakable(text) and MARKDOWN_ANSWER in note
    assert wording.calls == [] if off == "disabled" else len(wording.calls) == 1


def test_a_file_lookup_s_source_line_goes_to_the_brain_and_a_plain_answer_needs_no_model(wording):
    text, note = announced("The web server listens on port 8080.\nSource: jarvis/core/config.py:42")
    assert text == "On the wake word: The web server listens on port 8080."
    assert note == "Not said aloud, for follow-up questions: Source: jarvis/core/config.py:42"
    assert wording.calls == []


def test_a_long_plain_answer_is_worded_down_too(wording):
    wording.reply = "It's hey jarvis, and you can change it."
    long = " ".join(f"Point {n} about the wake word." for n in range(6))
    text, note = announced(long)
    assert text == "On the wake word: It's hey jarvis, and you can change it."
    assert note == f"Not said aloud, for follow-up questions: {long}"


@pytest.mark.parametrize("answer, said", [
    ("**The release is on 10/14/2026.**\n\n## Check source\n- notes/releases.md", "The release is on 10/14/2026."),
    ("**Mix it 3.5:1, water to flour.**\nSee recipes/bread.md at line 4.", "Mix it 3.5:1, water to flour."),
])
def test_dates_ratios_and_a_bold_sentence_on_its_own_line_are_still_said(wording, answer, said):
    wording.reply = RuntimeError("no")
    assert announced(answer)[0] == f"On the wake word: {said}"


def test_an_answer_that_is_all_detail_still_says_it_is_back(wording):
    wording.reply = "- jarvis/core/config.py:88"
    assert announced("- jarvis/core/config.py:88")[0] == "The lookup on the wake word is back; ask me for the details."


def test_a_quick_file_lookup_hands_its_turn_the_whole_answer_unworded(monkeypatch, wording):
    monkeypatch.setattr(side, "LOOKUP_WAIT", 1.0)
    j = side.Jobs(lambda text, note: None, find=lambda brief: asyncio.sleep(0, MARKDOWN_ANSWER))
    assert asyncio.run(j.ask("x", "the wake word")) == f"On the wake word: {MARKDOWN_ANSWER}"
    assert wording.calls == []


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
    reads = {t.name for t in tools.all_tools() if side.reads(t)}
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
    assert set(options.allowed_tools) <= set(side.WEB) | {f"mcp__jarvis__{t.name}" for t in tools.all_tools() if side.reads(t)}
    assert "Bash" not in options.allowed_tools and options.model


# ── File lookups: in the turn only while quick ────────────────────


def finds(answers: dict[str, float]):
    """A fake file lookup: each brief answers after its given seconds."""
    async def find(brief: str) -> str:
        await asyncio.sleep(answers[brief])
        return f"{brief} is in config.py."
    return find


def file_jobs(answers: dict[str, float]) -> tuple[side.Jobs, list[tuple[str, str]]]:
    said: list[tuple[str, str]] = []
    return side.Jobs(lambda text, note: said.append((text, note)), find=finds(answers)), said


def test_a_quick_file_lookup_answers_in_the_turn_and_isnt_announced(monkeypatch):
    monkeypatch.setattr(side, "LOOKUP_WAIT", 1.0)
    j, said = file_jobs({"The port": 0.01})

    async def main():
        answer = await j.ask("The port", "the port")
        await asyncio.sleep(0.02)
        return answer

    assert asyncio.run(main()) == "On the port: The port is in config.py." and said == [] and not j.running


def test_a_slow_file_lookup_stops_holding_the_turn_and_is_announced_when_done(monkeypatch):
    monkeypatch.setattr(side, "LOOKUP_WAIT", 0.05)
    j, said = file_jobs({"The port": 0.2})

    async def main():
        loop = asyncio.get_running_loop()
        began = loop.time()
        told = await j.ask("The port", "the port")
        held, running = loop.time() - began, dict(j.running)
        await asyncio.sleep(0.3)
        return told, held, running

    told, held, running = asyncio.run(main())
    assert held < 0.15 and list(running.values()) == ["the port"]
    assert told.startswith("Still looking into the port") and "announced when it's ready" in told
    assert said == [("On the port: The port is in config.py.", "")]


def test_a_turn_s_lookups_share_one_wait_so_a_second_cant_hold_it_again(monkeypatch):
    """Two slow lookups chained in one turn hold it no longer than one; the next turn gets a fresh wait."""
    monkeypatch.setattr(side, "LOOKUP_WAIT", 0.05)
    j, said = file_jobs({"slow": 0.2, "quick": 0.01})

    async def main():
        loop = asyncio.get_running_loop()
        began = loop.time()
        first = await j.ask("slow", "the first")
        second = await j.ask("quick", "the second")  # the turn's wait is spent: straight to the background
        held = loop.time() - began
        await asyncio.sleep(0.3)
        j.new_turn()
        third = await j.ask("quick", "the third")
        return first, second, held, third

    first, second, held, third = asyncio.run(main())
    assert first.startswith("Still looking") and second.startswith("Still looking") and held < 0.15
    assert third == "On the third: quick is in config.py."
    assert sorted(t for t, _ in said) == ["On the first: slow is in config.py.", "On the second: quick is in config.py."]


def test_a_file_lookup_cut_off_with_its_turn_carries_on_and_is_announced(monkeypatch):
    monkeypatch.setattr(side, "LOOKUP_WAIT", 5.0)
    j, said = file_jobs({"The port": 0.1})

    async def main():
        turn = asyncio.create_task(j.ask("The port", "the port"))
        await asyncio.sleep(0.02)
        turn.cancel()
        with pytest.raises(asyncio.CancelledError):
            await turn
        await asyncio.sleep(0.2)

    asyncio.run(main())
    assert [t for t, _ in said] == ["On the port: The port is in config.py."]


def test_file_lookups_count_against_the_running_limit(monkeypatch):
    monkeypatch.setattr(side, "LOOKUP_WAIT", 0.0)
    j, _ = file_jobs({"x": 1.0})

    async def main():
        told = [await j.ask("x", about) for about in ("one", "two", "three")]
        await j.close()
        return told

    told = asyncio.run(main())
    assert told[2].startswith("Not started") and "one, two" in told[2] and "in the turn" not in told[2]


def test_a_file_lookup_reads_only_non_secrets_and_only_so_many():
    reads = side.Reads(limit=2)
    run = lambda name, args: asyncio.run(reads(name, args, None))  # noqa: E731
    for name, args in (("Edit", {"file_path": "/Users/x/app/a.py"}), ("Bash", {"command": "ls"}),
                       ("Agent", {"subagent_type": "lookup"}), ("Read", {"file_path": f"{SECRETS_DIR}/google.json"}),
                       ("Read", {"file_path": "/Users/x/app/.env"}), ("Glob", {"pattern": "*", "path": "~/.ssh"})):
        assert isinstance(run(name, args), PermissionResultDeny), name
    assert isinstance(run("Read", {"file_path": "/Users/x/app/pyproject.toml"}), PermissionResultAllow)
    assert isinstance(run("Grep", {"pattern": "version", "path": "/Users/x/app"}), PermissionResultAllow)
    spent = run("Glob", {"pattern": "*.toml", "path": "/Users/x/app"})
    assert isinstance(spent, PermissionResultDeny) and "answer now with what you found" in spent.message


def test_a_file_lookup_runs_on_the_lookup_model_with_every_read_gated(monkeypatch, tmp_path):
    seen = {}

    async def query(prompt, options):
        seen["prompt"], seen["options"] = prompt, options
        yield ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False, num_turns=2,
                            session_id="s", result=" Port 8080, in config.py. ")

    monkeypatch.setattr("claude_agent_sdk.query", query)
    monkeypatch.setattr(shell, "scratch_dir", lambda: tmp_path / "scratch")
    assert asyncio.run(side.find("Which port does the web server use?")) == "Port 8080, in config.py."
    options = seen["options"]
    assert options.model == LOOKUP_MODEL and options.tools == side.FILE_READS and options.allowed_tools == []
    assert isinstance(options.can_use_tool, side.Reads) and options.max_turns == side.LOOKUP_TURNS
    assert options.cwd == str(tmp_path / "scratch") and (tmp_path / "scratch").is_dir()


def test_the_file_lookup_tool_uses_the_conversation_s_jobs(monkeypatch):
    monkeypatch.setattr(conversation.current(), "lookups", None)
    content, error = asyncio.run(tools.run("file_lookup", {"brief": "x", "about": "y"}))
    assert error and "only in a voice conversation" in content
    j, _ = file_jobs({"The port": 0.0})

    async def main():
        monkeypatch.setattr(conversation.current(), "lookups", j)
        return await tools.run("file_lookup", {"brief": "The port", "about": "the port"})

    assert asyncio.run(main()) == ("On the port: The port is in config.py.", False)


class OneTurnClient:
    async def query(self, prompt) -> None: ...

    async def receive_response(self):
        yield text_event("Sure.")
        yield result()


def test_each_voice_turn_gets_a_fresh_lookup_wait(monkeypatch):
    monkeypatch.setattr(memory, "log_exchange", lambda *a, **k: None)
    j, _ = file_jobs({})
    j.waited = 30.0
    monkeypatch.setattr(conversation.current(), "lookups", j)

    async def main():
        brain = cli.CliBrain()
        brain.client = OneTurnClient()
        return "".join([c async for c in brain.respond("what port is it on?")])

    assert asyncio.run(main()) == "Sure." and j.waited == 0.0


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
