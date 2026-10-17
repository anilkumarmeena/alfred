"""The Claude logins JARVIS runs on, in priority order, and which one it runs on now.

One login is this Mac's own Claude Code sign-in (MAC, named ""; JARVIS_CLAUDE_CONFIG_DIR's when .env names one), the
one `claude` in a terminal uses. Every other is a CLI config folder in secrets/claude-logins/<name>, so each has its
own keychain entry, account, usage limits and session transcripts, and signing one in or out never touches another.
All are on the list alike for the user to order; the Mac's always stays on it (the terminal uses it, so it's never
signed out here), so JARVIS always has one, and any other can be removed.
Install-wide, like the Claude login always was (see jarvis.storage.profile). Signing in: jarvis.claude_login.

JARVIS runs on the first login in the priority order (secrets/claude-logins/order) that's usable: not one whose
limit is used up (brain.usage, per login) or whose sign-in stopped working (its last check, or a call that failed to
authenticate). Chosen at every CLI start, so a change in order or a used-up limit applies from the next call.

  order() / set_order(names)   the logins JARVIS may use, by priority
  removable(name) / remove(name)   any login but the Mac's
  active() / skipped(name)     the login in use / why one is passed over ("" if it isn't)
  env(name) / of_env(env)      the CLI's environment for a login / the login an environment is for
  folder(name) / folders()     a login's config folder / every login's
  resume_on(session_id)        the login to resume a session on: its own, or, while that one is passed over, the one
                               in use, with the transcript copied over
  elsewhere(session_id)        a usable login to move a session to now, while its own is passed over, or None
  named() / make() / remove()  JARVIS's logins; removable(name): it isn't the last
  note(name, ...) / checks()   the last check of each login (could the services use it?)
"""

import json
import re
import shutil
import time
from pathlib import Path
from typing import TYPE_CHECKING

from jarvis import config
from jarvis.core.files import locked, write_atomic

if TYPE_CHECKING:
    from jarvis.storage.profile import Profile

MAC = ""  # this Mac's own Claude Code sign-in
NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,31}")
RESERVED = {"default", "order"}
SESSION = re.compile(r"[A-Za-z0-9-]{1,64}")


def _root() -> Path:
    return config.CLAUDE_LOGINS_DIR


def valid(name: str) -> str:
    """`name` as a new login's name, or ValueError."""
    name = name.strip().lower()
    if not NAME.fullmatch(name) or name in RESERVED:
        raise ValueError("A Claude login's name is up to 32 lowercase letters, digits and dashes.")
    return name


def named() -> list[str]:
    """JARVIS's own logins, by name."""
    root = _root()
    return sorted(p.name for p in root.iterdir() if p.is_dir() and NAME.fullmatch(p.name)) if root.is_dir() else []


def known(name: str) -> str:
    """`name` if it's one of JARVIS's logins or the fallback, else ValueError."""
    if name != MAC and name not in named():
        raise ValueError(f"There's no Claude login called {name}.")
    return name


def folder(name: str) -> Path:
    """A login's CLI config folder (session transcripts are under its projects/)."""
    return _root() / name if name else Path(config.CLAUDE_CONFIG_DIR)


def folders() -> list[Path]:
    return [folder(name) for name in (MAC, *named())]


def env(name: str) -> dict[str, str]:
    """The CLI's environment for a login. The fallback sets CLAUDE_CONFIG_DIR only for JARVIS_CLAUDE_CONFIG_DIR's:
    setting it at all makes the CLI look for another keychain entry (see jarvis.config)."""
    if name:
        return {"CLAUDE_CONFIG_DIR": str(folder(name))}
    return {"CLAUDE_CONFIG_DIR": config.CLAUDE_OWN_CONFIG_DIR} if config.CLAUDE_OWN_CONFIG_DIR else {}


