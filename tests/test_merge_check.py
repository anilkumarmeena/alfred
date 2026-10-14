"""Tests for jarvis.tasks.merge_check: branch test failures block a self-merge unless pre-existing or worktree-only."""

import asyncio
import shutil
import sys
from pathlib import Path

import pytest
from test_in_place import add_new_file, branches, env, git, jarvis, run_worktree_task  # noqa: F401  (fixtures)

from jarvis.core import proc
from jarvis.tasks import merge_check, projects

NEW = "tests/test_new.py::test_it"
OLD = "tests/test_old.py::test_flaky"
ENV_ONLY = sorted(merge_check.ENV_ONLY)[0]


@pytest.fixture
def suite(monkeypatch, env):
    """Fake test suite: failures per folder ("branch" = worktree, "main" = main checkout).

    Records each run as (folder, files asked for).
    """
    fake = {"branch": set(), "main": set(), "runs": []}
    monkeypatch.setattr(merge_check, "WORKTREES_DIR", env["tmp"] / "worktrees")

    async def failures(folder: Path, venv: Path, tests=()) -> set[str]:
        fake["runs"].append((folder, list(tests)))
        on_main = folder == projects.JARVIS_REPO
        return {t for t in fake["main" if on_main else "branch"] if not tests or t.partition("::")[0] in tests}

    monkeypatch.setattr(merge_check, "failures", failures)
    return fake


def test_a_test_newly_failing_on_the_branch_refuses_the_merge_naming_it(jarvis, suite, monkeypatch):
    r, task = run_worktree_task(monkeypatch, add_new_file, project="jarvis")
    head = git(jarvis, "rev-parse", "HEAD")
    suite["branch"] = {NEW}
    with pytest.raises(ValueError) as refused:
        asyncio.run(r.merge(task["id"]))
    assert str(refused.value) == (f"Task {task['id']}'s branch {task['branch']} wasn't merged, since 1 test fails on "
                                  f"it that pass on main: {NEW}.")
    assert git(jarvis, "rev-parse", "HEAD") == head and task["branch"] in branches(jarvis)
    assert Path(task["worktree"]).exists() and suite["runs"] == [(Path(task["worktree"]), [])]


def test_failures_that_main_shares_or_the_environment_explains_dont_block(jarvis, suite, monkeypatch):
    (jarvis / "tests").mkdir()
    (jarvis / "tests" / "test_old.py").write_text("")  # also in the main checkout, so rerun there
    r, task = run_worktree_task(monkeypatch, add_new_file, project="jarvis")
    suite["branch"], suite["main"] = {OLD, ENV_ONLY}, {OLD}
    merged = asyncio.run(r.merge(task["id"]))
    assert "merged into main as one commit, its tests passing" in merged["spoken"] and (jarvis / "new.py").exists()
    assert suite["runs"][1] == (jarvis, ["tests/test_old.py"])  # only the failing file, not the env-only one


def test_a_refusal_names_a_few_and_counts_the_rest(jarvis, suite, monkeypatch):
    r, task = run_worktree_task(monkeypatch, add_new_file, project="jarvis")
    suite["branch"] = {f"tests/test_new.py::test_{i}" for i in range(8)}
    with pytest.raises(ValueError, match=r"since 8 tests fail on it that pass on main: tests/test_new.py::test_0, "
                                         r".*test_4, and 3 more\.$"):
        asyncio.run(r.merge(task["id"]))


@pytest.mark.parametrize("trouble, said", [
    (RuntimeError("ModuleNotFoundError: No module named 'jarvis.gone'"), "its tests couldn't run: ModuleNotFound"),
    (TimeoutError(), f"its tests didn't finish within {merge_check.TIMEOUT // 60} minutes"),
])
def test_tests_that_cant_run_refuse_the_merge_too(jarvis, suite, monkeypatch, trouble, said):
    async def broken(folder, venv, tests=()):
        raise trouble

    monkeypatch.setattr(merge_check, "failures", broken)
    r, task = run_worktree_task(monkeypatch, add_new_file, project="jarvis")
    with pytest.raises(ValueError, match=said):
        asyncio.run(r.merge(task["id"]))
    assert task["branch"] in branches(jarvis)


def test_a_branch_whose_worktree_is_gone_is_tested_in_a_checkout_made_for_it(jarvis, suite, monkeypatch, env):
    r, task = run_worktree_task(monkeypatch, add_new_file, project="jarvis")
    git(jarvis, "worktree", "remove", "--force", task["worktree"])
    seen = []

    async def failures(folder, venv, tests=()):
        seen.append(((folder / "new.py").exists(), venv))
        return set()

    monkeypatch.setattr(merge_check, "failures", failures)
    merged = asyncio.run(r.merge(task["id"]))
    assert seen == [(True, jarvis / ".venv")] and "its tests passing" in merged["spoken"]
    assert "merge-check" not in git(jarvis, "worktree", "list")
    assert list((env["tmp"] / "worktrees").glob("merge-check-*")) == []


def test_other_projects_branches_merge_without_running_tests(env, suite, monkeypatch):
    r, task = run_worktree_task(monkeypatch, add_new_file)
    merged = asyncio.run(r.merge(task["id"]))
    assert suite["runs"] == [] and "its tests" not in merged["spoken"]


# ── Running the suite ───────────────────────────────────────────

def test_the_suite_runs_with_the_venv_given_and_its_failures_are_read(monkeypatch, tmp_path):
    ran = {}

    async def run_try(*argv, timeout, cwd, env):
        ran.update(argv=argv, cwd=cwd, env=env)
        return 1, ("....F.E\n=== short test summary info ===\n"
                   "FAILED tests/test_a.py::test_one - AssertionError: assert 1 == 2\n"
                   "FAILED tests/test_a.py::test_two[a b]\n"
                   "ERROR tests/test_b.py - ImportError: cannot import name 'x'\n"
                   "2 failed, 4 passed, 1 error in 1.0s")

    monkeypatch.setattr(proc, "run_try", run_try)
    monkeypatch.setenv("VIRTUAL_ENV", "/somewhere/else")
    failed = asyncio.run(merge_check.failures(tmp_path, tmp_path / "venv", ["tests/test_a.py"]))
    assert failed == {"tests/test_a.py::test_one", "tests/test_a.py::test_two[a b]", "tests/test_b.py"}
    assert ran["argv"][:2] == ("uv", "run") and ran["argv"][-1] == "tests/test_a.py" and ran["cwd"] == tmp_path
    assert ran["env"]["UV_PROJECT_ENVIRONMENT"] == str(tmp_path / "venv") and "VIRTUAL_ENV" not in ran["env"]


def test_a_suite_that_wont_start_says_why(monkeypatch, tmp_path):
    async def run_try(*argv, timeout, cwd, env):
        return 4, "ERROR: usage: pytest [options]\nerror: unrecognized arguments: --bogus"

    monkeypatch.setattr(proc, "run_try", run_try)
    with pytest.raises(RuntimeError, match="unrecognized arguments"):
        asyncio.run(merge_check.failures(tmp_path, tmp_path / "venv"))


@pytest.mark.skipif(not shutil.which("uv"), reason="needs uv")
def test_a_real_run_finds_the_failing_test(tmp_path):
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "tiny"\nversion = "0"\nrequires-python = ">=3.12"\n')
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_tiny.py").write_text("def test_passes():\n    pass\n\n\n"
                                                     "def test_fails():\n    assert False\n")
    failed = asyncio.run(merge_check.failures(tmp_path, Path(sys.prefix)))
    assert failed == {"tests/test_tiny.py::test_fails"}
