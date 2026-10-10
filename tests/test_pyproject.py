"""Tests for pyproject dependency sources: a task worktree must build the environment like the main checkout."""

import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_sources_resolve_from_any_checkout_and_are_pinned():
    sources = tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["uv"]["sources"]
    for name, source in sources.items():
        assert "path" not in source, f"{name}: a path source doesn't resolve from a task's worktree"
        if "git" in source:
            assert source.get("rev") or source.get("tag"), f"{name}: pin the git source to a commit or tag"
