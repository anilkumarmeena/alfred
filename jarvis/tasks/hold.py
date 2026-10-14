"""Do not disturb ("pause all"): hold unasked output until a time or until resumed. Also the nightly quiet hours, the
user's quiet_hours preference (watches and notices skip them).

While on: the scheduler runs nothing (items stay due), notices (jarvis.tasks.notices) wait, and task updates skip
desktop notifications and phone pushes (still said in a live session). Approval requests still come through.
On end: held reminders are said with their due time, each held task/watch runs once, and notices catch up.

Stored in settings under "hold" as {"since", "until", "ended"}. The last hold is kept after it ends so the
scheduler can tell a deliberately held run from one missed while JARVIS was off (`covered`).
"""

import functools
import json
import logging
import time
from datetime import datetime, time as clock_time, timedelta

from jarvis.core import config, profile
from jarvis.state import settings
from jarvis.tasks import schedules

log = logging.getLogger("jarvis.tasks.hold")
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


# ── Quiet hours ─────────────────────────────────────────────────


def window(spec: str) -> tuple[clock_time, clock_time]:
    """'23:00-08:00' -> (23:00, 08:00). ValueError unless it's two different HH:MM times."""
    start, end = (datetime.strptime(p.strip(), "%H:%M").time() for p in spec.split("-"))
    if start == end:
        raise ValueError("quiet hours that start when they end")
    return start, end


@functools.cache  # read at every check: a bad value is warned about once
def parse_quiet_hours(spec: str) -> tuple[clock_time, clock_time] | None:
    """'23:00-08:00' -> (23:00, 08:00); None if empty, "off" or invalid."""
    if not (spec := spec.strip()) or spec.lower() in config.OFF:
        return None
    try:
        return window(spec)
    except ValueError:
        log.warning("quiet hours should be two different times like 23:00-08:00, not %r; ignoring them", spec)
        return None


def quiet_hours(spec: str | None = None) -> tuple[clock_time, clock_time] | None:
    """The nightly quiet hours: `spec` if given, else the current user's quiet_hours preference (read now, so a change
    applies at once); None if they're off."""
    if spec is None:
        spec = config.preference("quiet_hours") or config.DEFAULTS["quiet_hours"]
    return parse_quiet_hours(spec)


def is_quiet(now: datetime, spec: str | None = None) -> bool:
    """Whether `now` falls in the quiet hours (`spec`, else the current user's)."""
    return in_quiet_hours(now, quiet_hours(spec))


def in_quiet_hours(now: datetime, quiet: tuple[clock_time, clock_time] | None) -> bool:
    if quiet is None:
        return False
    start, end, t = *quiet, now.time()
    return start <= t < end if start < end else t >= start or t < end
