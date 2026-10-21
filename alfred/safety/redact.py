"""Keys and tokens taken out of command and task output before a model, a log or an announcement gets it.

Two ways: by shape (sk-..., ghp_..., AKIA..., a private key block, a value given to a key-like NAME= or "token":), and
by value: Alfred's own keys from its environment, and each entry of the .env files in the folder a command ran in, its
git checkout and its registered project (commands.env_files). Both sets are what a script that reads its project's keys
could print.
"""

import functools
import os
import re
from pathlib import Path

from dotenv import dotenv_values

REDACTED = "[redacted]"
MIN_VALUE = 8  # shorter .env values (true, 3000, en) are too common to hide

_KEY_SHAPES = (
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)",
    r"\bsk-(?:ant-|proj-)?[A-Za-z0-9_-]{20,}",  # Anthropic, OpenAI
    r"\b[sr]k_(?:live|test)_[A-Za-z0-9]{16,}",  # Stripe
    r"\bsk_[a-f0-9]{40,}",  # ElevenLabs
    r"\bgh[pousr]_[A-Za-z0-9]{30,}", r"\bgithub_pat_[A-Za-z0-9_]{30,}", r"\bglpat-[A-Za-z0-9_-]{20,}",
    r"\bxox[abposr]-[A-Za-z0-9-]{10,}",  # Slack
    r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b",  # AWS
    r"\bAIza[0-9A-Za-z_-]{35}", r"\bya29\.[0-9A-Za-z_-]{20,}",  # Google
    r"\bhf_[A-Za-z0-9]{30,}", r"\bnpm_[A-Za-z0-9]{36}\b",
    r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}",  # a JWT
)
_SHAPES = re.compile("|".join(_KEY_SHAPES))
# The value given to a key-like name: API_KEY=..., "client_secret": "...", Authorization: Bearer ...
_NAMED = re.compile(r"""(?P<name>(?:\b[A-Z0-9_]*(?:TOKEN|SECRET|PASSWORD|PASSCODE|API_?KEY|PRIVATE_KEY|ACCESS_KEY|"""
                    r"""CREDENTIAL)[A-Z0-9_]*|(?i:\b(?:[\w-]*(?:token|secret|password|api_?key|apikey)|authorization)))"""
                    r"""["']?[ \t]*[:=][ \t]*["']?(?:(?i:bearer|basic|token)[ \t]+)?)(?P<value>[^\s"',;]{"""
                    + str(MIN_VALUE) + ",})")


def redact(text: str, folder: str | Path | None = None) -> str:
    """`text` with keys and tokens replaced by REDACTED: by shape, and by value (Alfred's own; `folder`'s .env files and
    those of its git checkout)."""
    if not text:
        return text
    for value in sorted(_values(folder), key=len, reverse=True):  # a longer value first, before a part of it
        text = text.replace(value, REDACTED)
    text = _SHAPES.sub(REDACTED, text)
    return _NAMED.sub(lambda m: m["name"] + REDACTED, text)


def _values(folder: str | Path | None) -> set[str]:
    from alfred.safety.host import SENSITIVE

    names = _own_names(_stamp(_own_env()))
    values = {v for k, v in os.environ.items() if (k in names or SENSITIVE.search(k)) and k != "SSH_AUTH_SOCK"
              and _keylike(v)}
    if folder:
        from alfred.safety.commands import env_files

        for env in env_files(Path(folder).expanduser()):
            values |= _file_values(env, _stamp(env))
    return values


def _own_env() -> Path:
    from alfred.config import HOME_DIR

    return HOME_DIR / ".env"


def _stamp(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


@functools.lru_cache(maxsize=4)
def _own_names(_stamp: float) -> frozenset[str]:
    """The names of Alfred's own keys in its environment: its .env's and the vault's."""
    from alfred.safety.host import env_keys

    return frozenset(env_keys())


@functools.lru_cache(maxsize=64)
def _file_values(path: Path, _stamp: float) -> frozenset[str]:
    try:
        return frozenset(v for v in dotenv_values(path).values() if v and _keylike(v))
    except (OSError, ValueError, UnicodeDecodeError):
        return frozenset()


def _keylike(value: str) -> bool:
    """Long enough and not a number, a file path or a common setting, which are too common to hide."""
    return (len(value) >= MIN_VALUE and not value.isdigit() and not value.startswith(("/", "~"))
            and value.lower() not in ("production", "development"))
