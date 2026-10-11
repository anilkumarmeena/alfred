"""A machine keeps its JARVIS on the same commit as the main computer's, updating itself from the main computer.

On connecting, the main computer says which commit it runs. A machine on another one waits until it's idle (its runs
and jobs finished), fetches a bundle of that commit over the link (no GitHub login needed, and unpushed commits come
too), checks it out, installs its locked dependencies and restarts. If the new code fails to start STARTS_ALLOWED
times in a row, the next start rolls back to the commit before, and the main computer is told once.

`settle` runs first thing at start-up and imports nothing beyond the standard library, so a broken update can't stop
the rollback.
"""

import asyncio
import json
import logging
import os
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

log = logging.getLogger("jarvis.node.update")

STARTS_ALLOWED = 3  # starts of a new commit that may fail to connect before rolling back
IDLE_POLL = 10.0  # seconds between checks for idleness while an update waits
RESTART = 75  # exit status asking the service manager to start it again (it restarts on any exit)
SYNC_MINUTES = 15


def _state_file(root: Path) -> Path:
    return root / "data" / "node" / "update.json"


def _read(root: Path) -> dict:
    try:
        found = json.loads(_state_file(root).read_text())
    except (OSError, ValueError):
        return {}
    return found if isinstance(found, dict) else {}


def _write(root: Path, state: dict) -> None:
    path = _state_file(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state))
    os.replace(tmp, path)


def _sync_dependencies(root: Path) -> tuple[bool, str]:
    """uv sync from the checked-out lockfile; (worked, last output)."""
    try:
        done = subprocess.run(["uv", "sync", "--frozen"], cwd=root, capture_output=True, text=True,
                              timeout=SYNC_MINUTES * 60)
    except (OSError, subprocess.SubprocessError) as e:
        return False, str(e)
    return done.returncode == 0, (done.stderr or done.stdout)[-500:]


def settle(root: Path) -> str:
    """At start-up: count a start of a just-updated commit, rolling back once it has failed too often.

    Returns what happened ("" if nothing to do), for the log and the main computer."""
    state = _read(root)
    if state.get("status") != "trying":
        return ""
    state["starts"] = int(state.get("starts") or 0) + 1
    if state["starts"] <= STARTS_ALLOWED:
        _write(root, state)
        return ""
    previous = str(state.get("previous") or "")
    done = subprocess.run(["git", "-C", str(root), "checkout", "-q", "--detach", previous], capture_output=True, text=True)
    synced, out = _sync_dependencies(root) if done.returncode == 0 else (False, done.stderr)
    said = (f"the update to {str(state.get('commit'))[:7]} didn't start {STARTS_ALLOWED} times, so it went back to "
            f"{previous[:7]}" + ("" if done.returncode == 0 and synced else f", and going back failed too: {out}"))
    _write(root, {"status": "rolled back", "said": said, "commit": state.get("commit"), "at": time.time()})
    return said


def started(root: Path) -> str:
    """Once connected after an update: it worked. Returns a rollback's story still to tell the main computer, or ""."""
    state = _read(root)
    if state.get("status") == "trying":
        _write(root, {"status": "done", "commit": state.get("commit"), "at": time.time()})
    elif state.get("status") == "rolled back" and not state.get("told"):
        _write(root, state | {"told": True})
        return str(state.get("said") or "")
    return ""


def failed_commit(root: Path) -> str:
    """A commit that was rolled back from, so it isn't tried again until the main computer moves on."""
    state = _read(root)
    return str(state.get("commit") or "") if state.get("status") == "rolled back" else ""


class Updater:
    """Waits for idleness, then updates to the commit the main computer runs and restarts.

    `busy()` says whether runs or jobs are going; `fetch(have, into)` gets the bundle (returns the commit it holds);
    `restart()` ends the process for the service manager to start again."""

    def __init__(self, root: Path, busy: Callable[[], bool], restart: Callable[[], None]) -> None:
        self.root, self.busy, self.restart = root, busy, restart
        self.wanted = ""
        self.problem = ""
        self._going: asyncio.Task | None = None

    @property
    def pending(self) -> bool:
        return bool(self.wanted)

    def want(self, commit: str, have: str, fetch) -> None:
        """The main computer runs `commit`: update to it when idle, unless it's this one or one that failed."""
        if not commit or commit == have or commit == failed_commit(self.root):
            self.wanted = ""
            return
        self.wanted = commit
        if self._going is None or self._going.done():
            self._going = asyncio.create_task(self._when_idle(have, fetch))

    async def _when_idle(self, have: str, fetch) -> None:
        while self.busy():
            await asyncio.sleep(IDLE_POLL)
        commit = self.wanted
        if not commit:
            return
        try:
            await self.apply(have, commit, fetch)
        except Exception as e:
            self.problem = f"couldn't update to {commit[:7]}: {e}"[:300]
            log.warning(self.problem)
            self.wanted = ""
            return
        self.restart()

    async def apply(self, have: str, commit: str, fetch) -> None:
        """Fetch, check out and install `commit`, keeping `have` to roll back to. RuntimeError on failure, after
        putting the old commit back."""
        from jarvis.node import bundles

        if await bundles.dirty(self.root):
            raise RuntimeError("this machine's JARVIS has uncommitted changes, which an update would overwrite")
        spool = self.root / "data" / "node" / "spool"
        bundle = spool / f"update-{commit[:12]}.bundle"
        try:
            got = await fetch(have, bundle)
            if got != commit:
                raise RuntimeError("the main computer moved on while sending its code; trying again later")
            await bundles.fetch_update(self.root, bundle, commit)
        finally:
            bundle.unlink(missing_ok=True)
        await bundles.check_out(self.root, commit)
        synced, out = await asyncio.to_thread(_sync_dependencies, self.root)
        if not synced:
            await bundles.check_out(self.root, have)
            await asyncio.to_thread(_sync_dependencies, self.root)
            raise RuntimeError(f"its dependencies didn't install: {out}")
        _write(self.root, {"status": "trying", "previous": have, "commit": commit, "starts": 0, "at": time.time()})
        log.info("updated to %s; restarting", commit[:7])
