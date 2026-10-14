"""Tests for jarvis.node's pairing and link: keys, sign-in both ways, signed requests, the online check."""

import asyncio
import time

import pytest

from jarvis.core import vault
from jarvis.node import agent, hub as hubs, keys, link, machines
from jarvis.node.link import Link, LinkDown, Refused, memory_pair


@pytest.fixture
def mine(own_profile, monkeypatch, tmp_path):
    """An isolated profile with a vault key, so the main computer's key can be made."""
    monkeypatch.setattr(vault, "ENV_FILE", tmp_path / "dotenv")
    vault.ensure()
    return own_profile


def paired(name: str = "Studio") -> tuple[machines.Machine, str]:
    """A machine paired and confirmed, with its private key."""
    key = keys.new()
    code = machines.new_code()["code"]
    found = machines.redeem(code, keys.public(key), {"name": name, "cores": 12, "tools": ["git", "ffmpeg"]})
    return machines.confirm(found.id), key


def connect(h: hubs.Hub, node: int, key: str, **options) -> agent.Agent:
    """A machine's agent whose dial reaches `h` through an in-memory channel."""
    async def dial():
        ours, theirs = memory_pair()
        asyncio.create_task(h.accept(theirs))
        return ours

    return agent.Agent(node, key, machines.main_public(), dial, **options)


async def until(check, seconds: float = 5) -> None:
    for _ in range(int(seconds * 100)):
        if check():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("timed out")


# ── Keys ────────────────────────────────────────────────────────


def test_a_signature_checks_out_only_for_its_own_key_and_data():
    a, b = keys.new(), keys.new()
    sig = keys.sign(a, b"hello")
    assert keys.verify(keys.public(a), b"hello", sig)
    assert not keys.verify(keys.public(a), b"hellO", sig)
    assert not keys.verify(keys.public(b), b"hello", sig)
    assert not keys.verify("not a key", b"hello", sig) and not keys.verify(keys.public(a), b"hello", "junk")


def test_a_fingerprint_is_short_and_the_same_on_both_screens():
    key = keys.public(keys.new())
    shown = keys.fingerprint(key)
    assert len(shown.split()) == 4 and shown == keys.fingerprint(key)


# ── Pairing ─────────────────────────────────────────────────────


def test_a_pairing_code_works_once_and_the_machine_waits_for_confirmation(mine):
    code = machines.new_code()["code"]
    key = keys.public(keys.new())
    found = machines.redeem(code.lower().replace("-", ""), key, {"name": "Studio", "cores": 12, "ram_gb": 64})
    assert (found.name, found.status, found.cores, found.ram_gb) == ("Studio", machines.PENDING, 12, 64.0)
    with pytest.raises(ValueError, match="wrong, used or expired"):
        machines.redeem(code, key, {"name": "Again"})
    assert machines.paired() == []  # not until the user checks the fingerprint
    assert machines.confirm(found.id).paired


def test_an_expired_or_made_up_code_pairs_nothing(mine):
    code = machines.new_code(now=time.time() - machines.CODE_MINUTES * 60 - 1)["code"]
    for wrong in (code, "ABCD-EFGH"):
        with pytest.raises(ValueError):
            machines.redeem(wrong, keys.public(keys.new()), {})
    assert machines.load() == []


def test_a_malformed_key_is_refused(mine):
    with pytest.raises(ValueError, match="isn't a machine key"):
        machines.redeem(machines.new_code()["code"], "nonsense", {})


def test_machines_get_unique_names_found_by_name_or_alias(mine):
    one, _ = paired("Studio")
    two, _ = paired("Studio")
    assert (one.name, two.name) == ("Studio", "Studio 2")
    machines.rename(one.id, "Render box", ["the big Mac"])
    assert machines.find("the render box").id == one.id and machines.find("big mac").id == one.id
    with pytest.raises(ValueError, match="Render box, Studio 2"):
        machines.find("laptop")
    with pytest.raises(ValueError, match="means this computer"):
        machines.rename(one.id, "here")


