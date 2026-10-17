"""Tests for CliBrain.respond when talked over: interrupted replies, running commands, handover, CLI restarts."""

import asyncio

from claude_agent_sdk import ClaudeSDKError

from fake_cli import Hold, next_turn, result, text, tool_call, tool_done
from jarvis.brain import cli
from jarvis.gates import shell

STORY = "tell me a story"


def test_talking_over_a_reply_stops_it_and_the_next_turn_starts_clean(voice):
    hold = Hold()
    v = voice([text("Once upon a time."), hold, text(" Never heard."), result()], [text("Sunny."), result()])

    async def main():
        return await v.talk_over(STORY, hold), await v.say("what's the weather?")

    cut, reply = asyncio.run(main())
    assert (cut, reply) == ("Once upon a time.", "Sunny.")
    assert v.cli.interrupts == 1 and len(v.clis) == 1 and not v.cli.closed
    assert "still working on" not in v.cli.prompts[1] and "didn't hear" not in v.cli.prompts[1]
    assert v.logged == [(STORY, "Once upon a time.", True), ("what's the weather?", "Sunny.", False)]


def test_work_cut_off_mid_tool_call_is_handed_to_the_next_turn_once(voice):
    hold = Hold()
    v = voice([text("Looking."), tool_call("w1", "WebSearch", {"query": "flights to Goa"}), hold, result()],
              [text("Hotels too."), result()], [text("Okay."), result()])

    async def main():
        await v.talk_over("find me   flights\nto Goa", hold)
        await v.say("and hotels?")
        await v.say("thanks")

    asyncio.run(main())
    assert v.cli.interrupts == 1
    assert 'still working on "find me flights to Goa"' in v.cli.prompts[1] and "background_lookup" in v.cli.prompts[1]
    assert "still working on" not in v.cli.prompts[2]


def test_a_tool_that_already_finished_isnt_work_cut_off(voice):
    hold = Hold()
    v = voice([tool_call("w1", "WebSearch"), tool_done("w1", "results"), next_turn(), text("Found three."), hold,
               result()], [text("Okay."), result()])

    async def main():
        await v.talk_over("find flights", hold)
        await v.say("never mind")

    asyncio.run(main())
    assert v.cli.interrupts == 1 and "still working on" not in v.cli.prompts[1]


def command_turn(hold: Hold) -> list:
    return [text("Rendering now."), tool_call("b1", "Bash", {"command": "npm run render"}), hold,
            tool_done("b1", "rendered out.mp4"), next_turn(), text("The render finished."), result()]


def test_a_command_talked_over_finishes_unheard_and_its_words_reach_the_next_turn_once(voice):
    hold = Hold()
    v = voice(command_turn(hold), [text("It's in out.mp4."), result()], [text("Okay."), result()])

    async def main():
        await v.talk_over("render the video", hold)
        reply = v.brain.respond("how long will it take?")
        first = await anext(reply)  # said at once, while the command still runs
        hold.release()
        return first, "".join([chunk async for chunk in reply]), await v.say("great")

    first, rest, last = asyncio.run(main())
    assert first == shell.HOLD + " " and rest == "It's in out.mp4." and last == "Okay."
    assert v.cli.interrupts == 0 and not v.brain._running
    assert 'didn\'t hear the rest of that reply: "The render finished."' in v.cli.prompts[1]
    assert "didn't hear" not in v.cli.prompts[2]


def test_no_hold_line_once_the_command_finished_before_the_next_turn(voice):
    hold = Hold()
    v = voice(command_turn(hold), [text("It's in out.mp4."), result()])

    async def main():
        await v.talk_over("render the video", hold)
        hold.release()
        await v.brain._drain
        return await v.say("is it done?")

    assert asyncio.run(main()) == "It's in out.mp4."
    assert v.cli.interrupts == 0


def test_an_interrupt_that_fails_restarts_the_cli(voice):
    hold = Hold()
    v = voice([text("Once."), hold, result()], [text("Fresh start."), result()],
              interrupt_fails=ClaudeSDKError("the CLI went away"))

    async def main():
        await v.talk_over(STORY, hold)
        await v.brain._drain
        first = v.cli
        return first, v.brain.client, await v.say("hello?")

    first, client_after, reply = asyncio.run(main())
    assert first.closed and client_after is None
    assert reply == "Fresh start." and len(v.clis) == 2 and v.cli.connected


def test_an_interrupted_reply_that_never_ends_restarts_the_cli(voice, monkeypatch):
    monkeypatch.setattr(cli, "INTERRUPT_SECONDS", 0.05)
    hold = Hold()
    v = voice([text("Once."), hold, result()], [text("Fresh start."), result()], drain_hangs=True)

    async def main():
        await v.talk_over(STORY, hold)
        await v.brain._drain
        return await v.say("hello?")

    assert asyncio.run(main()) == "Fresh start."
    assert v.clis[0].interrupts == 1 and v.clis[0].closed and len(v.clis) == 2


def test_a_command_reply_that_breaks_off_while_finishing_restarts_the_cli(voice):
    hold = Hold()
    v = voice([text("Rendering."), tool_call("b1", "Bash", {"command": "npm run render"}), hold,
               ClaudeSDKError("the CLI died")], [text("Back."), result()])

    async def main():
        await v.talk_over("render the video", hold)
        hold.release()
        await v.brain._drain
        return await v.say("did it work?")

    assert asyncio.run(main()) == "Back."
    assert v.clis[0].closed and v.clis[0].interrupts == 0 and len(v.clis) == 2
    assert not v.brain._running and "didn't hear" not in v.cli.prompts[0]


def test_the_cli_failing_mid_reply_says_so_and_the_next_turn_restarts_it(voice):
    v = voice([text("Let me check."), ClaudeSDKError("broken pipe")], [text("Here now."), result()])

    async def main():
        return await v.say("check my mail"), await v.say("well?")

    first, second = asyncio.run(main())
    assert first == "Let me check. I can't reach Claude right now."
    assert second == "Here now." and v.clis[0].closed and len(v.clis) == 2
    assert v.logged[0] == ("check my mail", "Let me check.", False)  # Claude's words only, not JARVIS's apology


def test_a_failed_turn_with_nothing_said_apologises(voice):
    v = voice([result("API Error: 500 Internal server error")])
    assert asyncio.run(v.say("hi")) == "Sorry, that didn't work. Something went wrong on my end."


def test_a_subagents_words_arent_spoken(voice):
    v = voice([text("Looking."), tool_call("a1", "Agent", {"subagent_type": "lookup"}),
               text("Found it in config.py.", parent="a1"), tool_done("a1", "config.py"), next_turn(),
               text("It's in config.py."), result()])
    assert asyncio.run(v.say("where's the port set?")) == "Looking. It's in config.py."
