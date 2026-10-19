"""A fresh worktree pre-warmed with the main checkout's dependencies, cloned copy-on-write (git_ops.worktree_op).

  node_modules  at the top and one folder down (web/node_modules), where the worktree has that folder
  .venv         a clone, never the main one shared: host commands could write the main one, and the sandbox can't, so
                a task's uv sync would fail there. Its scripts and .pth files are pointed at the clone and the worktree.
Only a clone is made (clonefile on APFS, a reflink on Linux), never a full copy: where the filesystem can't clone,
nothing is copied and the task installs as before. Only folders git ignores in the worktree are copied, so none of it
is ever committed.
"""

import ctypes
import logging
import os
import re
import shutil
import subprocess
from pathlib import Path

from alfred.core import platform

log = logging.getLogger("alfred.tasks.runner")
NODE, VENV = "node_modules", ".venv"
CLONE_NOFOLLOW = 0x0001  # clonefile(2): a symlink is cloned as itself, not followed
TIMEOUT = 300  # seconds for one folder's clone with cp (Linux)
SCRIPT_MOST = 1 << 20  # bytes: a bigger file in .venv/bin isn't a script to point at the clone


def warm(repo: Path, worktree: Path) -> list[str]:
    """Clone the main checkout's dependency folders into a new worktree; the ones cloned, as relative paths."""
    found = wanted(repo, worktree)
    ignored = _ignored(worktree, found)
    done = []
    for rel in found:
        if rel not in ignored or not clone(repo / rel, worktree / rel):
            continue
        if rel == VENV:
            try:
                moved(worktree / VENV, repo, worktree)
            except OSError as e:  # a script left pointing at the main .venv would install there: no clone at all
                log.warning("couldn't point %s's .venv at itself, so it isn't kept: %s", worktree, e)
                shutil.rmtree(worktree / VENV, ignore_errors=True)
                continue
        done.append(rel)
    return done


def _real_dir(path: Path) -> bool:
    return path.is_dir() and not path.is_symlink()


def wanted(repo: Path, worktree: Path) -> list[str]:
    """Dependency folders the main checkout has and the worktree lacks, in a folder the worktree has."""
    tops = sorted(d.name for d in repo.iterdir() if not d.name.startswith(".") and d.name != NODE and _real_dir(d))
    found = [VENV, NODE, *(f"{top}/{NODE}" for top in tops)]
    return [rel for rel in found if _real_dir(repo / rel) and _real_dir((worktree / rel).parent)
            and not os.path.lexists(worktree / rel)]


def _ignored(worktree: Path, found: list[str]) -> set[str]:
    """Those of `found` git ignores in the worktree (as folders, which they aren't yet there)."""
    if not found:
        return set()
    try:
        done = subprocess.run(["git", "-C", str(worktree), "check-ignore", "--", *(f"{rel}/" for rel in found)],
                              capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.SubprocessError):
        return set()
    return {line.rstrip("/") for line in done.stdout.splitlines()}


def clone(source: Path, dest: Path) -> bool:
    """Clone folder `source` as `dest`, copy-on-write; False, with nothing left at `dest`, where it can't be."""
    if os.path.lexists(dest):
        return False
    try:
        done = _clonefile(source, dest) if platform.MAC else _reflink(source, dest)
    except (OSError, subprocess.SubprocessError) as e:  # a timeout, too
        log.info("no copy-on-write clone of %s: %s", source, e)
        done = False
    if not done:
        shutil.rmtree(dest, ignore_errors=True)
    return done


def _clonefile(source: Path, dest: Path) -> bool:
    """macOS: one clonefile(2) of the whole folder. It fails, never copies, off APFS or across volumes."""
    libc = ctypes.CDLL(None, use_errno=True)
    libc.clonefile.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint32]
    if libc.clonefile(os.fsencode(source), os.fsencode(dest), CLONE_NOFOLLOW) == 0:
        return True
    errno = ctypes.get_errno()
    raise OSError(errno, os.strerror(errno))


def _reflink(source: Path, dest: Path) -> bool:
    """Linux: cp --reflink=always, once one file shows the filesystem can (else cp would fail file by file)."""
    if (probe := _a_file(source)) is None:
        return False
    test = dest.with_name(f".{dest.name}.reflink")
    try:
        tried = subprocess.run(["cp", "--reflink=always", "--", str(probe), str(test)], capture_output=True, timeout=30,
                               check=False)
    finally:
        test.unlink(missing_ok=True)
    if tried.returncode:
        return False
    return subprocess.run(["cp", "-a", "--reflink=always", "--", str(source), str(dest)], capture_output=True,
                          timeout=TIMEOUT, check=False).returncode == 0


def _a_file(folder: Path) -> Path | None:
    """Some regular file in `folder`, or None."""
    for top, _, names in os.walk(folder):
        for name in names:
            if (path := Path(top, name)).is_file() and not path.is_symlink():
                return path
    return None


def moved(venv: Path, repo: Path, worktree: Path) -> None:
    """Point a .venv cloned from `repo` into `worktree` at itself: the paths into `repo` in its scripts (shebangs,
    activate) and .pth files (editable installs) now lead into `worktree`. Writing a clone's file leaves the
    original's alone."""
    old = re.compile(re.escape(os.fsencode(repo)) + rb"(?![^/\s'\"])")
    new = os.fsencode(worktree)
    for file in [*(venv / "bin").iterdir(), *venv.glob("lib/python*/site-packages/*.pth")]:
        if file.is_symlink() or not file.is_file() or file.stat().st_size > SCRIPT_MOST:
            continue
        data = file.read_bytes()
        if b"\0" not in data and old.search(data):
            file.write_bytes(old.sub(lambda m: new, data))
