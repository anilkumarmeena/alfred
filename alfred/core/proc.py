"""Subprocess helpers. Arguments are passed as argv, never through a shell."""

import asyncio
import contextlib
import os
import signal
from pathlib import Path

PIPE, STDOUT = asyncio.subprocess.PIPE, asyncio.subprocess.STDOUT


async def _spawn(argv: tuple[str, ...], timeout: float, input: bytes | None, stderr: int,
                 cwd: str | Path | None = None, env: dict[str, str] | None = None,
                 group: bool = False) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *argv, stdin=PIPE if input is not None else None, stdout=PIPE, stderr=stderr, cwd=cwd, env=env,
        start_new_session=group
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(input), timeout)
    except (TimeoutError, asyncio.CancelledError):  # don't leave it running after a timeout or a stop
        with contextlib.suppress(ProcessLookupError, PermissionError):  # gone already (macOS: a zombie group)
            if group:
                os.killpg(proc.pid, signal.SIGKILL)
            else:
                proc.kill()
        await proc.wait()
        raise
    return proc.returncode or 0, out.decode().strip(), (err or b"").decode().strip()


async def run(*argv: str, timeout: float = 30, input: bytes | None = None) -> str:
    """Stdout; raises RuntimeError with stderr on non-zero exit."""
    code, out, err = await _spawn(argv, timeout, input, PIPE)
    if code:
        raise RuntimeError(err or f"{argv[0]} exited with {code}")
    return out


async def run_try(*argv: str, timeout: float = 30, cwd: str | Path | None = None,
                  env: dict[str, str] | None = None, group: bool = False) -> tuple[int, str]:
    """(exit code, combined stdout+stderr); a non-zero exit does not raise. With `group`, it runs in its own process
    group, all of which a timeout or a stop kills (a launcher such as uv would otherwise leave its child running)."""
    code, out, _ = await _spawn(argv, timeout, None, STDOUT, cwd, env, group)
    return code, out
