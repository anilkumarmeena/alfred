"""Claude usage-limit state, as last reported by the CLI's rate-limit events, shared in data/usage.json (the default
login's; usage-<name>.json for each of Alfred's own, alfred.llm.claude_logins), for the login in use.

Events arrive only on state change (allowed / allowed_warning / rejected) per window, with reset time and sometimes
utilization. Never queries Claude; a window past its reset is treated as fresh.

  windows()    known state by window
  out()        a window used up for every model, or None
  left()       share of the tightest limit for every model still left, 0 to 1
  available()  the model to use (lighter while its family's weekly window is used up)
  used_up_until() when a window used up for every model resets
  hold_until() when scheduled model runs should wait until (used-up window, or five-hour past USAGE_HOLD)
  said()       a one- or two-sentence summary, for usage_left
  of(login)    a login's windows as stored, for said(kept=...)
"""

import json
import logging
import re
import time
from datetime import datetime
from pathlib import Path

from alfred.config import MODELS, USAGE_FILE, USAGE_HOLD
from alfred.core.files import locked, write_atomic
from alfred.llm import claude_logins, roles
from alfred.llm.roles import Role
from alfred.storage import profile

log = logging.getLogger("alfred.llm.usage")

NAMES = {"five_hour": "five-hour", "seven_day": "weekly", "seven_day_opus": "weekly Opus",
         "seven_day_sonnet": "weekly Sonnet", "overage": "extra-usage", "unknown": "usage"}
# Model families (config/models.toml): windows limiting one family only, and its fallback while that's used up
OWN = {f.window: f.name for f in MODELS.families if f.window}
LIGHTER = {f.name: f.lighter for f in MODELS.families if f.lighter}
UNKNOWN_RESET = 30 * 60  # seconds a used-up window with no reset time stays used up
FORGET = 6 * 3600  # seconds before a window with no reset time is forgotten
CLOSE = 0.2  # share of a limit taken to be left when Claude warns it's getting close without saying how close
# Fallback when no rate-limit event arrived: CLI limit wording in an error
LIMIT_TEXT = re.compile(r"usage limit|rate.?limit|hit your limit|limit reached", re.IGNORECASE)


def _file(login: str | None = None) -> Path:
    """A login's usage file (the one in use without `login`): each account has limits of its own."""
    name = claude_logins.active() if login is None else login
    return USAGE_FILE.with_name(f"usage-{name}.json") if name else USAGE_FILE


def _read(login: str | None = None) -> dict[str, dict]:
    file = _file(login)
    try:
        data = json.loads(file.read_text())
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        log.warning("%s is unreadable; starting afresh", file.name)
        return {}


def of(login: str) -> dict[str, dict]:
    return _read(login)


def record(info, now: float | None = None, login: str | None = None) -> None:
    """Store a RateLimitEvent's rate_limit_info under its window, for the login the call ran on (the one in use if
    not known). Never raises."""
    now = now or time.time()
    try:
        login = claude_logins.active(now) if login is None else login
        with locked(file := _file(login)):  # the voice agent and tasks both record calls on the same login
            data = _read(login)
            data[info.rate_limit_type or "unknown"] = {
                "status": info.status, "resets_at": info.resets_at, "utilization": info.utilization,
                "overage": info.overage_status, "seen": now}
            write_atomic(file, json.dumps(data))
    except Exception as e:
        log.warning("couldn't record Claude usage: %s", type(e).__name__)


def _ends(w: dict) -> float:
    """When a window's known state expires."""
    if w.get("resets_at"):
        return float(w["resets_at"])
    return w.get("seen", 0) + (UNKNOWN_RESET if w.get("status") == "rejected" else FORGET)


def windows(now: float | None = None, kept: dict | None = None) -> dict[str, dict]:
    """Known state of each window that hasn't reset yet: this computer's, or `kept` (another machine's account)."""
    now = now or time.time()
    return {k: w for k, w in (_read() if kept is None else kept).items() if isinstance(w, dict) and _ends(w) > now}


def _used_up(w: dict) -> bool:
    return w.get("status") == "rejected" and w.get("overage") != "allowed"  # extra usage carries on past it


def out(now: float | None = None, kept: dict | None = None) -> tuple[str, dict] | None:
    """(name, details) of a window used up for every model (latest reset if several), or None."""
    found = [(k, w) for k, w in windows(now, kept).items() if k not in OWN and _used_up(w)]
    return max(found, key=lambda kw: _ends(kw[1]), default=None)


def left(now: float | None = None, kept: dict | None = None) -> float:
    """Share of the tightest limit for every model still left, 0 to 1: this computer's login in use, or `kept` (another
    login's or machine's). Claude reports only as limits get close, so nothing reported counts as all of it."""
    if out(now, kept):
        return 0.0
    known = [w for k, w in windows(now, kept).items() if k not in OWN]
    used = [float(w["utilization"]) for w in known if w.get("utilization") is not None]
    if used:
        return round(max(0.0, 1 - max(used)), 2)
    return CLOSE if any(w.get("status") == "allowed_warning" for w in known) else 1.0


def family(model: str) -> str:
    """The family of `model` with a usage window of its own ("opus"), or ""."""
    return f.name if (f := MODELS.family(model)) and f.name in LIGHTER else ""