# ── Where jobs run ──────────────────────────────────────────────


def test_placement_is_this_computer_until_a_machine_is_paired_then_jarvis_chooses(mine):
    assert machines.placement() == {"commands": machines.HERE, "tasks": machines.HERE}
    machines.redeem(machines.new_code()["code"], keys.public(keys.new()), {"name": "Studio"})
    assert machines.placement()["tasks"] == machines.HERE  # pending: nowhere else to run yet
    found, _ = paired("Other")
    assert machines.placement() == {"commands": machines.AUTO, "tasks": machines.AUTO}
    machines.set_placement("commands", str(found.id))
    assert machines.where(machines.placement()["commands"]) == "Other"
    with pytest.raises(ValueError):
        machines.set_placement("tasks", "999")


def test_a_choice_the_user_made_survives_pairing_another_machine(mine):
    paired("Studio")
    machines.set_placement("tasks", machines.HERE)
    paired("Other")
    assert machines.placement() == {"commands": machines.AUTO, "tasks": machines.HERE}


def test_removing_the_machine_jobs_go_to_moves_them_back(mine):
    found, _ = paired("Studio")
    machines.set_placement("commands", str(found.id))
    machines.remove(found.id)
    assert machines.placement() == {"commands": machines.HERE, "tasks": machines.HERE}


# ── Sign-in and requests ────────────────────────────────────────


def test_both_sides_prove_who_they_are_and_requests_are_answered(mine):
    async def main():
        found, key = paired()
        main_key = machines.main_key()
        ours, theirs = memory_pair()
        machine_side = asyncio.create_task(
            link.handshake_machine(ours, found.id, key, machines.main_public(), {"cores": 12}))
        node, main_session, facts = await link.handshake_main(theirs, main_key, machines.public_of)
        await theirs.send({"kind": "welcome", "body": {"hi": 1}})
        session, welcome = await machine_side
        assert (node, main_session, facts["cores"], welcome) == (found.id, session, 12, {"hi": 1})

        async def echo(body):
            return {"heard": body["say"]}

        machine = Link(ours, session, key, machines.main_public(), {"echo": echo})
        main = Link(theirs, main_session, main_key, found.public_key)
        serving = [asyncio.create_task(machine.serve()), asyncio.create_task(main.serve())]
        assert await main.call("echo", {"say": "hi"}) == {"heard": "hi"}
        with pytest.raises(Refused, match="no nothing here"):
            await main.call("nothing")
        await main.close()
        await asyncio.gather(*serving)
        with pytest.raises(LinkDown):
            await main.call("echo", {"say": "hi"})

    asyncio.run(main())


def test_a_machine_with_someone_elses_key_is_turned_away(mine):
    async def main():
        found, _ = paired()
        ours, theirs = memory_pair()
        main_side = asyncio.create_task(link.handshake_main(theirs, machines.main_key(), machines.public_of))
        with pytest.raises(Refused, match="doesn't match"):
            await link.handshake_machine(ours, found.id, keys.new(), machines.main_public(), {})
        with pytest.raises(Refused):
            await main_side

    asyncio.run(main())


def test_a_machine_checks_it_reached_the_right_main_computer(mine):
    async def main():
        found, key = paired()
        ours, theirs = memory_pair()
        impostor = asyncio.create_task(link.handshake_main(theirs, keys.new(), machines.public_of))
        with pytest.raises(Refused, match="couldn't prove who it is"):
            await link.handshake_machine(ours, found.id, key, machines.main_public(), {})
        impostor.cancel()

    asyncio.run(main())


