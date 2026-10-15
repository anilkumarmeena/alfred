"""Run JARVIS's own test suite on a task branch before merging it (git_ops.merge_op).

Runs in the task's worktree (or a temporary checkout of the branch), using the main .venv unless the worktree has
its own. Failures not in ENV_ONLY are re-run on the target branch in the main checkout (whole files); those that
fail there too are pre-existing. Any remaining failure blocks the merge. A stopped merge kills the run, pytest's
children included, and removes the temporary checkout.
"""

import os
import re
import tempfile
from collections.abc import Sequence
from pathlib import Path

from jarvis.core import proc
from jarvis.core.config import WORKTREES_DIR

TIMEOUT = 600  # seconds for one run of the suite
SHOWN = 5  # failing tests listed in a refusal
# Tests that always fail in a worktree or task sandbox (missing local assets), regardless of the branch
ENV_ONLY = {
    "tests/test_panel.py::test_preferences_kept_from_the_panel_and_read_by_config",  # needs Kokoro voices in models/
    "tests/test_push.py::test_service_worker_is_served_from_the_root",  # needs web/dist
    "tests/test_task_client.py::test_the_runner_stopping_mid_stream_is_it_being_down_not_an_error",  # /tmp socket
}
FAILED = re.compile(r"^(?:FAILED|ERROR) (.+?)(?: - .*)?$", re.M)  # pytest -rfE summary lines


async def failures(folder: Path, venv: Path, tests: Sequence[str] = ()) -> set[str]:
    """Failing pytest node ids in `folder` (optionally limited to files `tests`), using .venv `venv`.

    Raises RuntimeError if pytest couldn't run, TimeoutError past TIMEOUT.
    """
    env = {k: v for k, v in os.environ.items() if k != "VIRTUAL_ENV"} | {"UV_PROJECT_ENVIRONMENT": str(venv)}
    code, out = await proc.run_try("uv", "run", "--no-sync", "--with", "pytest", "pytest", "-q", "-rfE",
                                   "-p", "no:cacheprovider", *tests, timeout=TIMEOUT, cwd=folder, env=env, group=True)
    failed = set(FAILED.findall(out))
    if code not in (0, 1, 5) and not failed:  # 5: nothing collected (e.g. all skipped)
        raise RuntimeError(" ".join(out.splitlines()[-3:])[:300] or f"pytest exited with {code}")
    return failed


async def new_failures(repo: Path, branch: str, worktree: str | None) -> list[str]:
    """Tests failing on `branch` but not on the main checkout at `repo`."""
    venv = repo / ".venv"
    if worktree and Path(worktree).is_dir():
        folder = Path(worktree)
        failed = await failures(folder, folder / ".venv" if (folder / ".venv").is_dir() else venv)
    else:
        WORKTREES_DIR.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="merge-check-", dir=WORKTREES_DIR) as tmp:
            await proc.run("git", "-C", str(repo), "worktree", "add", "--detach", tmp, branch, timeout=120)
            try:
                failed = await failures(Path(tmp), venv)
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
