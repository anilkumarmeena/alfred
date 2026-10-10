"""Who JARVIS works for: a profile per person, kept in the profiles table of the install's database (config.DB_FILE).

A profile is the person's name and time zone, and where their own things are: their data folder (memory, the
conversations, the speech vocabulary, pictures, skills, the heartbeat's and the learning review's records, push devices,
the web browser's logins), their database (tasks, the to-do list, projects, Google accounts, settings, secrets) and
their sign-ins' folder. What belongs to the install rather than to anyone (the code, the models, JARVIS's Claude login
and its usage, the task runner's socket, worktrees and caches) stays in config.

The first profile, "owner", is made from what the install already had: the name and time zone from .env
(JARVIS_USER_NAME, JARVIS_TIMEZONE) or the Settings panel, data/ for its data and data/tasks.db for its database, so
nothing moves. Its name and time zone in .env still win over the table's, as every preference's variable does; with
neither, the Mac account's first name and the Mac's time zone.

Code asks for current(): the profile the session or task it runs in was given (`using`), else the one this process was
given (`activate`), else the owner's, read afresh from the database at most once every FRESH_SECONDS, so a name or time
zone changed in the Settings panel applies without a restart.
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

# config and database are imported where they're used: config reads preferences from the current profile's database as
# it loads, so this module must be whole before config starts.

log = logging.getLogger("jarvis.core.profile")

OWNER = "owner"
FRESH_SECONDS = 1.0
ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")  # it names folders

_SCHEMA = """
create table profiles (
    id text primary key,
    name text not null default '',      -- what JARVIS calls them; '' = the Mac account's first name
    timezone text not null default '',  -- '' = the Mac's own
    data_dir text,                      -- their data folder; null = the owner's data/, anyone else's data/profiles/<id>
    db_file text,                       -- their database; null = the owner's config.DB_FILE, anyone else's tasks.db in data_dir
    created real not null
)"""


@dataclass(frozen=True)
class Profile:
    id: str
    name: str  # what JARVIS calls them, as resolved: never empty
    timezone: str  # a valid zone name, as resolved
    data: Path
    db: Path
    secrets: Path  # their sign-ins (Google tokens), as hidden from tasks as the install's secrets/ (it's inside it)

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    # Their files, in their data folder
    @property
    def memory_file(self) -> Path:
        return self.data / "memory.md"  # long-term facts (jarvis.state.memory)

    @property
    def recent_file(self) -> Path:
        return self.data / "recent.md"  # the latest exchanges

    @property
    def conversations(self) -> Path:
        return self.data / "conversations"  # every exchange, one file a day: YYYY-MM-DD.md

    @property
    def vocabulary_file(self) -> Path:
        return self.data / "vocabulary.md"  # what speech recognition should expect (jarvis.state.vocabulary)

    @property
    def pictures(self) -> Path:
        return self.data / "pictures"  # photos sent and screenshots (jarvis.state.pictures)

    @property
    def transcripts(self) -> Path:
        return self.data / "transcripts"  # recordings transcribed (jarvis.core.transcribe)

    @property
    def shared_files(self) -> Path:
        return self.data / "shared_files.json"  # files sent from the phone (jarvis.state.shared_files)

    @property
    def heartbeat_file(self) -> Path:
        return self.data / "heartbeat.json"  # what the heartbeat has already flagged

    @property
    def learning_file(self) -> Path:
        return self.data / "learning.json"  # how far through the conversations the learning review has read

    @property
    def skills(self) -> Path:
        return self.data / "skills"  # their skills, a folder each (jarvis.skills)

    @property
    def push_subscriptions(self) -> Path:
        return self.data / "push_subscriptions.json"  # Web Push, one per device that tapped Enable notifications

    @property
    def push_devices(self) -> Path:
        return self.data / "push_devices.json"  # APNs device tokens, one per app install

    @property
    def audio(self) -> Path:
        return self.data / "audio"  # audio files being rendered (jarvis.voice.audio_file)

    @property
    def shell(self) -> Path:
        return self.data / "shell"  # the voice brain's working folder, and the only place its sandbox may write

    @property
    def browser_profile(self) -> Path:
        return self.data / "browser/profile"  # its cookies are logins: kept as secret as secrets/ (safety.commands)

    @property
    def browser_shots(self) -> Path:
        return self.data / "browser/screenshots"

    @property
    def voice_logs(self) -> Path:
        """The voice brain's Claude session transcripts: the CLI names each project folder after its cwd, `shell`."""
        from jarvis.core import config
        return Path(config.CLAUDE_CONFIG_DIR) / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(self.shell))


def _db() -> AbstractContextManager[sqlite3.Connection]:
    from jarvis.core import config, database
    return database.table("profiles", _SCHEMA, made=_seed, at=config.DB_FILE)


def _seed() -> None:
    """The owner's profile, from what the install had before profiles: its name and time zone as .env or the settings
    table had them. Both empty fall back at use, to the Mac's."""
    from jarvis.core import config
    found = {k: os.environ.get(config.PREFERENCES[k], "").strip() or config.stored(k, config.DB_FILE)
             for k in ("name", "timezone")}
    with _db() as db:
        db.execute("insert or ignore into profiles (id, name, timezone, created) values (?, ?, ?, ?)",
                   (OWNER, found["name"], found["timezone"], time.time()))


def resolve(row: dict) -> Profile:
    """A profile as code uses it, from its row: the owner's .env and the Mac's own filling in what it lacks."""
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
    """The profile `pid`, read now. The owner's, from a database that can't be read, is the install's defaults
    (logged), so JARVIS still starts."""
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
    """The profile's row as kept, before anything fills it in: what the Settings panel shows and changes."""
    with _db() as db:
        row = db.execute("select * from profiles where id = ?", (pid,)).fetchone()
    return dict(row) if row else {}


def add(pid: str, name: str = "", timezone: str = "") -> Profile:
    """A new person's profile, their data in data/profiles/<pid>. Raises ValueError for a bad or taken id."""
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
    """Changes the profile's name or time zone ("" goes back to the default); returns it as it is now."""
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
    """The profile this code runs as: its session's or task's, else its process's, else the owner's."""
    return _session.get() or _process or owner()


def name() -> str:
    """What JARVIS calls the person it's working for now."""
    return current().name


def tz() -> ZoneInfo:
    """Their time zone."""
    return current().tz


def owner() -> Profile:
    global _owner
    if _owner is None or time.monotonic() - _owner[0] > FRESH_SECONDS:
        _owner = (time.monotonic(), load(OWNER))
    return _owner[1]


def forget() -> None:
    """Read the owner's profile afresh next time, as after a change."""
    global _owner
    _owner = None


def use(profile: Profile) -> Token:
    """Runs the rest of this context, and the asyncio tasks it starts from here on, as `profile`, such as a whole
    conversation (jarvis.state.conversation.begin)."""
    return _session.set(profile)


@contextmanager
def using(profile: Profile) -> Iterator[Profile]:
    """Runs the block, and the asyncio tasks it starts, as `profile`."""
    token = use(profile)
    try:
        yield profile
    finally:
        _session.reset(token)


def activate(profile: Profile | None) -> None:
    """Makes `profile` this whole process's, its threads' included (None: back to the owner's), for a process that
    works for one person."""
    global _process
    _process = profile
