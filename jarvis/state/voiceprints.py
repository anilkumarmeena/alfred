"""Speaker embeddings for the speaker check (jarvis.voice.speaker), in the profile database's voiceprints table.

The main user is OWNER; anyone else enrolled (`python -m jarvis.voice.speaker enroll --name ...`) also passes.
Voiceprints from a different speaker model are ignored until re-enrolled. Re-enrolling replaces by name.
"""

import logging
import sqlite3
import time
from contextlib import AbstractContextManager
from dataclasses import dataclass

import numpy as np

from jarvis import config
from jarvis.storage import database, profile

log = logging.getLogger("jarvis.state.voiceprints")

OWNER = "owner"

_SCHEMA = """
create table voiceprints (
    name text primary key,      -- 'owner' = main user
    model text not null,        -- config.SPEAKER_MODEL_ID that produced it
    embedding blob not null,    -- float32, unit length
    seconds real not null,      -- speech used
    consistency real not null,  -- mean cosine similarity of its pieces to it
    via text not null,          -- voice, panel or cli
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
        """Control-panel view, without the embedding."""
        return {"name": self.name, "seconds": round(self.seconds, 1), "consistency": round(self.consistency, 2),
                "via": self.via, "created": self.created, "current": self.model == config.SPEAKER_MODEL_ID}


def _db() -> AbstractContextManager[sqlite3.Connection]:
    return database.table("voiceprints", _SCHEMA)


def _row(r: sqlite3.Row) -> Voiceprint:
    return Voiceprint(np.frombuffer(r["embedding"], dtype=np.float32).copy(), r["seconds"], r["consistency"], r["name"],
                      r["model"], r["via"], r["created"])


def every() -> list[Voiceprint]:
    """All voiceprints, owner first, any model."""
    with _db() as db:
        rows = db.execute("select * from voiceprints order by name != ?, created", (OWNER,)).fetchall()
    return [_row(r) for r in rows]


def load(model: str = config.SPEAKER_MODEL_ID) -> list[Voiceprint]:
    """Voiceprints from `model`. On DB error, logs and returns [] (the check then accepts anyone)."""
    try:
        return [v for v in every() if v.model == model]
    except sqlite3.Error as e:
        log.error("can't read the voiceprints from %s: %s", profile.current().db.name, e)
        return []


def save(print_: Voiceprint) -> Voiceprint:
    """Insert or replace by name; return it."""
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
    """Delete by name; False if none existed."""
    with _db() as db:
        return db.execute("delete from voiceprints where name = ?", (name,)).rowcount > 0