def of_env(env: dict[str, str] | None) -> str | None:
    """The login a CLI environment is for, or None if it's none of them."""
    given = (env or {}).get("CLAUDE_CONFIG_DIR", "")
    if not given or given == config.CLAUDE_OWN_CONFIG_DIR:
        return MAC
    found = Path(given)
    return found.name if found.parent == _root() and found.name in named() else None


def _saved(file: str) -> list[str]:
    try:
        found = json.loads((_root() / file).read_text())
    except (OSError, ValueError):
        return []
    return [n for n in found if isinstance(n, str)] if isinstance(found, list) else []


def _save(file: str, names: list[str]) -> None:
    _root().mkdir(mode=0o700, parents=True, exist_ok=True)
    write_atomic(_root() / file, json.dumps(names))


def order() -> list[str]:
    """The logins JARVIS may use, highest priority first: the saved order, then any not in it (the Mac's first,
    then new ones)."""
    every = [MAC, *named()]
    kept = [n for n in dict.fromkeys(_saved("order")) if n in every]
    return kept + [n for n in every if n not in kept]


def set_order(names: list[str]) -> None:
    """Save the priority order: every login JARVIS may use, once each."""
    if sorted(names) != sorted(order()):
        raise ValueError("The order has to list every Claude login once.")
    _save("order", names)


def skipped(name: str, now: float | None = None) -> str:
    """Why JARVIS passes over a login ("" if it doesn't): its sign-in stopped working, or its limit is used up."""
    from jarvis.llm import usage  # it reads the login in use from here

    if name not in order():
        return "it isn't on JARVIS's list"  # removed while a task on it waited
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
            shutil.copy(found, to)  # a fresh modified time: this copy is now the latest (holding)
    return now_on


def elsewhere(session_id: str | None) -> str | None:
    """The login a session can carry on on now while its own is passed over (its limit used up, say): the one in use,
    if that's another and usable. None when it should wait: its own still works, or every login is passed over."""
    held = holding(session_id)
    if held is None or not skipped(held):
        return None
    now_on = active()
    return now_on if now_on != held and not skipped(now_on) else None


def make(name: str) -> Path:
    """A named login's folder, made private if new."""
    _root().mkdir(mode=0o700, parents=True, exist_ok=True)
    made = _root() / valid(name)
    made.mkdir(mode=0o700, exist_ok=True)
    return made


def removable(name: str) -> str:
    """`name` if it can be removed (any login but the Mac's), else ValueError."""
    if name == MAC:
        raise ValueError("This Mac's own Claude Code login stays on the list: move it down instead.")
    if name not in named():
        raise ValueError(f"There's no Claude login called {name}.")
    return name


def remove(name: str) -> None:
    """Forget a login's folder (sign it out first); the order closes up around it. Never the Mac's."""
    removable(name)
    shutil.rmtree(folder(name), ignore_errors=True)
    with locked(file := _root() / "checks.json"):  # services and the panel both note checks
        found = checks()
        if found.pop(name, None):
            write_atomic(file, json.dumps(found))


def holding(session_id: str | None) -> str | None:
    """The login whose folder holds session `session_id`'s latest transcript, or None: once resume_on has moved a
    session, the login it moved from keeps an older copy."""
    if not session_id or not SESSION.fullmatch(session_id):
        return None
    held = [(found.stat().st_mtime, name) for name in (MAC, *named())
            for found in folder(name).glob(f"projects/*/{session_id}.jsonl")]
    return max(held, key=lambda h: h[0])[1] if held else None


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
    with locked(file := _root() / "checks.json"):  # each process notes its own logins' checks
        write_atomic(file, json.dumps(checks() | {name: {"ok": ok, "said": said, "at": time.time()}}))


def voice_logs(who: "Profile | None" = None) -> Path:
    """The voice brain's Claude session transcripts for a profile (the current one), in the login in use: the CLI
    names the folder after the session's working folder, the profile's `shell`."""
    from jarvis.storage import profile

    shell = (who or profile.current()).shell
    return folder(active()) / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(shell))
