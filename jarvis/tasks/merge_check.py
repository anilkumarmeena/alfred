"""JARVIS's tests, run before one of its own task branches is merged (Runner._merge).

The suite runs on the branch, in the task's worktree (or a checkout of the branch made for the run, if that's gone),
with the main checkout's .venv unless the worktree has its own. A test that fails there and isn't known to fail only
for want of what a worktree lacks (ENV_ONLY) is run again on the branch being merged into, in the main checkout, its
whole test file at once: one that fails there too isn't the branch's doing. Any other refuses the merge, named.
"""

import os
import re
import tempfile
from collections.abc import Sequence
from pathlib import Path

from jarvis.core import proc
from jarvis.core.config import WORKTREES_DIR

TIMEOUT = 600  # seconds for one run of the suite
SHOWN = 5  # failing tests named in a refusal
# Tests that fail for want of what a worktree or a task's sandbox doesn't have, whatever the branch does
ENV_ONLY = {
    "tests/test_panel.py::test_preferences_kept_from_the_panel_and_read_by_config",  # Kokoro's voices, in models/
    "tests/test_push.py::test_service_worker_is_served_from_the_root",  # the web app's build, web/dist
    "tests/test_task_client.py::test_the_runner_stopping_mid_stream_is_it_being_down_not_an_error",  # a socket in /tmp
}
FAILED = re.compile(r"^(?:FAILED|ERROR) (.+?)(?: - .*)?$", re.M)  # pytest's short summary, one line per failure


async def failures(folder: Path, venv: Path, tests: Sequence[str] = ()) -> set[str]:
    """The tests that fail in `folder` (all, or those in the files `tests`), by pytest node id, run with the .venv
    `venv`. Raises RuntimeError if the tests couldn't run, TimeoutError if they ran past TIMEOUT."""
    env = {k: v for k, v in os.environ.items() if k != "VIRTUAL_ENV"} | {"UV_PROJECT_ENVIRONMENT": str(venv)}
    code, out = await proc.run_try("uv", "run", "--no-sync", "--with", "pytest", "pytest", "-q", "-rfE",
                                   "-p", "no:cacheprovider", *tests, timeout=TIMEOUT, cwd=folder, env=env)
    failed = set(FAILED.findall(out))
    if code not in (0, 1, 5) and not failed:  # 5: none collected, as for test files that are all skipped
        raise RuntimeError(" ".join(out.splitlines()[-3:])[:300] or f"pytest exited with {code}")
    return failed


async def new_failures(repo: Path, branch: str, worktree: str | None) -> list[str]:
    """The tests that fail on `branch` but not on what's checked out in `repo`, JARVIS's main checkout."""
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
    """Why `branch` mustn't be merged into `into` because of its tests, said; "" if they pass."""
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
