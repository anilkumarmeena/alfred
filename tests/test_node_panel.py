"""Tests for machines in the web server and panel: pairing over HTTP, confirming, renaming, where jobs run."""

import asyncio
import json

import pytest
from test_node_link import connect, mine, paired, until  # noqa: F401  (mine is a fixture)

from jarvis import panel, server
from jarvis.auth import logins
from jarvis.node import hub as hubs, keys, machines
from jarvis.state import activity
from jarvis.tasks import client
from jarvis.tools import machines as machine_tools


class PairRequest:
    remote = "100.64.0.9"

    def __init__(self, body: dict) -> None:
        self.body = body

    async def json(self) -> dict:
        return self.body


def pair(body: dict) -> tuple[int, dict]:
    reply = asyncio.run(server.node_pair(PairRequest(body)))
    return reply.status, json.loads(reply.body)


@pytest.fixture
def runner_hub(mine, monkeypatch):
    """The panel's runner calls answered by a hub in this process."""
    h = hubs.Hub()
    monkeypatch.setattr(client, "machines", h.listing)
    monkeypatch.setattr(client, "machine_remove", h.remove)
    return h


def call(name: str, body: dict | None = None):
    return asyncio.run(panel.call(name, body or {}))


def owner() -> dict:
    """A panel call's body from the owner's login, as the web server sets it."""
    return {"login": logins.owner()}


def test_a_machine_pairs_over_http_with_a_code_and_gets_this_computers_key(mine, monkeypatch):
    monkeypatch.setattr(server, "_failures", [])
    code = call("machine_code", owner())
    key = keys.public(keys.new())
    status, got = pair({"code": code["code"], "public_key": key, "facts": {"name": "Studio", "cores": 12}})
    assert status == 200 and got["main_public"] == machines.main_public() and got["name"] == "Studio"
    assert code["main_fingerprint"] == keys.fingerprint(got["main_public"])
    assert got["fingerprint"] == keys.fingerprint(key)
    assert "asks to be paired" in activity.recent()[0]["text"]
    status, got = pair({"code": code["code"], "public_key": key, "facts": {}})
    assert status == 403 and "wrong, used or expired" in got["error"]
    assert len(server._failures) == 1  # counts toward the lockout


def test_too_many_wrong_codes_lock_pairing_out(mine, monkeypatch):
    monkeypatch.setattr(server, "_failures", [])
    for _ in range(server.MAX_FAILURES):
        pair({"code": "ABCD-EFGH", "public_key": keys.public(keys.new())})
    status, got = pair({"code": call("machine_code", owner())["code"], "public_key": keys.public(keys.new())})
    assert status == 429 and machines.load() == []


def test_the_routes_are_there():
    routes = {(r.method, r.resource.canonical) for r in server.app().router.routes()}
    assert {("POST", "/api/node/pair"), ("GET", "/api/node")} <= routes


def test_confirming_renaming_and_placing_from_the_panel(runner_hub):
    key = keys.new()
    found = machines.redeem(machines.new_code()["code"], keys.public(key), {"name": "Studio"})
    shown = call("machines")
    assert shown["machines"][0]["status"] == machines.PENDING and shown["machines"][0]["online"] is False
    assert shown["fingerprint"] == keys.fingerprint(machines.main_public())
    shown = call("machine_confirm", owner() | {"id": found.id})
    assert shown["machines"][0]["status"] == machines.PAIRED
    assert shown["placement"] == {"commands": machines.AUTO, "tasks": machines.AUTO}
    shown = call("machine_save", {"id": found.id, "name": "Render box", "aliases": "the studio, studio mac"})
    assert (shown["machines"][0]["name"], shown["machines"][0]["aliases"]) == ("Render box", ["the studio", "studio mac"])
    shown = call("placement_save", {"kind": "tasks", "value": machines.HERE})
    assert shown["placement"]["tasks"] == machines.HERE
    with pytest.raises(ValueError):
        call("placement_save", {"kind": "research", "value": machines.AUTO})


def test_only_the_owners_login_pairs_confirms_or_forgets_a_machine(runner_hub, tmp_path, monkeypatch):
    monkeypatch.setattr(logins, "FILE", tmp_path / "logins.json")
    logins.add("Alex", "alex-secret")  # the first login: the owner's
    logins.add("Sam", "sam-secret")
    found = machines.redeem(machines.new_code()["code"], keys.public(keys.new()), {"name": "Studio"})
    for name, body in (("machine_code", {}), ("machine_confirm", {"id": found.id}), ("machine_remove", {"id": found.id})):
        with pytest.raises(PermissionError, match="Only Alex's login"):
            call(name, {"login": "Sam"} | body)
    assert machines.need(found.id).status == machines.PENDING
    shown = call("machines", {"login": "Sam"})
    assert shown["can_pair"] is False and shown["owner"] == "Alex"
    shown = call("machine_confirm", {"login": "Alex", "id": found.id})
    assert shown["can_pair"] is True and shown["machines"][0]["status"] == machines.PAIRED
    assert call("placement_save", {"login": "Sam", "kind": "tasks", "value": machines.HERE})["placement"]["tasks"] == \
        machines.HERE  # where jobs run is anyone's to say


def test_removing_from_the_panel_drops_the_live_link(runner_hub):
    async def main():
        found, key = paired()
        machine = connect(runner_hub, found.id, key)
        running = asyncio.create_task(machine.run())
        await until(lambda: runner_hub.connected(found.id) is not None)
        shown = await panel.call("machines", {})
        assert shown["machines"][0]["online"] is True
        shown = await panel.call("machine_remove", owner() | {"id": found.id})
        assert shown["machines"] == [] and runner_hub.connected(found.id) is None
        machine.stop()
        await running

    asyncio.run(main())


def test_the_brain_hears_which_machines_are_online_and_where_jobs_run(runner_hub):
    async def main():
        found, key = paired()
        machine = connect(runner_hub, found.id, key)
        running = asyncio.create_task(machine.run())
        await until(lambda: runner_hub.connected(found.id) is not None)
        machines.set_placement("commands", str(found.id))
        told = await machine_tools.machines_list()
        assert told["machines"][0]["name"] == "Studio" and told["machines"][0]["online"]
        assert told["machines"][0]["runs"] == ["Long commands"]
        assert told["where_jobs_run"] == {"Long commands": "Studio", "Claude tasks": "let JARVIS choose",
                                          "Research": "this computer, always"}
        machine.stop()
        await running

    asyncio.run(main())
