"""How close JARVIS's Claude login is to its usage limits, as the CLI last reported them.

The CLI reports the limit with a rate_limit_event (the SDK's RateLimitEvent) whenever its state changes: allowed,
allowed_warning (getting close) or rejected (used up), for one window (five_hour, seven_day, seven_day_opus,
seven_day_sonnet or overage), with when that window resets and, when it says, the fraction used. The voice brain and
the task runner record what they see here, in data/usage.json, so both processes, the scheduler and the heartbeat
share the latest word. Nothing here asks Claude anything: what's known is as fresh as the last call either process
made, and a window past its reset counts as fresh again.

  windows()    what's known now, by window
  out()        a window that's used up for every model (so a lighter model won't help), or None
  available()  the model to use: a lighter one while the model's own weekly window is used up
  hold_until() when scheduled runs that need a model should wait until: the reset of a window that's used up, or of
               the five-hour window once USAGE_HOLD of it is used
  said()       all of it in a sentence or two, for usage_left
"""

import json
import logging
import re
import time
from datetime import datetime

from jarvis.core import profile
from jarvis.core.config import FAST_MODEL, HELPER_MODEL, USAGE_FILE, USAGE_HOLD
from jarvis.core.files import write_atomic

log = logging.getLogger("jarvis.brain.usage")

NAMES = {"five_hour": "five-hour", "seven_day": "weekly", "seven_day_opus": "weekly Opus",
         "seven_day_sonnet": "weekly Sonnet", "overage": "extra-usage", "unknown": "usage"}
OWN = {"seven_day_opus": "opus", "seven_day_sonnet": "sonnet"}  # windows that hold back one family of models only
LIGHTER = {"opus": HELPER_MODEL, "sonnet": FAST_MODEL}  # what to use instead while a family's window is used up
UNKNOWN_RESET = 30 * 60  # a window reported used up with no reset time is taken as used up for this long
FORGET = 6 * 3600  # a window with no reset time is forgotten this long after it was last reported
# For when no rate-limit event arrived: the CLI's own wording for the limit, in a failed call's error
LIMIT_TEXT = re.compile(r"usage limit|rate.?limit|hit your limit|limit reached", re.IGNORECASE)


def _read() -> dict[str, dict]:
    try:
        data = json.loads(USAGE_FILE.read_text())
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        log.warning("%s is unreadable; starting afresh", USAGE_FILE.name)
        return {}


def record(info, now: float | None = None) -> None:
    """Keep what a RateLimitEvent's rate_limit_info says, for its window. Never raises: it's only bookkeeping."""
    now = now or time.time()
    try:
        data = _read()
        data[info.rate_limit_type or "unknown"] = {
            "status": info.status, "resets_at": info.resets_at, "utilization": info.utilization,
            "overage": info.overage_status, "seen": now}
        write_atomic(USAGE_FILE, json.dumps(data))
    except Exception as e:
        log.warning("couldn't record Claude usage: %s", type(e).__name__)


def _ends(w: dict) -> float:
    """When what's known of a window stops being true."""
    if w.get("resets_at"):
        return float(w["resets_at"])
    return w.get("seen", 0) + (UNKNOWN_RESET if w.get("status") == "rejected" else FORGET)


def windows(now: float | None = None) -> dict[str, dict]:
    """What's known of each window now, leaving out windows that have reset since."""
    now = now or time.time()
    return {k: w for k, w in _read().items() if isinstance(w, dict) and _ends(w) > now}


def _used_up(w: dict) -> bool:
    return w.get("status") == "rejected" and w.get("overage") != "allowed"  # extra usage, if on, carries on past it


def out(now: float | None = None) -> tuple[str, dict] | None:
    """The window used up for every model, with its details (the one that resets last, if several); None if none."""
    found = [(k, w) for k, w in windows(now).items() if k not in OWN and _used_up(w)]
    return max(found, key=lambda kw: _ends(kw[1]), default=None)


