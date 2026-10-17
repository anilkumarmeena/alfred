"""Run JARVIS's own test suite on a task branch before merging it (git_ops.merge_op).

Runs in the task's worktree (or a temporary checkout of the branch), using the main .venv unless the worktree has
its own. Failures not in ENV_ONLY are re-run on the target branch in the main checkout (whole files); those that
fail there too are pre-existing. Any remaining failure blocks the merge. A stopped merge kills the run, pytest's
children included, and removes the temporary checkout.

Every run of the suite notes which tests ran and failed on which code (`record`, called by tests/conftest.py), keyed
by the git tree a commit of that code has. A branch whose exact tree already had every test it collects run, with
no failures beyond ENV_ONLY (the task's own final run, usually), isn't run again (`passed`).
"""

import asyncio
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from collections.abc import Collection, Sequence
from pathlib import Path

from jarvis import projects
from jarvis.config import WORKTREES_DIR
from jarvis.core import proc
from jarvis.core.files import write_atomic

TIMEOUT = 600  # seconds for one run of the suite
RECORDS = "jarvis-tested"  # folder in the repo's git dir: <tree>.json per run of the suite
RECORD_DAYS = 14  # records older than this are dropped
SHOWN = 5  # failing tests listed in a refusal
# Tests that always fail in a worktree or task sandbox (missing local assets), regardless of the branch
ENV_ONLY = {
    "tests/test_panel.py::test_preferences_kept_from_the_panel_and_read_by_config",  # needs Kokoro voices in models/
    "tests/test_push.py::test_service_worker_is_served_from_the_root",  # needs web/dist
    "tests/test_task_client.py::test_the_runner_stopping_mid_stream_is_it_being_down_not_an_error",  # /tmp socket
}
FAILED = re.compile(r"^(?:FAILED|ERROR) (.+?)(?: - .*)?$", re.M)  # pytest -rfE summary lines


async def _pytest(folder: Path, venv: Path, *args: str) -> tuple[int, str]:
    """Run pytest in `folder` with .venv `venv`: (exit code, output)."""
    env = {k: v for k, v in os.environ.items() if k != "VIRTUAL_ENV"} | {"UV_PROJECT_ENVIRONMENT": str(venv)}
    return await proc.run_try("uv", "run", "--no-sync", "--with", "pytest", "pytest", "-q", "-p", "no:cacheprovider",
                              *args, timeout=TIMEOUT, cwd=folder, env=env, group=True)


async def failures(folder: Path, venv: Path, tests: Sequence[str] = ()) -> set[str]:
    """Failing pytest node ids in `folder` (optionally limited to files `tests`), using .venv `venv`.

    Raises RuntimeError if pytest couldn't run, TimeoutError past TIMEOUT.
    """
    code, out = await _pytest(folder, venv, "-rfE", *tests)
    failed = set(FAILED.findall(out))
    if code not in (0, 1, 5) and not failed:  # 5: nothing collected (e.g. all skipped)
        raise RuntimeError(" ".join(out.splitlines()[-3:])[:300] or f"pytest exited with {code}")
    return failed


# ── Runs already made on the same code ──────────────────────────


def code_tree(folder: Path) -> str | None:
    """The git tree a task's commit of `folder` as it is now would have (projects.PATHSPEC); None if there's none."""
    with tempfile.TemporaryDirectory() as tmp:
        index = Path(tmp) / "index"
        env = os.environ | {"GIT_INDEX_FILE": str(index)}  # leaves the folder's own index alone
        try:
            own = subprocess.run(["git", "-C", str(folder), "rev-parse", "--path-format=absolute", "--git-path",
                                  "index"], capture_output=True, text=True, check=True, timeout=30).stdout.strip()
            # A copy of the folder's own index, as commit_op adds to: its stat cache spares hashing every file again
            steps = [("add", "-A", *projects.PATHSPEC), ("write-tree",)]
            if own and Path(own).is_file():
                shutil.copyfile(own, index)
            else:
                steps.insert(0, ("read-tree", "HEAD"))
            for args in steps:
                out = subprocess.run(["git", "-C", str(folder), *args], env=env, capture_output=True, text=True,
                                     check=True, timeout=60).stdout
        except (OSError, subprocess.SubprocessError):
            return None
    return out.strip() or None


