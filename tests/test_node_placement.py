"""Tests for jarvis.node.placement: where a job runs, by the user's word, their setting and an online check."""

import asyncio
import json
import time

import pytest
from test_node_link import mine, paired  # noqa: F401  (mine is a fixture)

from jarvis.brain import sdk, usage
from jarvis.node import machines, placement

FREE = {"same_protocol": True, "free": 2, "usage": {}, "online": True}
USED_UP = {"five_hour": {"status": "rejected", "resets_at": time.time() + 3600, "overage": "rejected"}}


class Hub:
    """Online checks answered from a table: machine id -> answer (None = offline)."""

    def __init__(self, answers: dict) -> None:
        self.answers = answers

    async def check(self, node: int, timeout: float = 5) -> dict | None:
        return self.answers.get(node)

    async def check_all(self, timeout: float = 5) -> dict:
        return {m.id: self.answers.get(m.id) for m in machines.paired()}


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


def choose(hub, kind=placement.TASKS, asked=""):
    return asyncio.run(placement.choose(hub, kind, {"title": "a job"}, asked))


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


def test_a_failed_choice_runs_here(mine, monkeypatch):
    studio, _ = paired("Studio")

    async def broken(*args, **kwargs):
        raise RuntimeError("down")

    monkeypatch.setattr(sdk, "ask_json", broken)
    assert choose(Hub({studio.id: FREE})) == placement.HERE


def test_when_this_computers_account_is_used_up_a_task_goes_to_a_machine(mine, monkeypatch):
    studio, _ = paired("Studio")
    machines.set_placement("tasks", machines.HERE)
    machines.set_placement("commands", machines.HERE)
    monkeypatch.setattr(usage, "_read", lambda: USED_UP)
    place = choose(Hub({studio.id: FREE}))
    assert place.node == studio.id and "hit its limit" in place.said
    assert choose(Hub({studio.id: FREE}), placement.COMMANDS) == placement.HERE  # commands use no Claude account