def _named(model: str) -> str:
    """Spoken family name, e.g. 'Sonnet' for a Sonnet model; the id itself if no family matches."""
    return f.spoken if (f := MODELS.family(model)) else model


def available(model: "Role | str", now: float | None = None) -> str:
    """The model for `model` (a role, tier or id), or a lighter one while its family's weekly window is used up
    (Opus->Sonnet->Haiku)."""
    model = roles.model(model)
    seen = set()
    for _ in range(len(LIGHTER)):
        used = {OWN[k] for k, w in windows(now).items() if k in OWN and _used_up(w)}
        if (f := family(model)) not in used or model in seen:
            break
        seen.add(model)
        log.info("Claude's %s limit is used up; using %s instead of %s", f.capitalize(), LIGHTER[f], model)
        model = LIGHTER[f]
    return model


def used_up_until(now: float | None = None) -> float | None:
    """When the window used up for every model resets (latest if several), or None if none is."""
    return _ends(found[1]) if (found := out(now)) else None


def hold_until(now: float | None = None) -> float | None:
    """Timestamp scheduled model runs should wait until, or None to run now.

    Holds on an all-model window used up, or the five-hour window past USAGE_HOLD (leaving the rest for voice and
    manual tasks).
    """
    now = now or time.time()
    if found := used_up_until(now):
        return found
    five = windows(now).get("five_hour")
    if five and five.get("resets_at") and (five.get("utilization") or 0) >= USAGE_HOLD:
        return float(five["resets_at"])
    return None


def when(ts: float, now: float | None = None) -> str:
    """'at 3 PM', 'at 3:30 PM', or 'on Monday at 9 AM' when it isn't today."""
    at, today = datetime.fromtimestamp(ts, profile.tz()), datetime.fromtimestamp(now or time.time(), profile.tz()).date()
    said = "at " + at.strftime("%-I:%M %p").replace(":00 ", " ")
    return said if at.date() == today else f"on {at:%A} {said}"


def used_up(window: str, resets: float | None, now: float | None = None) -> str:
    """'five-hour limit is used up, resetting at 3 PM', without the reset when it isn't known."""
    return f"{NAMES.get(window, 'usage')} limit is used up" + _resetting(resets, now)


def _resetting(resets: float | None, now: float) -> str:
    return f", resetting {when(resets, now)}" if resets else ""


def _what(name: str) -> str:
    """A window as it's said: 'five-hour', 'weekly Opus'."""
    return NAMES.get(name, name.replace("_", " "))


def _window(name: str, w: dict, now: float) -> str:
    what = _what(name)
    if _used_up(w):
        said = f"the {what} limit is used up"
    elif w.get("utilization") is not None:
        said = f"{round(w['utilization'] * 100)}% of the {what} limit is used"
    elif w.get("status") == "allowed_warning":
        said = f"the {what} limit is getting close"
    elif w.get("status") == "rejected":
        said = f"the {what} limit is used up, and extra usage is carrying on"
    else:
        said = f"the {what} limit is fine"
    return said + _resetting(w.get("resets_at"), now)


def said(now: float | None = None, kept: dict[str, dict] | None = None) -> str:
    """Known state in a sentence or two: this computer's, or `kept` windows (another machine's account)."""
    now = now or time.time()
    known = windows(now, kept)
    if not known:
        return ("Claude hasn't reported anything about the usage limits lately: it says more only as they get close, "
                "so there's nothing to worry about.")
    parts = [_window(k, w, now) for k, w in sorted(known.items(), key=lambda kw: list(NAMES).index(kw[0])
                                                    if kw[0] in NAMES else 99)]
    latest = max(w.get("seen", 0) for w in known.values())
    text = "; ".join(parts)
    return f"{text[0].upper()}{text[1:]} (as of {datetime.fromtimestamp(latest, profile.tz()):%H:%M})."


def alerts(now: float | None = None) -> list[tuple[str, str]]:
    """Warnings as (key, sentence); a key is stable per window until reset, so notices say each once."""
    now = now or time.time()
    out_ = []
    first, on = claude_logins.order()[0], claude_logins.active(now)
    if on != first:  # moved down the priority order
        out_.append((f"usage:login:{first}:{on}", f"Alfred is running on the {on or 'default'} Claude login for now: "
                                                    f"the {first or 'default'} login is first, but "
                                                    f"{claude_logins.skipped(first, now)}."))
    for name, w in windows(now).items():
        what = _what(name)
        reset = f", until it resets {when(w['resets_at'], now)}" if w.get("resets_at") else ""
        if _used_up(w):
            then = (f"Alfred uses {_named(LIGHTER[OWN[name]])} meanwhile" if name in OWN
                    else "scheduled tasks and checks wait for it")
            out_.append((f"usage:{name}:{w.get('resets_at')}:out", f"Claude's {what} limit is used up{reset}; {then}."))
        elif w.get("status") == "allowed_warning" or (w.get("utilization") or 0) >= USAGE_HOLD:
            used = f"{round(w['utilization'] * 100)}% of" if w.get("utilization") is not None else "nearly all of"
            out_.append((f"usage:{name}:{w.get('resets_at')}:close",
                         f"Alfred has used {used} Claude's {what} limit" + (f"; it resets {when(w['resets_at'], now)}."
                                                                            if w.get("resets_at") else ".")))
    return out_
