"""Metal (the GPU) inside Claude Code's macOS sandbox, for MLX Whisper and other GPU work in sandboxed commands.

The CLI's Seatbelt profile opens only the IOSurface IOKit clients, and its settings can add Mach services but no IOKit
rules, so Metal finds no device. `sandbox-bin/env`, first on the CLI's PATH, appends exactly what Metal needs to each
sandboxed command's profile: the GPU's user client (AGXDeviceUserClient) and the shader compiler
(com.apple.MTLCompilerService). Files, network and every other limit stay the CLI's own, so Metal's shader caches stay
unwritable and compiling Metal source at run time (mx.compile) still fails. If the CLI stops starting commands through
`env`, the shim is skipped and Metal is just unavailable again (tests/test_gpu.py catches that).
"""

import os
from pathlib import Path

from jarvis.core import platform

SHIM_DIR = Path(__file__).with_name("sandbox-bin")  # holds only `env`


def env(sandbox: dict | None) -> dict[str, str]:
    """PATH with the shim first, for a CLI whose Bash runs in `sandbox` on macOS; else nothing."""
    if not (platform.MAC and sandbox and sandbox.get("enabled")):
        return {}
    return {"PATH": f"{SHIM_DIR}{os.pathsep}{os.environ.get('PATH', '')}"}
