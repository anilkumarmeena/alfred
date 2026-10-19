"""File helpers for state kept in data/ and secrets/.

write_atomic stops a reader seeing a half-written file; update (and locked, for callers that read and write in
steps) also stops two processes that each read, change and rewrite the same file from losing one another's change.
"""

import fcntl
import os
import tempfile
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path


def write_atomic(file: Path, data: str | bytes, mode: int | None = None) -> None:
    """Atomically replace `file` via a temp file and rename. Mode: `mode`, else the old file's, else 0600."""
    fd, tmp = tempfile.mkstemp(dir=file.parent, prefix=f".{file.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb" if isinstance(data, bytes) else "w") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        if mode is not None:
            os.chmod(tmp, mode)
        elif file.exists():
            os.chmod(tmp, file.stat().st_mode & 0o777)
        os.replace(tmp, file)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


@contextmanager
def locked(file: Path):
    """Hold `file`'s cross-process lock: an exclusive flock on a hidden sidecar, since `file` itself is replaced by
    renames. Every process that changes `file` must take it; reads need not, as writes are atomic."""
    with open(file.with_name(f".{file.name}.lock"), "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield  # released when the lock file closes


def update(file: Path, change: Callable[[str], str], mode: int | None = None) -> str:
    """Rewrite `file` as `change(its text)` ("" if missing) under its lock, atomically; returns the new text.

    Unchanged text isn't rewritten; if `change` raises, nothing is."""
    with locked(file):
        old = file.read_text() if file.exists() else ""
        if (new := change(old)) != old:
            write_atomic(file, new, mode)
        return new
