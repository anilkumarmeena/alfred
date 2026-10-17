"""Settings-panel preferences, read when used so a change applies without reloading code: .env first, then the
profile's settings table, then the shipped default (config.PREFERENCES says which variable overrides each; config/
jarvis.toml's [preferences] holds the defaults). When each takes effect is in config.PREFERENCES's comment."""

import os
import sqlite3
from contextlib import closing
from datetime import datetime, time
from pathlib import Path

from jarvis import config
from jarvis.storage import profile


def stored(key: str, db_file: Path | None = None) -> str:
    """Preference from the settings table of `db_file` (default: current profile's DB); "" if absent or unreadable."""
    try:
        with closing(sqlite3.connect(f"file:{db_file or profile.current().db}?mode=ro", uri=True)) as db:
            row = db.execute("select value from settings where key = ?", (key,)).fetchone()
    except sqlite3.Error:
        return ""
    return row[0].strip() if row else ""


def preference(key: str) -> str:
    """Preference from .env, else the settings table; "" means use the caller's default."""
    return os.environ.get(config.PREFERENCES[key], "").strip() or stored(key)


def _number(key: str, span: tuple[float, float]) -> float:
    return config._number(preference(key), float(config.DEFAULTS[key]), *span)


def _mode(key: str, modes: tuple[str, ...]) -> str:
    mode = preference(key).lower()
    return mode if mode in modes else config.DEFAULTS[key]


def max_tasks() -> int:
    """Tasks running at once; the rest queue. Read at service start."""
    return int(_number("max_tasks", config.MAX_TASKS_RANGE))


def tts_voice() -> str:
    """Kokoro voice id: b*/a* = British/American, then m/f."""
    return preference("voice") or config.DEFAULTS["voice"]


def tts_speed() -> float:
    return _number("voice_speed", config.SPEED_RANGE)


def speech_gate() -> str:
    return _mode("speech_gate", config.SPEECH_GATE_MODES)


def speech_gate_save_audio() -> bool:
    return config.speech_gate_save_audio(speech_gate())


def speaker_check() -> str:
    return _mode("speaker_check", config.SPEAKER_MODES)


def speaker_threshold() -> float:
    """Cosine similarity needed for an utterance of at least SPEAKER_LONG_SECONDS of speech."""
    return _number("speaker_threshold", config.SPEAKER_THRESHOLD_RANGE)


def _on(key: str) -> bool:
    return config._switch(preference(key), config._switch(config.DEFAULTS[key], True))


def browser_on() -> bool:
    return _on("browser")


def browser_headless() -> bool:
    return (preference("browser_window") or config.DEFAULTS["browser_window"]).lower() == "headless"


def injection_check_on() -> bool:
    return _on("injection_check")


def injection_model() -> str:
    return preference("injection_model") or config.DEFAULTS["injection_model"]


def quiet_window(spec: str) -> tuple[time, time]:
    """A quiet_hours value, '23:00-08:00' -> (23:00, 08:00). ValueError unless it's two different HH:MM times."""
    start, end = (datetime.strptime(p.strip(), "%H:%M").time() for p in spec.split("-"))
    if start == end:
        raise ValueError("quiet hours that start when they end")
    return start, end
