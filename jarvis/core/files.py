"""File helpers for state kept in data/ and secrets/."""

import os
import tempfile
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
