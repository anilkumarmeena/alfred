"""Settings the user changes as they go, kept in the settings table of data/tasks.db beside the tasks, the to-do list
and the Google accounts, so the web app can change them too.

"about" is the user's "about you" text, what JARVIS knows about them (their job, their own projects, what to keep
apart), one point per line, which the prompt passes on (brain._about). The services read it at start-up, so restart
them after a change.

The preferences (config.PREFERENCES) are the rest: name, time zone, voice, speed, speech gate, how many tasks run at
once, the web browser (on or off, its window headed or headless), the speaker check (on, shadow or off) with its
threshold, and the safety check for hidden instructions in what tools bring in (on or off, and its model). config
reads them, a preference's variable in .env winning over what's here; see there for when each applies. Name and time
zone are kept in the profile (jarvis.core.profile) rather than here, and apply without a restart.

    uv run python -m jarvis.state.settings about                     show it
    uv run python -m jarvis.state.settings about --set "Works at Acme."   replace it; a newline starts another point
    uv run python -m jarvis.state.settings about --from FILE         replace it with a file's text, or stdin's with -
    uv run python -m jarvis.state.settings about --clear
    uv run python -m jarvis.state.settings prefs                     show the preferences
    uv run python -m jarvis.state.settings prefs voice bm_lewis      change one; "" goes back to the default
"""

import argparse
import logging
import os
import sqlite3
import sys
from contextlib import AbstractContextManager
from pathlib import Path

import numpy as np

from jarvis.core import config, database, profile

log = logging.getLogger("jarvis.state.settings")

RESTART = "Restart the services for JARVIS to see the change: make services-restart"

_SCHEMA = """
create table settings (
    key text primary key,
    value text not null
)"""


def tidy(text: str) -> str:
    """One point per line: each line trimmed, blank lines left out."""
    return "\n".join(l for l in (l.strip() for l in text.splitlines()) if l)


def _db() -> AbstractContextManager[sqlite3.Connection]:
    return database.table("settings", _SCHEMA)


def get(key: str, default: str = "") -> str:
    with _db() as db:
        row = db.execute("select value from settings where key = ?", (key,)).fetchone()
    return row[0] if row else default


def put(key: str, value: str) -> None:
    """Sets `key` to `value`; an empty value takes the setting out."""
    with _db() as db:
        if value:
            db.execute("insert into settings values (?, ?) on conflict(key) do update set value = excluded.value",
                       (key, value))
        else:
            db.execute("delete from settings where key = ?", (key,))


def about() -> str:
    """The user's "about you" text. A database that can't be read is logged and counts as none, so JARVIS starts."""
    try:
        return get("about")
    except sqlite3.Error as e:
        log.error("can't read \"about you\" from %s: %s", profile.current().db.name, e)
        return ""


def set_about(text: str) -> str:
    """Replaces the "about you" text, tidied (see `tidy`), and returns it as kept."""
    text = tidy(text)
    put("about", text)
    return text


# ── Preferences ─────────────────────────────────────────────────

VOICES_FILE = config.ROOT / "models/voices-v1.0.bin"
ACCENTS = {"a": "American", "b": "British"}


def voices() -> list[str]:
    """Kokoro's English voices, as its voices file has them: a* American, b* British; then f or m."""
    try:
        with np.load(VOICES_FILE) as found:
            names = found.files
    except (OSError, ValueError):
        names = []
    return sorted(n for n in names if n[:1] in ACCENTS and n[1:3] in ("f_", "m_")) or [config.tts_voice()]


