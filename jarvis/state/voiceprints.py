"""Voiceprints: the user's voice as a speaker embedding, for the speaker check (jarvis.voice.speaker). Kept in the
voiceprints table of data/tasks.db, the user's own data beside their settings, never in code or config.

The user JARVIS works for has the name OWNER; anyone else enrolled (`python -m jarvis.voice.speaker enroll --name ...`)
counts as the user too. Each voiceprint names the speaker model that made it: one from another model can't be compared,
so it's left out until enrolled again. Enrolling again replaces it.
"""

import logging
import sqlite3
import time
from contextlib import AbstractContextManager
from dataclasses import dataclass

import numpy as np

from jarvis.core import config, database, profile

log = logging.getLogger("jarvis.state.voiceprints")

OWNER = "owner"

_SCHEMA = """
create table voiceprints (
    name text primary key,      -- whose voice: owner for the user JARVIS works for
    model text not null,        -- the speaker model that made it (config.SPEAKER_MODEL_ID)
    embedding blob not null,    -- float32, unit length
    seconds real not null,      -- of speech it was made from
    consistency real not null,  -- how alike its pieces were: their mean cosine similarity to it
    via text not null,          -- how it was made: voice, panel or cli
    created real not null
)"""


@dataclass
class Voiceprint:
    embedding: np.ndarray
    seconds: float
    consistency: float
    name: str = OWNER
    model: str = config.SPEAKER_MODEL_ID
    via: str = "voice"
    created: float = 0.0

    def described(self) -> dict:
        """As the control panel shows it: no embedding."""
        return {"name": self.name, "seconds": round(self.seconds, 1), "consistency": round(self.consistency, 2),
                "via": self.via, "created": self.created, "current": self.model == config.SPEAKER_MODEL_ID}


def _db() -> AbstractContextManager[sqlite3.Connection]:
    return database.table("voiceprints", _SCHEMA)


def _row(r: sqlite3.Row) -> Voiceprint:
    return Voiceprint(np.frombuffer(r["embedding"], dtype=np.float32).copy(), r["seconds"], r["consistency"], r["name"],
                      r["model"], r["via"], r["created"])


def every() -> list[Voiceprint]:
    """All voiceprints kept, the owner's first, whichever model made them."""
    with _db() as db:
        rows = db.execute("select * from voiceprints order by name != ?, created", (OWNER,)).fetchall()
    return [_row(r) for r in rows]


def load(model: str = config.SPEAKER_MODEL_ID) -> list[Voiceprint]:
    """The voiceprints `model` made, to compare utterances with. A database that can't be read is logged and counts as
    none, so the voice still starts (and answers anyone, as with nobody enrolled)."""
    try:
        return [v for v in every() if v.model == model]
    except sqlite3.Error as e:
        log.error("can't read the voiceprints from %s: %s", profile.current().db.name, e)
        return []


def save(print_: Voiceprint) -> Voiceprint:
    """Keeps a voiceprint, replacing the one of the same name; returns it as kept."""
    print_.created = print_.created or time.time()
    embedding = np.asarray(print_.embedding, dtype=np.float32)
    with _db() as db:
        db.execute("insert or replace into voiceprints values (?, ?, ?, ?, ?, ?, ?)",
                   (print_.name, print_.model, embedding.tobytes(), float(print_.seconds), float(print_.consistency),
                    print_.via, print_.created))
    log.info("voiceprint saved for %s: %.0f s of speech, consistency %.2f, via %s", print_.name, print_.seconds,
             print_.consistency, print_.via)
    return print_


def remove(name: str = OWNER) -> bool:
    """Forgets a voiceprint; False if there was none by that name."""
    with _db() as db:
        return db.execute("delete from voiceprints where name = ?", (name,)).rowcount > 0
