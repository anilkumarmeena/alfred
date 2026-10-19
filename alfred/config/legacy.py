"""Names from before Alfred was called Alfred (it was JARVIS), so an install made then keeps working until
alfred.setup_local moves it over: its JARVIS_* variables, its ~/.jarvis home and its com.jarvis.* services.

The variables are renamed as config loads (adopt_env, upgrade_env_file); the home and the services move when any setup
or service command runs (alfred.setup_local.migrate_legacy). The jarvis package in the checkout keeps the old
services' entry points starting meanwhile. Standard library only: config imports it before anything else.
"""

import os
import re
import tempfile
from collections.abc import MutableMapping
from pathlib import Path

OLD_PREFIX, NEW_PREFIX = "JARVIS_", "ALFRED_"  # .env and environment variables
OLD_HOME = ".jarvis"  # the install's home in the user's home folder, now .alfred
OLD_LABELS = "com.jarvis"  # service label prefix, now com.alfred
_ASSIGNMENT = re.compile(rf"^(\s*(?:export\s+)?){OLD_PREFIX}(\w+)(\s*=)", re.M)


def adopt_env(environ: MutableMapping[str, str] = os.environ) -> list[str]:
    """Rename the old variables in `environ` to the new names, unless a new one is set already (the old one goes either
    way, so a secret never lingers under its old name for child processes). Returns the new names adopted."""
    adopted = []
    for old in [k for k in environ if k.startswith(OLD_PREFIX)]:
        value, new = environ.pop(old), NEW_PREFIX + old.removeprefix(OLD_PREFIX)
        if not environ.get(new, "").strip():
            environ[new] = value
            adopted.append(new)
    return adopted


def upgrade_env_file(path: Path) -> bool:
    """Rename the old variables in a .env file, in place (atomically, keeping its mode); a variable whose new name is
    there too is left alone. Whether it changed anything; False when the file is missing or can't be written (a task's
    sandbox hides .env), when adopt_env still covers what it holds."""
    try:
        text = path.read_text()
    except OSError:
        return False
    have = set(re.findall(rf"^\s*(?:export\s+)?{NEW_PREFIX}(\w+)\s*=", text, re.M))
    new = _ASSIGNMENT.sub(lambda m: m[0] if m[2] in have else f"{m[1]}{NEW_PREFIX}{m[2]}{m[3]}", text)
    if new == text:
        return False
    tmp = ""
    try:
        mode = path.stat().st_mode & 0o777
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
        with os.fdopen(fd, "w") as f:
            f.write(new)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except OSError:
        if tmp:
            Path(tmp).unlink(missing_ok=True)
        return False
    return True


def old_home(user_home: Path | None = None) -> Path:
    return (user_home or Path.home()) / OLD_HOME