def _clean(key: str, value: str) -> str:
    """`value` as kept for preference `key`; raises ValueError, with what to say, for one it can't take."""
    if not (value := value.strip()):
        return ""
    if key == "name":
        if len(value) > 40:
            raise ValueError("Keep the name to 40 characters.")
        return value
    if key == "timezone":
        if not config.zone_ok(value):
            raise ValueError(f"{value} isn't a time zone. Use one like Europe/London or Asia/Kolkata.")
        return value
    if key == "voice":
        if value not in voices():
            raise ValueError(f"There's no voice called {value}.")
        return value
    if key == "speech_gate":
        if value.lower() not in config.SPEECH_GATE_MODES:
            raise ValueError(f"The speech gate is {', '.join(config.SPEECH_GATE_MODES)}.")
        return value.lower()
    if key == "speaker_check":
        if value.lower() not in config.SPEAKER_MODES:
            raise ValueError(f"The speaker check is {', '.join(config.SPEAKER_MODES)}.")
        return value.lower()
    if key in ("browser", "injection_check"):
        if value.lower() not in config.ON + config.OFF:
            raise ValueError(f"The {'browser' if key == 'browser' else 'safety check'} is on or off.")
        return "on" if value.lower() in config.ON else "off"
    if key == "injection_model":
        if value not in config.INJECTION_MODELS:
            raise ValueError(f"The safety check runs on {' or '.join(config.INJECTION_MODELS)}.")
        return value
    if key == "browser_window":
        if value.lower() not in config.BROWSER_WINDOWS:
            raise ValueError(f"The browser window is {' or '.join(config.BROWSER_WINDOWS)}.")
        return value.lower()
    fraction = key in ("voice_speed", "speaker_threshold")
    low, high = {"voice_speed": config.SPEED_RANGE,
                 "speaker_threshold": config.SPEAKER_THRESHOLD_RANGE}.get(key, config.MAX_TASKS_RANGE)
    try:
        number = float(value) if fraction else int(value)
    except ValueError:
        raise ValueError(f"{value} isn't a number.") from None
    if not low <= number <= high:
        raise ValueError(f"Pick a number from {low:g} to {high:g}.")
    return f"{number:g}"


PROFILE_KEYS = ("name", "timezone")  # kept in the profile, not the settings table


def set_preference(key: str, value: str) -> None:
    """Keeps preference `key`; an empty value goes back to the default. Raises ValueError for an unknown key or a
    value it can't take."""
    if key not in config.PREFERENCES:
        raise ValueError(f"There's no {key} setting.")
    if key in PROFILE_KEYS:
        profile.change(profile.current().id, **{key: _clean(key, value)})
    else:
        put(key, _clean(key, value))


def preferences() -> dict:
    """Each preference as the Settings panel shows it: what's kept here, what applies with nothing kept, and what .env
    sets (which wins, so the panel can't change it); and the voices to pick from."""
    defaults = config.DEFAULTS | {"name": config.mac_name() or "the user", "timezone": config.mac_timezone() or "UTC"}
    mine = profile.stored(profile.current().id)  # name and time zone are kept there

    def kept(key: str) -> str:
        return (mine.get(key) or "") if key in PROFILE_KEYS else get(key)

    shown = {key: {"value": kept(key), "default": defaults[key], "env": os.environ.get(var, "").strip()}
             for key, var in config.PREFERENCES.items()}
    return {"preferences": shown, "voices": voices()}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m jarvis.state.settings", description="JARVIS's settings.")
    commands = parser.add_subparsers(dest="command", required=True)
    about_ = commands.add_parser("about", help='show or change the "about you" text')
    how = about_.add_mutually_exclusive_group()
    how.add_argument("--set", metavar="TEXT", help="replace it with TEXT; a newline starts another point")
    how.add_argument("--from", dest="source", metavar="FILE", help="replace it with FILE's text, or stdin's with -")
    how.add_argument("--clear", action="store_true", help="take it out")
    prefs = commands.add_parser("prefs", help="show the preferences, or change one")
    prefs.add_argument("key", nargs="?", choices=list(config.PREFERENCES))
    prefs.add_argument("value", nargs="?", help='the new value; "" goes back to the default')
    args = parser.parse_args(argv)

    if args.command == "prefs":
        return _prefs(args.key, args.value)

    if args.set is None and args.source is None and not args.clear:
        print(about() or 'No "about you" yet. Set it with: uv run python -m jarvis.state.settings about --set "..."')
        return 0
    if args.source is not None:
        try:
            text = sys.stdin.read() if args.source == "-" else Path(args.source).expanduser().read_text()
        except OSError as e:
            sys.exit(f"Couldn't read {args.source}: {e}")
    else:
        text = "" if args.clear else args.set
    kept = set_about(text)
    print(f'"About you" is now:\n{kept}' if kept else '"About you" is empty now.')
    print(RESTART)
    return 0


def _prefs(key: str | None, value: str | None) -> int:
    if key and value is not None:
        try:
            set_preference(key, value)
        except ValueError as e:
            sys.exit(str(e))
    for name, p in preferences()["preferences"].items():
        if key in (None, name):
            print(f"{name}: {p['env'] or p['value'] or p['default']}"
                  + (" (from .env)" if p["env"] else "" if p["value"] else " (default)"))
    if key and value is not None:
        print(RESTART)
    return 0


if __name__ == "__main__":
    sys.exit(main())
