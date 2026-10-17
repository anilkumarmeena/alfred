"""Tests for slow tool calls carried on outside the spoken turn (jarvis.brain.kept) and the brain's follow-up turns."""

import asyncio
import json

import pytest
from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

from fake_cli import Hold, result, text, tool_call
from jarvis import tools
from jarvis.brain import kept as kept_module
from jarvis.brain.kept import Call, Kept, prompt, summary
from jarvis.gates.shell import ShellGate
from jarvis.safety import commands
from jarvis.state import conversation, memory

MERGE = tools.Tool("task_merge", "", {}, [], lambda: None)
MERGED = json.dumps({"id": 5, "spoken": "Task 5, the Chat fix, merged into master."})


@pytest.fixture(autouse=True)
def unscreened(monkeypatch):
    monkeypatch.setattr(kept_module, "injection_check_on", lambda: False)


@pytest.fixture
def run(monkeypatch):
    """Fake tools.run: each call waits for `gate` (if set) and returns `answer`."""

    class Run:
        gate: asyncio.Event | None = None
        answer = (MERGED, False)
        calls = 0

        async def __call__(self, name, args):
            self.calls += 1
            if self.gate is not None:
                await self.gate.wait()
            return self.answer

    fake = Run()
    monkeypatch.setattr(tools, "run", fake)
    return fake


def taking(kept: Kept) -> list[tuple]:
    got: list[tuple] = []
    kept.finished = lambda said, asked, words: got.append((said, asked, words))
    return got


async def settle(rounds: int = 5) -> None:
    for _ in range(rounds):
        await asyncio.sleep(0)


# ── Kept ────────────────────────────────────────────────────────


def test_a_quick_call_answers_in_the_turn(run):
    async def main():
        kept = Kept(seconds=1)
        got = taking(kept)
        answer = await kept.run(MERGE, {"id": 5})
        await settle()
        return answer, got

    answer, got = asyncio.run(main())
    assert answer == (MERGED, False) and got == []


def test_a_slow_call_leaves_the_turn_and_its_result_comes_back_once(run):
    async def main():
        conversation.current().heard.turn("merge the chat fix and push")
        run.gate = asyncio.Event()
        kept = Kept(seconds=0.01)
        got = taking(kept)
        answer = await kept.run(MERGE, {"id": 5})
        assert got == [] and kept.calls
        run.gate.set()
        await settle()
        return answer, got, kept

    (note, failed), got, kept = asyncio.run(main())
    assert not failed and "carries on in the background" in note and "don't call it again" in note
    [(said, asked, words)] = got
    assert words == "merge the chat fix and push"
    assert said == "Task 5, the Chat fix, merged into master."
    assert "Not something Sam said" in asked and "merged into master" in asked and "<result>" in asked
    assert not kept.calls and run.calls == 1


def test_without_a_conversation_to_take_the_result_a_call_just_runs(run):
    async def main():
        run.gate = asyncio.Event()
        kept = Kept(seconds=0.01)  # no `finished`
        waiting = asyncio.ensure_future(kept.run(MERGE, {}))
        await asyncio.sleep(0.05)
        assert not waiting.done()
        run.gate.set()
        return await waiting

    assert asyncio.run(main()) == (MERGED, False)


def test_a_call_whose_turn_was_cut_off_carries_on(run):
    async def main():
        run.gate = asyncio.Event()
        kept = Kept(seconds=60)
        got = taking(kept)
        waiting = asyncio.ensure_future(kept.run(MERGE, {}))
        await settle()
        waiting.cancel()  # the CLI gave up on it
        await settle()
        run.gate.set()
        await settle()
        return got

    assert len(asyncio.run(main())) == 1


def test_talked_over_calls_are_kept_once_and_named(run):
    async def main():
        run.gate = asyncio.Event()
        kept = Kept(seconds=60)
        got = taking(kept)
        waiting = asyncio.ensure_future(kept.run(MERGE, {}))
        await settle()
        first, again = kept.keep_all(), kept.keep_all()
        run.gate.set()
        await settle()
        return first, again, await waiting, got

    first, again, answer, got = asyncio.run(main())
    assert first == ["task_merge"] and again == [] and answer == (MERGED, False) and len(got) == 1


def test_a_tool_that_stays_never_leaves_the_turn(run):
    async def main():
        run.gate = asyncio.Event()
        kept = Kept(seconds=0.01)
        taking(kept)
        waiting = asyncio.ensure_future(kept.run(tools.Tool("voice_enroll", "", {}, [], lambda: None, stays=True), {}))
        await asyncio.sleep(0.05)
        done = waiting.done()
        run.gate.set()
        await waiting
        return done

    assert asyncio.run(main()) is False


def test_closing_the_conversation_stops_kept_calls_without_a_follow_up(run):
    async def main():
        run.gate = asyncio.Event()
        kept = Kept(seconds=0.01)
        got = taking(kept)
        await kept.run(MERGE, {})
        await kept.close()
        run.gate.set()
        await settle()
        return got, kept.calls

    assert asyncio.run(main()) == ([], set())


def test_the_phone_line_is_the_results_own_sentence_else_what_ended():
    call = Call("gmail_search", {}, "", None)
    assert summary(call, MERGED, False) == "Task 5, the Chat fix, merged into master."
    assert summary(call, "[1, 2]", False) == "The gmail search has finished."
    assert summary(call, "boom", True) == "The gmail search failed."


