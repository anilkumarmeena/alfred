"""Where JARVIS's settings and data live, and reading the settings files.

JARVIS_HOME holds everything an install keeps: config.toml, .env, data/, secrets/, models/ and logs/. It's the
JARVIS_HOME variable if set, else ~/.jarvis if that folder exists, else the code checkout (the layout from before
JARVIS_HOME). The checkout's config/ holds the shipped defaults; JARVIS_HOME's config.toml (or the file JARVIS_CONFIG
names) overrides them table by table, key by key.
"""

import os
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]  # the code checkout
DEFAULTS_DIR = ROOT / "config"
SHIPPED = (("", "jarvis.toml"), ("models", "models.toml"))  # (table it fills, file in DEFAULTS_DIR)


class ConfigError(Exception):
    """A settings file is missing, unreadable or lacks a value the code needs."""


def home(env: Mapping[str, str] = os.environ, user_home: Path | None = None) -> Path:
    if given := env.get("JARVIS_HOME", "").strip():
        return Path(given).expanduser()
    own = (user_home or Path.home()) / ".jarvis"
    return own if (own / ".env").exists() or (own / "data").exists() else ROOT


def _read(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as f:
            return tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path} isn't valid TOML: {e}") from None


def merged(base: dict[str, Any], over: Mapping[str, Any]) -> dict[str, Any]:
    """`base` with `over` laid on top: tables merge key by key, anything else is replaced."""
    out = dict(base)
    for key, value in over.items():
        out[key] = merged(out[key], value) if isinstance(value, Mapping) and isinstance(out.get(key), dict) else value
    return out


def load(home_dir: Path, env: Mapping[str, str] = os.environ, defaults_dir: Path = DEFAULTS_DIR) -> dict[str, Any]:
    """The shipped defaults with the user's file laid on top."""
    settings: dict[str, Any] = {}
    for table, name in SHIPPED:
        if not (path := defaults_dir / name).is_file():
            raise ConfigError(f"JARVIS's default settings are missing: {path}")
        settings = merged(settings, {table: _read(path)} if table else _read(path))
    user = Path(env["JARVIS_CONFIG"]).expanduser() if env.get("JARVIS_CONFIG", "").strip() else home_dir / "config.toml"
    if user.is_file():
        settings = merged(settings, _read(user))
    elif env.get("JARVIS_CONFIG", "").strip():
        raise ConfigError(f"JARVIS_CONFIG names a file that isn't there: {user}")
    return settings


def value(settings: Mapping[str, Any], key: str) -> Any:
    """The value at a dotted key ("tasks.max_turns")."""
    node: Any = settings
    for part in key.split("."):
        if not isinstance(node, Mapping) or part not in node:
            raise ConfigError(f"No setting {key} in config/ (or your config.toml)")
        node = node[part]
    return node
