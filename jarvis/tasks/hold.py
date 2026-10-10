"""Do not disturb ("pause all"): hold unasked output until a time or until resumed.

While on: the scheduler runs nothing (items stay due), the heartbeat checks nothing, and task updates skip desktop
notifications and phone pushes (still said in a live session). Approval requests still come through.
On end: held reminders are said with their due time, each held task/watch runs once, the heartbeat catches up.

Stored in settings under "hold" as {"since", "until", "ended"}. The last hold is kept after it ends so the
scheduler can tell a deliberately held run from one missed while JARVIS was off (`covered`).
"""

import json
import time
from datetime import datetime, timedelta

from jarvis.core import profile
from jarvis.state import settings
from jarvis.tasks import schedules

KEY = "hold"
MAX_DAYS = 7  # longest allowed hold


def _read() -> dict:
    try:
        found = json.loads(settings.get(KEY) or "{}")
    except ValueError:
        return {}
    return found if isinstance(found, dict) else {}


def _over(h: dict, now: float) -> float | None:
    """End time if the hold is over, else None."""
    if h.get("ended"):
        return h["ended"]
    if h.get("until") and h["until"] <= now:
        return h["until"]
    return None


def status(now: float | None = None) -> dict | None:
    """Active hold as {"since", "until"} (until None: till resumed), or None."""
    now = now or time.time()
    h = _read()
    if not h.get("since") or _over(h, now) is not None:
        return None
    return {"since": h["since"], "until": h.get("until")}


def held(now: float | None = None) -> bool:
    return status(now) is not None


def covered(ts: float, now: float | None = None) -> bool:
    """Whether `ts` fell in the active or most recent hold."""
    now = now or time.time()
    h = _read()
    return bool(h.get("since")) and h["since"] <= ts <= (_over(h, now) or now)


def start(until: str = "", minutes: int = 0, now: datetime | None = None) -> dict:
    """Hold until `until` (HH:MM or local datetime), for `minutes`, or till resumed. Re-starting changes the end."""
    now = now or datetime.now(profile.tz())
    if until and minutes:
        raise ValueError("Give until or minutes, not both.")
    if minutes < 0:
        raise ValueError("Minutes can't be negative.")
    end = schedules.parse_time(until, now) if until else now + timedelta(minutes=minutes) if minutes else None
    if end and end <= now:
        raise ValueError(f"{end:%Y-%m-%d %H:%M} has already passed.")
    if end and end > now + timedelta(days=MAX_DAYS):
        raise ValueError(f"Do not disturb lasts at most {MAX_DAYS} days; leave the end out to hold until resumed.")
    since = (status(now.timestamp()) or {}).get("since") or now.timestamp()
    settings.put(KEY, json.dumps({"since": since, "until": end.timestamp() if end else None, "ended": None}))
    return status(now.timestamp())


def end(now: float | None = None) -> dict | None:
    """End the active hold; return it with "ended" set, or None if none."""
    now = now or time.time()
    if not (h := status(now)):
        return None
    settings.put(KEY, json.dumps(h | {"ended": now}))
    return h | {"ended": now}


def describe(h: dict | None, now: datetime | None = None) -> str:
    """'on until 15:00', 'on until resumed', or 'off'."""
    if not h:
        return "off"
    return f"on until {schedules.clock(h['until'], now)}" if h.get("until") else "on until resumed"
