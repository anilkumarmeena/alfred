"""Do not disturb: "pause all", which holds what JARVIS would otherwise say unasked, until a time or until resumed.

While it's on, the scheduler runs nothing (reminders, scheduled tasks and watches all stay due), the heartbeat checks
nothing, and the runner's task updates make no Mac notification or phone push (they're still said in a live session,
and stay unheard for the next one's greeting). Requests for a yes still come through, since a task waits on them.
When it ends, at its time or on resume, the held reminders are said, saying when they were due; each held task or
watch runs once; and the heartbeat flags what turned up meanwhile. The heartbeat's quiet hours stay as they are.

It's kept in the settings table (jarvis.state.settings) under "hold", as {"since", "until", "ended"}, so the voice
agent's tools, the control panel and the task runner share it. The last hold is kept once it's over, so the
scheduler can tell a run held on purpose from one missed while JARVIS was off (covered).
"""

import json
import time
from datetime import datetime, timedelta

from jarvis.core import profile
from jarvis.state import settings
from jarvis.tasks import schedules

KEY = "hold"
MAX_DAYS = 7  # the furthest a hold's end can be


def _read() -> dict:
    try:
        found = json.loads(settings.get(KEY) or "{}")
    except ValueError:
        return {}
    return found if isinstance(found, dict) else {}


def _over(h: dict, now: float) -> float | None:
    """When the hold ended (or ends, if that's passed), or None while it's on."""
    if h.get("ended"):
        return h["ended"]
    if h.get("until") and h["until"] <= now:
        return h["until"]
    return None


def status(now: float | None = None) -> dict | None:
    """The hold under way, {"since", "until"} (until None: till resumed), or None."""
    now = now or time.time()
    h = _read()
    if not h.get("since") or _over(h, now) is not None:
        return None
    return {"since": h["since"], "until": h.get("until")}


def held(now: float | None = None) -> bool:
    return status(now) is not None


def covered(ts: float, now: float | None = None) -> bool:
    """Whether `ts` fell in the hold under way, or in the last one."""
    now = now or time.time()
    h = _read()
    return bool(h.get("since")) and h["since"] <= ts <= (_over(h, now) or now)


def start(until: str = "", minutes: int = 0, now: datetime | None = None) -> dict:
    """Hold everything until `until` (HH:MM or a local date and time), for `minutes`, or with neither till resumed.
    Starting it while it's on changes its end."""
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
    """End the hold under way; returns it, with when it ended, or None if there was none."""
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
