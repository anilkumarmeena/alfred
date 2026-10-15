"""Tests for stopping the voice brain's background steps when the user asks: kept tool calls (jarvis.brain.kept),
background lookups (jarvis.brain.side), background_stop, and task_stop on a merge carrying on in the background."""

import asyncio
import time

import pytest
from test_kept import MERGE, run, settle, taking, unscreened  # noqa: F401  (run and unscreened are fixtures)

from jarvis.brain.kept import Kept
from jarvis.brain.side import Jobs
from jarvis.state import conversation
from jarvis.tasks import client
from jarvis.tools import side as side_tools, tasks as task_tools

STOPPED = {"id": 5, "title": "the Chat fix", "kind": "code_change", "status": "done", "seen": 1,
           "updated": time.time(), "spoken": "Task 5, the Chat fix, wasn't merged: its merge was stopped, so nothing "
                                     "changed and its branch is kept."}


@pytest.fixture
def stops(monkeypatch):
    """Fake runner stop: records the task ids, answers STOPPED (or raises `refuse`)."""

    class Stops(list):
        refuse: Exception | None = None

    asked = Stops()

    async def stop(task_id):
        asked.append(task_id)
        if asked.refuse is not None:
            raise asked.refuse
        return STOPPED

    monkeypatch.setattr(client, "stop", stop)
    return asked


async def kept_merge(run, kept: Kept, task_id: int = 5):  # noqa: F811
    """A task_merge call that has left its turn and is still running (calls leave only once results can come back)."""
    assert kept.finished is not None
    run.gate = asyncio.Event()
    await kept.run(MERGE, {"id": task_id})
    [call] = [c for c in kept.going() if c.args == {"id": task_id}]
    return call


def in_conversation(kept: Kept | None = None, lookups: Jobs | None = None) -> conversation.Conversation:
    talk = conversation.begin()
    talk.kept, talk.lookups = kept, lookups
    return talk


# ── Kept ────────────────────────────────────────────────────────


def test_a_stopped_kept_call_ends_without_coming_back(run):  # noqa: F811
    async def main():
        kept = Kept(seconds=0.01)
        got = taking(kept)
        call = await kept_merge(run, kept)

        async def runner_stop():
            return "stopped there"

        said = await kept.stop([call], runner_stop())
        run.gate.set()
        await settle()
        return said, call, got, kept.going()

    said, call, got, going = asyncio.run(main())
    assert said == "stopped there" and call.task.cancelled() and got == [] and going == []


def test_if_stopping_its_work_fails_the_call_carries_on_and_its_result_still_comes_back(run):  # noqa: F811
    async def main():
        kept = Kept(seconds=0.01)
        got = taking(kept)
        call = await kept_merge(run, kept)

        async def runner_stop():
            run.gate.set()  # it ends meanwhile
            await settle()
            raise ValueError("Task 5 isn't running.")

        with pytest.raises(ValueError, match="isn't running"):
            await kept.stop([call], runner_stop())
        await settle()
        return call, got

    call, got = asyncio.run(main())
    assert not call.task.cancelled() and [said for said, _, _ in got] == ["Task 5, the Chat fix, merged into master."]


# ── background_stop ─────────────────────────────────────────────


def test_with_nothing_going_it_says_so_and_points_to_the_other_stops():
    async def main():
        in_conversation(Kept())
        return await side_tools.background_stop()

    assert asyncio.run(main()).startswith("Nothing of yours is going in the background. A task stops with task_stop")


def test_it_stops_a_merge_carrying_on_through_the_runner_so_the_repo_is_left_clean(run, stops):  # noqa: F811
    async def main():
        kept = Kept(seconds=0.01)
        got = taking(kept)
        in_conversation(kept)
        call = await kept_merge(run, kept)
        said = await side_tools.background_stop("merge")
        run.gate.set()
        await settle()
        return said, call, got

    said, call, got = asyncio.run(main())
    assert said == STOPPED["spoken"] and stops == [5] and call.task.cancelled() and got == []


def test_it_asks_which_when_several_match_and_lists_what_is_going_when_none_does(run, stops):  # noqa: F811
    async def main():
        kept = Kept(seconds=0.01)
        taking(kept)
        in_conversation(kept)
        await kept_merge(run, kept, 5)
        await kept_merge(run, kept, 6)
        several = await side_tools.background_stop("merge")
        none = await side_tools.background_stop("weather")
        one = await side_tools.background_stop("task merge (id 6)")
        run.gate.set()
        return several, none, one

    several, none, one = asyncio.run(main())
    assert several == "Not stopped: 2 things going match, so ask which: task_merge (id 5); task_merge (id 6)."
    assert none == 'Not stopped: nothing going in the background matches "weather". Going: task_merge (id 5); task_merge (id 6).'
    assert one == STOPPED["spoken"] and stops == [6]


def test_it_stops_a_background_lookup_and_nothing_is_announced():
    announced = []

    async def look(brief: str) -> str:
        await asyncio.sleep(3600)
        return "found"

    async def main():
        lookups = Jobs(lambda text, note: announced.append(text), run=look)
        in_conversation(Kept(), lookups)
        lookups.start("Find flights to Lisbon in May.", "flights to Lisbon")
        said = await side_tools.background_stop()
        return said, lookups.running

    said, running = asyncio.run(main())
    assert said == "Stopped the lookup on flights to Lisbon; nothing is announced from it."
    assert running == {} and announced == []


# ── task_stop ───────────────────────────────────────────────────


def test_task_stop_on_a_merge_carrying_on_stops_it_and_its_result_doesnt_come_back_too(run, stops):  # noqa: F811
    async def main():
        kept = Kept(seconds=0.01)
        got = taking(kept)
        in_conversation(kept)
        merging = await kept_merge(run, kept, 5)
        other = await kept_merge(run, kept, 6)
        brief = await task_tools.task_stop(5)
        run.gate.set()
        await settle()
        return brief, merging, other, got

    brief, merging, other, got = asyncio.run(main())
    assert brief["spoken"] == STOPPED["spoken"] and stops == [5]
    assert merging.task.cancelled() and not other.task.cancelled()
    assert [said for said, _, _ in got] == ["Task 5, the Chat fix, merged into master."]  # task 6's merge, as it ended
