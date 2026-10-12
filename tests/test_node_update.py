"""Tests for a machine updating itself to the main computer's commit: bundles over the link, idleness, rollback."""

import asyncio
import json
import subprocess
from pathlib import Path

import pytest
from test_node_link import connect, mine, paired, until  # noqa: F401  (mine is a fixture)

from jarvis.node import bundles, hub as hubs, update


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repos(tmp_path, monkeypatch):
    """The main computer's checkout two commits ahead of the machine's clone."""
    gitconfig = tmp_path / "gitconfig"
    gitconfig.write_text("")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(gitconfig))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for who in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{who}_NAME", "Test")
        monkeypatch.setenv(f"GIT_{who}_EMAIL", "test@example.com")
    main = tmp_path / "main"
    main.mkdir()
    git(main, "init", "-q", "-b", "main")
    (main / "app.py").write_text("v1\n")
    git(main, "add", "-A")
    git(main, "commit", "-q", "-m", "one")
    machine = tmp_path / "machine"
    git(tmp_path, "clone", "-q", str(main), str(machine))
    for n in (2, 3):
        (main / "app.py").write_text(f"v{n}\n")
        git(main, "commit", "-qam", f"v{n}")
    synced: list[Path] = []
    monkeypatch.setattr(update, "_sync_dependencies", lambda root: (synced.append(root), (True, ""))[1])
    return {"main": main, "machine": machine, "synced": synced}


def test_a_machine_on_an_older_commit_fetches_the_main_computers_and_restarts(mine, repos):
    async def main():
        found, key = paired()
        h = hubs.Hub(root=repos["main"], spool=repos["main"].parent / "spool")
        restarted = asyncio.Event()
        machine = connect(h, found.id, key, root=repos["machine"], restart=restarted.set)
        running = asyncio.create_task(machine.run())
        await asyncio.wait_for(restarted.wait(), 20)
        assert await bundles.head(repos["machine"]) == git(repos["main"], "rev-parse", "HEAD")
        assert (repos["machine"] / "app.py").read_text() == "v3\n"
        assert repos["synced"] == [repos["machine"]]
        state = json.loads((repos["machine"] / "data/node/update.json").read_text())
        assert state["status"] == "trying" and state["previous"] == git(repos["machine"], "rev-parse", "HEAD~2")
        assert not list((repos["main"].parent / "spool").glob("*"))  # the bundle was removed once fetched
        machine.stop()
        await running

    asyncio.run(main())


def test_an_update_waits_until_the_machine_is_idle(mine, repos, monkeypatch):
    monkeypatch.setattr(update, "IDLE_POLL", 0.02)

    async def main():
        found, key = paired()
        h = hubs.Hub(root=repos["main"], spool=repos["main"].parent / "spool")
        restarted = asyncio.Event()
        machine = connect(h, found.id, key, root=repos["machine"], restart=restarted.set)
        work = {"runs": 1}
        machine.busy = lambda: work["runs"]
        running = asyncio.create_task(machine.run())
        await until(lambda: machine.updater.pending)
        await asyncio.sleep(0.2)
        assert not restarted.is_set() and (repos["machine"] / "app.py").read_text() == "v1\n"
        assert (await h.check(found.id))["updating"]
        work["runs"] = 0
        await asyncio.wait_for(restarted.wait(), 20)
        machine.stop()
        await running

    asyncio.run(main())


def test_uncommitted_changes_on_the_machine_block_the_update(mine, repos):
    async def main():
        found, key = paired()
        h = hubs.Hub(root=repos["main"], spool=repos["main"].parent / "spool")
        machine = connect(h, found.id, key, root=repos["machine"], restart=lambda: None)
        (repos["machine"] / "app.py").write_text("changed here\n")
        running = asyncio.create_task(machine.run())
        await until(lambda: bool(machine.updater.problem))
        assert "uncommitted changes" in machine.updater.problem
        assert "uncommitted changes" in (await h.check(found.id))["update_problem"]
        machine.stop()
        await running

    asyncio.run(main())


def test_dependencies_that_fail_to_install_put_the_old_commit_back(mine, repos, monkeypatch):
    monkeypatch.setattr(update, "_sync_dependencies", lambda root: (False, "no such package"))

    async def main():
        found, key = paired()
        h = hubs.Hub(root=repos["main"], spool=repos["main"].parent / "spool")
        before = git(repos["machine"], "rev-parse", "HEAD")
        machine = connect(h, found.id, key, root=repos["machine"], restart=lambda: None)
        running = asyncio.create_task(machine.run())
        await until(lambda: bool(machine.updater.problem))
        assert "didn't install" in machine.updater.problem
        assert git(repos["machine"], "rev-parse", "HEAD") == before
        machine.stop()
        await running

    asyncio.run(main())


def test_a_new_commit_that_keeps_failing_to_start_is_rolled_back_once(repos):
    machine = repos["machine"]
    before = git(machine, "rev-parse", "HEAD")
    git(machine, "fetch", "-q", str(repos["main"]), "main")
    new = git(machine, "rev-parse", "FETCH_HEAD")
    git(machine, "checkout", "-q", "--detach", new)
    update._write(machine, {"status": "trying", "previous": before, "commit": new, "starts": 0})
    for _ in range(update.STARTS_ALLOWED):
        assert update.settle(machine) == ""
    said = update.settle(machine)
    assert "went back to" in said and git(machine, "rev-parse", "HEAD") == before
    assert update.failed_commit(machine) == new  # not tried again until the main computer moves on
    assert update.started(machine) == said and update.started(machine) == ""  # told once


def test_starting_fine_after_an_update_settles_it(repos):
    update._write(repos["machine"], {"status": "trying", "previous": "abc", "commit": "def", "starts": 1})
    assert update.started(repos["machine"]) == ""
    assert update.settle(repos["machine"]) == ""
    assert json.loads((repos["machine"] / "data/node/update.json").read_text())["status"] == "done"