def _records(folder: Path) -> Path | None:
    """The folder of records of suite runs for `folder`'s repo, shared by all its worktrees; None if git can't say."""
    try:
        done = subprocess.run(["git", "-C", str(folder), "rev-parse", "--path-format=absolute", "--git-common-dir"],
                              capture_output=True, text=True, check=False, timeout=30)
    except (OSError, subprocess.SubprocessError):  # a timeout, too
        return None
    return Path(done.stdout.strip()) / RECORDS if done.returncode == 0 and done.stdout.strip() else None


def record(folder: Path, tree: str, ran: Collection[str], failed: Collection[str]) -> None:
    """Note that a run of tests on code `tree` in `folder` ran `ran` (node ids, passed or skipped) and `failed`, unless
    an earlier run on that code ran some test this one didn't; and drop records older than RECORD_DAYS."""
    if not (ran or failed) or not (records := _records(folder)):
        return
    records.mkdir(exist_ok=True)
    file = records / f"{tree}.json"
    try:
        before = json.loads(file.read_text())
        if not {*before["ran"], *before["failed"]} <= {*ran, *failed}:
            return  # a part of the suite run after the whole of it
    except (OSError, ValueError):
        pass
    old = time.time() - RECORD_DAYS * 86400
    for stale in records.glob("*.json"):
        if stale.stat().st_mtime < old:
            stale.unlink(missing_ok=True)
    write_atomic(file, json.dumps({"ran": sorted(ran), "failed": sorted(failed)}))


async def passed(folder: Path, venv: Path, branch: str) -> bool:
    """Whether every test `branch` (checked out in `folder`) collects has already been run on exactly its code, with
    no failures but ENV_ONLY's. Costs a collection (seconds), not a run of the suite."""
    code, tree = await proc.run_try("git", "-C", str(folder), "rev-parse", "-q", "--verify", f"{branch}^{{tree}}")
    try:
        records = await asyncio.to_thread(_records, folder)
        found = json.loads((records / f"{tree.strip()}.json").read_text()) if records and not code else None
    except (OSError, ValueError):
        found = None
    if not found or set(found["failed"]) - ENV_ONLY:
        return False
    code, out = await _pytest(folder, venv, "--collect-only")
    collected = {line for line in out.splitlines() if "::" in line and not line[0].isspace()}  # -q: one id a line
    return code == 0 and bool(collected) and collected <= {*found["ran"], *found["failed"]}


async def _branch_failures(folder: Path, venv: Path, branch: str) -> set[str]:
    """Failing tests in `folder`, where `branch` is checked out: none if its code has already passed (`passed`)."""
    return set() if await passed(folder, venv, branch) else await failures(folder, venv)


async def new_failures(repo: Path, branch: str, worktree: str | None) -> list[str]:
    """Tests failing on `branch` but not on the main checkout at `repo`."""
    venv = repo / ".venv"
    if worktree and Path(worktree).is_dir():
        folder = Path(worktree)
        failed = await _branch_failures(folder, folder / ".venv" if (folder / ".venv").is_dir() else venv, branch)
    else:
        WORKTREES_DIR.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="merge-check-", dir=WORKTREES_DIR) as tmp:
            await proc.run("git", "-C", str(repo), "worktree", "add", "--detach", tmp, branch, timeout=120)
            try:
                failed = await _branch_failures(Path(tmp), venv, branch)
            finally:
                await proc.run_try("git", "-C", str(repo), "worktree", "remove", "--force", tmp, timeout=120)
    failed -= ENV_ONLY
    files = sorted({f for f in (t.partition("::")[0] for t in failed) if (repo / f).is_file()})
    if files:
        failed -= await failures(repo, venv, files)
    return sorted(failed)


async def refusal(repo: Path, branch: str, worktree: str | None, into: str) -> str:
    """Spoken reason the tests block merging `branch` into `into`; "" if they pass."""
    try:
        failed = await new_failures(repo, branch, worktree)
    except TimeoutError:
        return f"its tests didn't finish within {TIMEOUT // 60} minutes"
    except RuntimeError as e:
        return f"its tests couldn't run: {e}"
    if not failed:
        return ""
    more = f", and {len(failed) - SHOWN} more" if len(failed) > SHOWN else ""
    said = "test fails" if len(failed) == 1 else "tests fail"
    return f"{len(failed)} {said} on it that pass on {into}: {', '.join(failed[:SHOWN])}{more}"
