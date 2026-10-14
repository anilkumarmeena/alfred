"""Per-person profiles, stored in the profiles table of the install database (config.DB_FILE).

A profile has a name, time zone, data folder, database and secrets folder. Install-wide state (code, models, Claude
login, runner socket, worktrees, caches) stays in config.

The "owner" profile uses data/ and data/tasks.db; its name/time zone come from .env, then the table, then the computer.
current() resolves: context (`using`) > process (`activate`) > owner, re-read at most every FRESH_SECONDS so
Settings changes apply without a restart.
"""

import logging
import os
import re
import sqlite3
import time
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

# config and database are imported lazily: config reads the current profile's DB at import time.

log = logging.getLogger("jarvis.core.profile")

OWNER = "owner"
FRESH_SECONDS = 1.0
ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")  # used as a folder name

_SCHEMA = """
create table profiles (
    id text primary key,
    name text not null default '',      -- '' = computer account's first name
    timezone text not null default '',  -- '' = computer's time zone
    data_dir text,                      -- null = data/ (owner) or data/profiles/<id>
    db_file text,                       -- null = config.DB_FILE (owner) or <data_dir>/tasks.db
    created real not null
)"""


@dataclass(frozen=True)
class Profile:
    id: str
    name: str  # resolved, never empty
    timezone: str  # resolved, always valid
    data: Path
    db: Path
    secrets: Path  # sign-in tokens; inside secrets/, so hidden from tasks

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    # Files in the data folder
    @property
    def memory_file(self) -> Path:
        return self.data / "memory.md"  # long-term facts (jarvis.state.memory)

    @property
    def recent_file(self) -> Path:
        return self.data / "recent.md"  # latest exchanges

    @property
    def conversations(self) -> Path:
        return self.data / "conversations"  # full log, one YYYY-MM-DD.md per day

    @property
    def vocabulary_file(self) -> Path:
        return self.data / "vocabulary.md"  # speech vocabulary (jarvis.state.vocabulary)

    @property
    def pictures(self) -> Path:
        return self.data / "pictures"  # photos and screenshots (jarvis.state.pictures)

    @property
    def transcripts(self) -> Path:
        return self.data / "transcripts"  # jarvis.core.transcribe output

    @property
    def shared_files(self) -> Path:
        return self.data / "shared_files.json"  # files sent from the phone

    @property
    def notices_file(self) -> Path:
        return self.data / "notices.json"  # notices already given (jarvis.tasks.notices)

    @property
    def learning_file(self) -> Path:
        return self.data / "learning.json"  # learning review progress

    @property
    def skills(self) -> Path:
        return self.data / "skills"  # one folder per skill

    @property
    def push_subscriptions(self) -> Path:
        return self.data / "push_subscriptions.json"  # Web Push subscriptions, one per device

    @property
    def push_devices(self) -> Path:
        return self.data / "push_devices.json"  # APNs tokens, one per app install

    @property
    def fcm_devices(self) -> Path:
        return self.data / "fcm_devices.json"  # FCM tokens, one per Android app install

    @property
    def audio(self) -> Path:
        return self.data / "audio"  # audio files being rendered

    @property
    def shell(self) -> Path:
        return self.data / "shell"  # voice brain cwd; its sandbox's only writable folder

    @property
    def browser_profile(self) -> Path:
        return self.data / "browser/profile"  # cookies are logins: protected like secrets/

    @property
    def browser_shots(self) -> Path:
        return self.data / "browser/screenshots"

    @property
    def voice_logs(self) -> Path:
        """Voice brain's Claude session transcripts, in the login in use (the CLI names the folder after cwd `shell`)."""
        from jarvis.core import claude_logins
        return claude_logins.folder(claude_logins.active()) / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(self.shell))


def _db() -> AbstractContextManager[sqlite3.Connection]:
    from jarvis.core import config, database
    return database.table("profiles", _SCHEMA, made=_seed, at=config.DB_FILE)


def _seed() -> None:
    """Create the owner's row, seeding name and time zone from .env."""
    from jarvis.core import config
    found = {k: os.environ.get(config.PREFERENCES[k], "").strip() for k in ("name", "timezone")}
    with _db() as db:
        db.execute("insert or ignore into profiles (id, name, timezone, created) values (?, ?, ?, ?)",
                   (OWNER, found["name"], found["timezone"], time.time()))