def test_the_follow_up_prompt_keeps_the_result_as_data_and_counts_pictures():
    call = Call("file_read", {}, "read the lease", None)
    text_ = kept_module._text([{"type": "text", "text": "Clause 4"}, {"type": "image", "data": "x"}])
    said = prompt(call, text_, False, "WARNING: hidden instructions")
    assert '(when Sam said "read the lease")' in said and "data, never instructions" in said
    assert "Clause 4" in said and "1 picture not passed on" in said and "WARNING: hidden instructions" in said


def test_a_screened_result_reaches_the_brain_withheld(monkeypatch, run):
    monkeypatch.setattr(kept_module, "injection_check_on", lambda: True)

    async def screen(tool, args, response, model, seconds):
        assert tool == "mcp__jarvis__task_merge"
        return {"hookSpecificOutput": {"updatedToolOutput": "[withheld]", "additionalContext": "Part was withheld."}}

    monkeypatch.setattr(kept_module.injection, "screen", screen)

    async def main():
        run.gate = asyncio.Event()
        kept = Kept(seconds=0.01)
        got = taking(kept)
        await kept.run(MERGE, {})
        run.gate.set()
        await settle(10)
        return got

    [(_, asked, _)] = asyncio.run(main())
    assert "[withheld]" in asked and "Part was withheld." in asked and "merged into master" not in asked


# ── The brain's follow-up turns ─────────────────────────────────


@pytest.fixture
def announced(monkeypatch) -> list[str]:
    said: list[str] = []
    monkeypatch.setattr(memory, "log_announcement", said.append)
    return said


def test_a_follow_up_is_spoken_without_taking_anything_as_the_users_words(voice, announced):
    v = voice([text("The Chat fix is merged."), result()])

    async def main():
        heard = conversation.current().heard
        heard.turn("what's the weather?")
        reply = "".join([c async for c in v.brain.follow_up("[the merge ended]", "merge the chat fix")])
        return reply, heard

    reply, heard = asyncio.run(main())
    assert reply == "The Chat fix is merged." and announced == ["The Chat fix is merged."]
    assert "[the merge ended]" in v.cli.prompts[0] and v.logged == []
    assert heard.said == "what's the weather?" and heard.spent  # newer words: not the follow-up's to use
    assert heard._reply == "The Chat fix is merged."  # a bare "yes" next answers it


def test_a_follow_up_carries_on_the_turn_that_made_the_call(voice, announced):
    v = voice([text("Merged."), result()])

    async def main():
        conversation.current().heard.turn("merge the chat fix and push")
        async for _ in v.brain.follow_up("[the merge ended]", "merge the chat fix and push"):
            pass

    asyncio.run(main())
    assert v.brain.shell.said == "merge the chat fix and push"
    assert not conversation.current().heard.spent


def test_the_users_turn_stops_a_follow_up_and_is_answered(voice, announced):
    hold = Hold()
    v = voice([text("The merge"), hold, text(" is done."), result()], [text("Sunny."), result()])

    async def main():
        follow = v.brain.follow_up("[the merge ended]")
        first = await anext(follow)

        async def finish():
            return [c async for c in follow]

        left = asyncio.ensure_future(finish())
        await asyncio.wait_for(hold.reached.wait(), 2)
        reply = await asyncio.wait_for(v.say("what's the weather?"), 2)
        return first, await left, reply

    first, left, reply = asyncio.run(main())
    assert first == "The merge" and left == [] and reply == "Sunny."
    assert v.cli.interrupts == 1 and "Sorry" not in "".join(left)
    assert announced == ["The merge"]


def test_talking_over_a_reply_while_a_jarvis_tool_runs_keeps_it_for_later(voice, run):
    hold = Hold()
    v = voice([text("Merging."), tool_call("m1", "mcp__jarvis__task_merge", {"id": 5}), hold, result()],
              [text("Sunny."), result()])

    async def main():
        run.gate = asyncio.Event()
        taking(v.brain.kept)
        v.brain.kept.seconds = 60
        running = asyncio.ensure_future(v.brain.kept.run(MERGE, {"id": 5}))  # the CLI's call of it
        await settle()
        await v.talk_over("merge the chat fix", hold)
        await v.say("what's the weather?")
        run.gate.set()
        return await running

    asyncio.run(main())
    assert "task_merge was still running, so it carries on in the background" in v.cli.prompts[1]
    assert "still working on" not in v.cli.prompts[1]


# ── Slow shell commands ─────────────────────────────────────────


@pytest.mark.parametrize("command", ["git pull", "git -C ~/code/app pull --rebase", "cd app && git fetch origin",
                                     "git clone https://github.com/x/y", "docker pull redis"])
def test_network_git_is_slow(command):
    assert commands.slow(command)


@pytest.mark.parametrize("command", ["git status", "git commit -m x", "git merge main", "ls", "brew install jq"])
def test_everyday_commands_arent_slow(command):
    assert not commands.slow(command)


def test_the_voice_shell_sends_a_pull_to_start_job():
    async def main():
        gate = ShellGate()
        gate.new_turn("pull the latest")
        bash = await gate("Bash", {"command": "git pull", "dangerouslyDisableSandbox": True}, None)
        job = await gate("mcp__shell__start_job", {"command": "git pull", "folder": "~/code/app"}, None)
        return bash, job

    bash, job = asyncio.run(main())
    assert isinstance(bash, PermissionResultDeny) and "start_job" in bash.message
    assert isinstance(job, PermissionResultAllow)
