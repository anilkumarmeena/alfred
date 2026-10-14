"""The Claude logins JARVIS can run on, in priority order, and which one it runs on now.

The default login is the user's own Claude Code sign-in (or JARVIS_CLAUDE_CONFIG_DIR's, when .env names one). Named
logins are JARVIS's own: each a CLI config folder in secrets/claude-logins/<name>, so each has its own keychain entry,
account, usage limits and session transcripts, and signing one in or out never touches another. Install-wide, like
the Claude login always was (see core.profile). Signing in: jarvis.claude_login.

JARVIS runs on the first login in the priority order (secrets/claude-logins/order) that's usable: not one whose
limit is used up (brain.usage, per login) or whose sign-in stopped working (its last check, or a call that failed to
authenticate). Chosen at every CLI start, so a change in order or a used-up limit applies from the next call.

  order() / set_order(names)   every login, by priority ("" = the default)
  active() / skipped(name)     the login in use / why one is passed over ("" if it isn't)
  env(name) / of_env(env)      the CLI's environment for a login / the login an environment is for
  folder(name) / folders()     a login's config folder / every login's
  resume_on(session_id)        the login to resume a session on: its own, or, while that one is passed over, the one
                               in use, with the transcript copied over
  named() / make() / remove()  JARVIS's own logins
  note(name, ...) / checks()   the last check of each login (could the services use it?)
"""

import json
import re
import shutil
import time
from pathlib import Path

from jarvis.core import config
from jarvis.core.files import write_atomic

DEFAULT = ""
NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,31}")
RESERVED = {"default", "order"}
SESSION = re.compile(r"[A-Za-z0-9-]{1,64}")


def _root() -> Path:
    return config.CLAUDE_LOGINS_DIR


def valid(name: str) -> str:
    """`name` as a new login's name, or ValueError."""
    name = name.strip().lower()
    if not NAME.fullmatch(name) or name in RESERVED:
        raise ValueError("A Claude login's name is up to 32 lowercase letters, digits and dashes (not \"default\").")
    return name


def named() -> list[str]:
    """JARVIS's own logins, by name."""
    root = _root()
    return sorted(p.name for p in root.iterdir() if p.is_dir() and NAME.fullmatch(p.name)) if root.is_dir() else []


def known(name: str) -> str:
    """`name` if it's the default ("") or one of JARVIS's logins, else ValueError."""
    if name != DEFAULT and name not in named():
        raise ValueError(f"There's no Claude login called {name}.")
    return name


def folder(name: str) -> Path:
    """A login's CLI config folder (session transcripts are under its projects/)."""
    return _root() / name if name else Path(config.CLAUDE_CONFIG_DIR)


def folders() -> list[Path]:
    return [folder(name) for name in (DEFAULT, *named())]


def env(name: str) -> dict[str, str]:
    """The CLI's environment for a login. The default sets CLAUDE_CONFIG_DIR only for JARVIS_CLAUDE_CONFIG_DIR's:
    setting it at all makes the CLI look for another keychain entry (see core.config)."""
    if name:
        return {"CLAUDE_CONFIG_DIR": str(folder(name))}
    return {"CLAUDE_CONFIG_DIR": config.CLAUDE_OWN_CONFIG_DIR} if config.CLAUDE_OWN_CONFIG_DIR else {}


def of_env(env: dict[str, str] | None) -> str | None:
    """The login a CLI environment is for, or None if it's none of them."""
    given = (env or {}).get("CLAUDE_CONFIG_DIR", "")
    if not given or given == config.CLAUDE_OWN_CONFIG_DIR:
        return DEFAULT
    found = Path(given)
    return found.name if found.parent == _root() and found.name in named() else None


def order() -> list[str]:
    """Every login, highest priority first: the saved order, then any not in it (the default first, then new ones)."""
    try:
        saved = json.loads((_root() / "order").read_text())
    except (OSError, ValueError):
        saved = []
    every = [DEFAULT, *named()]
    kept = [n for n in dict.fromkeys(saved if isinstance(saved, list) else []) if n in every]
    return kept + [n for n in every if n not in kept]