def test_unsigned_or_replayed_requests_are_dropped(mine):
    async def main():
        a, b = memory_pair()
        key_a, key_b = keys.new(), keys.new()
        got = []

        async def note(body):
            got.append(body["n"])
            return {}

        receiver = Link(b, "s1", key_b, keys.public(key_a), {"note": note})
        serving = asyncio.create_task(receiver.serve())
        sender = Link(a, "s1", key_a, keys.public(key_b))
        await sender.tell("note", {"n": 1})
        sig = keys.sign(key_a, link._signed("s1", 1, "note", {"n": 1}))
        await a.send({"id": 1, "kind": "note", "body": {"n": 1}, "sig": sig, "quiet": 1})  # the same again: a replay
        await a.send({"id": 5, "kind": "note", "body": {"n": 2}, "sig": "forged", "quiet": 1})
        other = keys.sign(key_a, link._signed("another session", 6, "note", {"n": 3}))
        await a.send({"id": 6, "kind": "note", "body": {"n": 3}, "sig": other, "quiet": 1})
        await sender.tell("note", {"n": 4})
        await until(lambda: got == [1, 4])
        await a.close()
        await serving

    asyncio.run(main())


# ── The hub and a machine's agent ───────────────────────────────


def test_a_paired_machine_connects_and_answers_the_online_check(mine):
    async def main():
        found, key = paired()
        h = hubs.Hub()
        machine = connect(h, found.id, key)
        running = asyncio.create_task(machine.run())
        await until(lambda: h.connected(found.id) is not None)
        check = await h.check(found.id)
        assert check["online"] and check["same_protocol"] and check["free"] >= 1
        listing = await h.listing()
        shown = listing["machines"][0]
        assert (shown["name"], shown["online"], shown["takes_work"]) == ("Studio", True, True)
        assert machines.get(found.id).last_seen > 0
        machine.stop()
        await running
        await until(lambda: h.connected(found.id) is None)
        assert await h.check(found.id) is None  # never an old "online" flag

    asyncio.run(main())


def test_an_unconfirmed_machine_is_told_to_wait(mine):
    async def main():
        key = keys.new()
        found = machines.redeem(machines.new_code()["code"], keys.public(key), {"name": "Studio"})
        h = hubs.Hub()
        machine = connect(h, found.id, key)
        with pytest.raises(Refused, match="waiting to be confirmed"):
            await machine.connect_once()
        assert h.connected(found.id) is None

    asyncio.run(main())


def test_a_removed_machine_loses_its_link_and_cant_come_back(mine):
    async def main():
        found, key = paired()
        h = hubs.Hub()
        machine = connect(h, found.id, key)
        running = asyncio.create_task(machine.run())
        await until(lambda: h.connected(found.id) is not None)
        assert (await h.remove(found.id))["removed"] == "Studio"
        with pytest.raises(Refused, match="isn't paired"):
            await machine.connect_once()
        machine.stop()
        await running

    asyncio.run(main())


def test_a_machine_dials_again_after_its_link_drops(mine, monkeypatch):
    monkeypatch.setattr(agent, "BACKOFF", (0.01,))

    async def main():
        found, key = paired()
        h = hubs.Hub()
        machine = connect(h, found.id, key)
        running = asyncio.create_task(machine.run())
        await until(lambda: h.connected(found.id) is not None)
        first = h.connected(found.id)
        await first.close()
        await until(lambda: h.connected(found.id) not in (None, first))
        assert (await h.check(found.id))["online"]
        machine.stop()
        await running

    asyncio.run(main())


def test_the_welcome_brings_the_users_name_and_the_code_to_run(mine, monkeypatch):
    async def main():
        found, key = paired()
        h = hubs.Hub()
        welcome = await h.welcome(found)
        assert welcome["user"] == mine.name and welcome["protocol"] == link.PROTOCOL and "commit" in welcome
        assert "quiet_hours" not in welcome  # a machine runs no watches or notices: those stay on the main computer

    asyncio.run(main())


def test_ws_url_follows_the_main_computers_scheme():
    assert agent.ws_url("https://main.example.ts.net/") == "wss://main.example.ts.net/api/node"
    assert agent.ws_url("http://localhost:8787") == "ws://localhost:8787/api/node"