def family(model: str) -> str:
    return next((f for f in LIGHTER if f in model.lower()), "")


def _named(model: str) -> str:
    """'Sonnet' for claude-sonnet-5, as it's said."""
    return next((f.capitalize() for f in ("opus", "sonnet", "haiku") if f in model.lower()), model)


def available(model: str, now: float | None = None) -> str:
    """`model`, or a lighter one while its own family's weekly window is used up (Opus to Sonnet, Sonnet to Haiku)."""
    seen = set()
    for _ in range(len(LIGHTER)):
        used = {OWN[k] for k, w in windows(now).items() if k in OWN and _used_up(w)}
        if (f := family(model)) not in used or model in seen:
            break
        seen.add(model)
        log.info("Claude's %s limit is used up; using %s instead of %s", f.capitalize(), LIGHTER[f], model)
        model = LIGHTER[f]
    return model


def hold_until(now: float | None = None) -> float | None:
    """When scheduled runs that need a model should wait until, or None to run them now: a window used up for every
    model holds them until it resets, and so does the five-hour window once USAGE_HOLD of it is used, leaving the rest
    for the voice and tasks started by hand."""
    now = now or time.time()
    if found := out(now):
        return _ends(found[1])
    five = windows(now).get("five_hour")
    if five and five.get("resets_at") and (five.get("utilization") or 0) >= USAGE_HOLD:
        return float(five["resets_at"])
    return None


def when(ts: float, now: float | None = None) -> str:
    """'at 3 PM', 'at 3:30 PM', or 'on Monday at 9 AM' when it isn't today."""
    at, today = datetime.fromtimestamp(ts, profile.tz()), datetime.fromtimestamp(now or time.time(), profile.tz()).date()
    said = "at " + at.strftime("%-I:%M %p").replace(":00 ", " ")
    return said if at.date() == today else f"on {at:%A} {said}"


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
    return said + (f", resetting {when(w['resets_at'], now)}" if w.get("resets_at") else "")


def said(now: float | None = None) -> str:
    """What's known, in a sentence or two."""
    now = now or time.time()
    known = windows(now)
    if not known:
        return ("Claude hasn't reported anything about the usage limits lately: it says more only as they get close, "
                "so there's nothing to worry about.")
    parts = [_window(k, w, now) for k, w in sorted(known.items(), key=lambda kw: list(NAMES).index(kw[0])
                                                    if kw[0] in NAMES else 99)]
    latest = max(w.get("seen", 0) for w in known.values())
    text = "; ".join(parts)
    return f"{text[0].upper()}{text[1:]} (as of {datetime.fromtimestamp(latest, profile.tz()):%H:%M})."


def alerts(now: float | None = None) -> list[tuple[str, str]]:
    """What to warn the user about now, as (key, sentence): each key stays the same for one window until it resets,
    so the heartbeat says each once."""
    now = now or time.time()
    out_ = []
    for name, w in windows(now).items():
        what = _what(name)
        reset = f", until it resets {when(w['resets_at'], now)}" if w.get("resets_at") else ""
        if _used_up(w):
            then = (f"JARVIS uses {_named(LIGHTER[OWN[name]])} meanwhile" if name in OWN
                    else "scheduled tasks and checks wait for it")
            out_.append((f"usage:{name}:{w.get('resets_at')}:out", f"Claude's {what} limit is used up{reset}; {then}."))
        elif w.get("status") == "allowed_warning" or (w.get("utilization") or 0) >= USAGE_HOLD:
            used = f"{round(w['utilization'] * 100)}% of" if w.get("utilization") is not None else "nearly all of"
            out_.append((f"usage:{name}:{w.get('resets_at')}:close",
                         f"JARVIS has used {used} Claude's {what} limit" + (f"; it resets {when(w['resets_at'], now)}."
                                                                            if w.get("resets_at") else ".")))
    return out_
