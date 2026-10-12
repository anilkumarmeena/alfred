"""Tests for jarvis.node.placement: where a job runs, by the user's word, their setting and an online check."""

import asyncio
import json
import time

import pytest
from test_node_link import mine, paired  # noqa: F401  (mine is a fixture)

from jarvis.brain import sdk, usage
from jarvis.node import home, machines, placement

FREE = {"same_protocol": True, "free": 2, "usage": {}, "online": True}
USED_UP = {"five_hour": {"status": "rejected", "resets_at": time.time() + 3600, "overage": "rejected"}}


class Hub:
    """Online checks answered from a table: machine id -> answer (None = offline)."""

    def __init__(self, answers: dict) -> None:
        self.answers = answers
        self.asked: list[list[str]] = []  # programs each check asked about

    async def check(self, node: int, timeout: float = 5, needs: list[str] = ()) -> dict | None:
        self.asked.append(list(needs))
        return self.answers.get(node)

    async def check_all(self, timeout: float = 5, needs: list[str] = ()) -> dict:
        return {m.id: await self.check(m.id, timeout, needs) for m in machines.paired()}


@pytest.fixture(autouse=True)
def no_model(monkeypatch):
    """The real placement model never runs here: its answer would depend on the computer's Claude login. A test that
    reaches it fakes sdk.ask_json itself; any other call fails the test."""
    stray = []

    async def unexpected(*args, **kwargs):
        stray.append(args[0])
        raise RuntimeError("an unfaked model call")

    monkeypatch.setattr(sdk, "ask_json", unexpected)
    yield
    assert not stray, "a placement test reached the model without faking it"


def choose(hub, kind=placement.TASKS, asked="", command="", running=0, slots=1):
    return asyncio.run(placement.choose(hub, kind, {"title": "a job", "command": command}, asked, running, slots))


@pytest.fixture
def here_has(monkeypatch):
    """The programs this computer finds, as a set the test fills (every program, until it says otherwise)."""
    found: set[str] | None = None

    async def have(names):
        return list(names) if found is None else [n for n in names if n in found]

    def only(*names: str) -> None:
        nonlocal found
        found = set(names)

    monkeypatch.setattr(home, "have", have)
    return only


def test_the_users_word_wins_and_an_offline_machine_falls_back_here(mine):
    studio, _ = paired("Studio")
    assert choose(Hub({studio.id: FREE}), asked="here") == placement.HERE
    assert choose(Hub({studio.id: FREE}), asked="studio").node == studio.id
    there = choose(Hub({}), asked="Studio")
    assert there.node is None and there.said == "Studio isn't online, so it's running on this computer instead."
    with pytest.raises(ValueError, match="no machine called"):
        choose(Hub({}), asked="laptop")


def test_a_machine_that_cant_take_a_task_says_why(mine):
    studio, _ = paired("Studio")
    machines.set_placement("tasks", str(studio.id))
    machines.set_placement("commands", str(studio.id))
    for answer, why in ((FREE | {"same_protocol": False}, "is updating"), (FREE | {"free": 0}, "is busy"),
                        (FREE | {"usage": USED_UP}, "has used up its Claude account")):
        assert why in choose(Hub({studio.id: answer})).said
    assert choose(Hub({studio.id: FREE | {"free": 0}}), placement.COMMANDS).node == studio.id  # commands take no slot


def test_letting_jarvis_choose_asks_the_model_among_those_able(mine, monkeypatch):
    studio, _ = paired("Studio")
    paired("Laptop")
    asked = []

    async def ask_json(prompt, schema, system, model, **options):
        asked.append((json.loads(prompt), schema))
        return {"where": str(studio.id), "why": "More cores."}, 0.0

    monkeypatch.setattr(sdk, "ask_json", ask_json)
    place = choose(Hub({studio.id: FREE}))  # the laptop is offline, so it isn't offered
    assert place.node == studio.id and place.said == ""
    material, schema = asked[0]
    assert list(material["machines"]) == [str(studio.id)] and schema["properties"]["where"]["enum"] == ["here", str(studio.id)]
    assert choose(Hub({})) == placement.HERE and len(asked) == 1  # nothing able: no call


def test_when_this_computers_account_is_used_up_a_task_goes_to_a_machine(mine, monkeypatch):
    studio, _ = paired("Studio")
    machines.set_placement("tasks", machines.HERE)
    machines.set_placement("commands", machines.HERE)
    monkeypatch.setattr(usage, "_read", lambda: USED_UP)
    place = choose(Hub({studio.id: FREE}))
    assert place.node == studio.id and "hit its limit" in place.said
    assert choose(Hub({studio.id: FREE}), placement.COMMANDS) == placement.HERE  # commands use no Claude account