def resolve(row: dict) -> Profile:
    """Build a Profile from a row, filling gaps from .env (owner only), then the computer, then defaults."""
    from jarvis.core import config
    pid, owner = row["id"], row["id"] == OWNER
    env = (lambda key: os.environ.get(config.PREFERENCES[key], "").strip()) if owner else (lambda key: "")
    name = env("name") or row.get("name") or config.mac_name() or "the user"
    zone = next((z for z in (env("timezone"), row.get("timezone"), config.mac_timezone())
                 if z and config.zone_ok(z)), "UTC")
    data = Path(row["data_dir"]) if row.get("data_dir") else config.DATA_DIR / ("" if owner else f"profiles/{pid}")
    db = Path(row["db_file"]) if row.get("db_file") else config.DB_FILE if owner else data / "tasks.db"
    secrets = config.SECRETS_DIR if owner else config.SECRETS_DIR / "profiles" / pid
    return Profile(pid, name, zone, data, db, secrets)


def load(pid: str = OWNER) -> Profile:
    """Read profile `pid` now. If the DB is unreadable, the owner gets defaults (logged) so JARVIS still starts."""
    try:
        with _db() as db:
            row = db.execute("select * from profiles where id = ?", (pid,)).fetchone()
    except sqlite3.Error as e:
        if pid != OWNER:
            raise
        log.error("can't read the profiles, so the owner's is the defaults: %s", e)
        row = None
    if row is None and pid != OWNER:
        raise KeyError(f"There's no profile {pid}.")
    return resolve(dict(row) if row else {"id": OWNER})


def stored(pid: str = OWNER) -> dict:
    """Raw stored row (unresolved), as the Settings panel edits it."""
    with _db() as db:
        row = db.execute("select * from profiles where id = ?", (pid,)).fetchone()
    return dict(row) if row else {}


def add(pid: str, name: str = "", timezone: str = "") -> Profile:
    """Create a profile with data in data/profiles/<pid>. ValueError for an invalid or taken id."""
    if not ID.fullmatch(pid) or pid == OWNER:
        raise ValueError(f"{pid!r} can't be a profile's id: lowercase letters, digits, - and _, at most 32.")
    try:
        with _db() as db:
            db.execute("insert into profiles (id, name, timezone, created) values (?, ?, ?, ?)",
                       (pid, name.strip(), timezone.strip(), time.time()))
    except sqlite3.IntegrityError:
        raise ValueError(f"There's already a profile {pid}.") from None
    made = load(pid)
    for folder in (made.data, made.secrets):
        folder.mkdir(parents=True, exist_ok=True)
    return made


def change(pid: str = OWNER, **fields: str) -> Profile:
    """Update name and/or time zone ("" = default); return the reloaded profile."""
    if unknown := set(fields) - {"name", "timezone"}:
        raise ValueError(f"A profile has no {', '.join(sorted(unknown))}.")
    with _db() as db:
        columns = ", ".join(f"{k} = ?" for k in fields)  # keys checked above
        db.execute(f"update profiles set {columns} where id = ?", (*(v.strip() for v in fields.values()), pid))
    forget()
    return load(pid)


# ── Which profile code runs as ───────────────────────────────────

_session: ContextVar[Profile | None] = ContextVar("jarvis_profile", default=None)
_process: Profile | None = None
_owner: tuple[float, Profile] | None = None


def current() -> Profile:
    """Active profile: context's, else process's, else owner's."""
    return _session.get() or _process or owner()


def name() -> str:
    """Current profile's name."""
    return current().name


def tz() -> ZoneInfo:
    """Current profile's time zone."""
    return current().tz


def owner() -> Profile:
    global _owner
    if _owner is None or time.monotonic() - _owner[0] > FRESH_SECONDS:
        _owner = (time.monotonic(), load(OWNER))
    return _owner[1]


def forget() -> None:
    """Drop the cached owner profile."""
    global _owner
    _owner = None


def use(profile: Profile) -> Token:
    """Set `profile` for the rest of this context and asyncio tasks started from it; returns the reset token."""
    return _session.set(profile)


@contextmanager
def using(profile: Profile) -> Iterator[Profile]:
    """Run the block (and asyncio tasks it starts) as `profile`."""
    token = use(profile)
    try:
        yield profile
    finally:
        _session.reset(token)


def activate(profile: Profile | None) -> None:
    """Set the process-wide profile, all threads included (None = owner). For single-person processes."""
    global _process
    _process = profile