def set_order(names: list[str]) -> None:
    """Save the priority order: every login, once each."""
    if sorted(names) != sorted(order()):
        raise ValueError("The order has to list every Claude login once.")
    _root().mkdir(mode=0o700, parents=True, exist_ok=True)
    write_atomic(_root() / "order", json.dumps(names))


def skipped(name: str, now: float | None = None) -> str:
    """Why JARVIS passes over a login ("" if it doesn't): its sign-in stopped working, or its limit is used up."""
    from jarvis.brain import usage  # it reads the login in use from here

    found = checks().get(name)
    if found and not found.get("ok"):
        return "its sign-in isn't working"
    if out := usage.out(now, kept=usage.of(name)):
        reset = out[1].get("resets_at")
        return f"its {usage.NAMES.get(out[0], 'usage')} limit is used up" + (f" until {usage.when(reset, now)}" if reset
                                                                               else "")
    return ""


def active(now: float | None = None) -> str:
    """The login JARVIS runs on: the first one by priority that isn't passed over; with every one passed over, the
    first (its limit's reset is what scheduled runs wait for)."""
    every = order()
    return next((name for name in every if not skipped(name, now)), every[0])


def resume_on(session_id: str | None) -> str | None:
    """The login to resume a session on (None: it isn't in any, start afresh on the one in use). Its own login while
    that one isn't passed over; else the one in use, with the session's transcript copied into its folder."""
    held = holding(session_id)
    if held is None or not skipped(held):
        return held
    now_on = active()
    if now_on == held:
        return held
    source, target = folder(held), folder(now_on)
    for found in source.glob(f"projects/*/{session_id}*"):  # the transcript, and its subagents' folder if any
        to = target / found.relative_to(source)
        to.parent.mkdir(parents=True, exist_ok=True)
        if found.is_dir():
            shutil.copytree(found, to, dirs_exist_ok=True)
        else:
            shutil.copy2(found, to)
    return now_on


def make(name: str) -> Path:
    """A named login's folder, made private if new."""
    _root().mkdir(mode=0o700, parents=True, exist_ok=True)
    made = _root() / valid(name)
    made.mkdir(mode=0o700, exist_ok=True)
    return made


def remove(name: str) -> None:
    """Forget one of JARVIS's logins' folder (sign it out first); the order closes up around it."""
    if not known(name):
        raise ValueError("The default login is the computer's own Claude Code sign-in: sign it out there.")
    shutil.rmtree(folder(name), ignore_errors=True)
    found = checks()
    if found.pop(name, None):
        write_atomic(_root() / "checks.json", json.dumps(found))


def holding(session_id: str | None) -> str | None:
    """The login whose folder holds session `session_id`'s transcript, or None."""
    if not session_id or not SESSION.fullmatch(session_id):
        return None
    for name in (DEFAULT, *named()):
        if next(folder(name).glob(f"projects/*/{session_id}.jsonl"), None):
            return name
    return None


def account(name: str) -> dict:
    """The account the CLI last signed `name` in as, from its config file ({} if none): emailAddress,
    organizationName, ... Not proof the login still works: check that (jarvis.claude_login.check)."""
    file = folder(name) / ".claude.json" if name or config.CLAUDE_OWN_CONFIG_DIR else Path.home() / ".claude.json"
    try:
        found = json.loads(file.read_text()).get("oauthAccount")
    except (OSError, ValueError, AttributeError):
        return {}
    return found if isinstance(found, dict) else {}


def checks() -> dict[str, dict]:
    """Each login's last check: {name: {"ok", "said", "at"}}."""
    try:
        found = json.loads((_root() / "checks.json").read_text())
    except (OSError, ValueError):
        return {}
    return found if isinstance(found, dict) else {}


def note(name: str, ok: bool, said: str) -> None:
    """Keep a login's check result for the panel."""
    _root().mkdir(mode=0o700, parents=True, exist_ok=True)
    write_atomic(_root() / "checks.json", json.dumps(checks() | {name: {"ok": ok, "said": said, "at": time.time()}}))