def test_covering_for_this_computers_account_picks_the_machine_with_the_most_left(mine, monkeypatch):
    studio, _ = paired("Studio")
    laptop, _ = paired("Laptop")
    machines.set_placement("tasks", machines.AUTO)
    monkeypatch.setattr(usage, "_read", lambda: USED_UP)
    nearly = {"seven_day": {"status": "allowed_warning", "utilization": 0.9, "resets_at": time.time() + 86400}}
    hub = Hub({studio.id: FREE | {"usage": nearly}, laptop.id: FREE})
    place = choose(hub)
    assert place.node == laptop.id and place.said.endswith("so it's running on Laptop.")
    busy = Hub({studio.id: FREE, laptop.id: FREE | {"free": 0}})  # a machine with no free slot can't take it
    assert choose(busy).node == studio.id


def test_a_command_goes_only_where_its_programs_are(mine, here_has):
    studio, _ = paired("Studio")
    laptop, _ = paired("Laptop")
    machines.set_placement("commands", str(laptop.id))
    lacks = {"same_protocol": True, "free": 2, "has": ["cat"]}
    has = {"same_protocol": True, "free": 2, "has": ["cat", "ffmpeg"]}
    hub = Hub({studio.id: has, laptop.id: lacks})
    place = choose(hub, placement.COMMANDS, command="ffmpeg -i in.mov out.mp4 && cat log.txt")
    assert place.node is None and place.said == "Laptop doesn't have ffmpeg, so it's running on this computer instead."
    assert hub.asked[0] == ["ffmpeg", "cat"]  # the machine is asked about exactly what the command runs
    here_has("cat")  # this computer lacks it too: the machine that has it takes it
    place = choose(hub, placement.COMMANDS, command="ffmpeg -i in.mov out.mp4")
    assert place.node == studio.id and place.said == "This computer doesn't have ffmpeg, so it's running on Studio."
    machines.set_placement("commands", machines.HERE)
    assert choose(Hub({laptop.id: lacks}), placement.COMMANDS, command="ffmpeg -i a b") == placement.HERE  # nowhere has it


def test_letting_jarvis_choose_shows_the_model_each_computers_room(mine, monkeypatch, here_has):
    studio, _ = paired("Studio")
    asked = []

    async def ask_json(prompt, schema, system, model, **options):
        asked.append(json.loads(prompt))
        return {"where": "here", "why": "It's light."}, 0.0

    monkeypatch.setattr(sdk, "ask_json", ask_json)
    half = {"five_hour": {"status": "allowed", "utilization": 0.5, "resets_at": time.time() + 3600}}
    assert choose(Hub({studio.id: FREE | {"usage": half, "cores": 12, "load": 1.0}}), command="ls") == placement.HERE
    seen = asked[0]
    assert seen["machines"][str(studio.id)]["claude_usage_left_percent"] == 50
    assert seen["machines"][str(studio.id)]["free_slots"] == 2 and seen["machines"][str(studio.id)]["cores"] == 12
    assert {"free_slots", "claude_usage_left_percent", "load"} <= set(seen["this_computer"])


def test_when_the_model_cant_be_asked_the_computer_with_the_most_room_takes_it(mine, monkeypatch):
    studio, _ = paired("Studio")

    async def broken(*args, **kwargs):
        raise RuntimeError("down")

    monkeypatch.setattr(sdk, "ask_json", broken)
    assert choose(Hub({studio.id: FREE}), running=1, slots=1).node == studio.id  # no slot free here
    monkeypatch.setattr(placement, "_here", lambda running, slots: placement.Standing(12, 0.0, 2, 1.0))
    assert choose(Hub({studio.id: FREE | {"cores": 12, "load": 0.0}})) == placement.HERE  # a tie stays here


def test_usage_left_reads_the_tightest_limit():
    soon = time.time() + 3600
    assert placement.usage_left({}) == 1.0
    assert placement.usage_left({"five_hour": {"status": "allowed", "utilization": 0.3, "resets_at": soon},
                                 "seven_day": {"status": "allowed", "utilization": 0.75, "resets_at": soon}}) == 0.25
    assert placement.usage_left({"seven_day": {"status": "allowed_warning", "resets_at": soon}}) == placement.CLOSE
    assert placement.usage_left(USED_UP) == 0.0
