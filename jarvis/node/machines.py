"""The machines a user has paired, in their profile's database, plus pairing codes and where each kind of job runs.

  nodes          one row per machine: name, key, what it reported about itself, when it was last seen
  node_pairings  one-time codes from Settings › Machines › Add a machine (only their hashes are kept)
  placement      settings key: per job kind, "here", "auto" (let JARVIS choose) or a machine's id

Pairing: the panel makes a code; the machine sends it with its public key (redeem) and gets this computer's public
key back; the panel then shows the machine's fingerprint, and only once the user confirms it there may the machine
connect. The main computer's own key is made on first use and kept in the vault.
"""

import hashlib
import json
import secrets
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field

from jarvis.core import database, vault
from jarvis.node import keys
from jarvis.state import settings

CODE_MINUTES = 10
CODE_LETTERS = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O or 1/I to misread
PENDING, PAIRED = "pending", "paired"
KEY_NAME = "machines.link_key"  # the main computer's private key, in the vault
HERE, AUTO = "here", "auto"
# Job kinds the user places, as Settings shows them. Research and anything needing accounts always run here.
KINDS = {"commands": "Long commands", "tasks": "Claude tasks"}
NAME_MOST = 40

_NODES = """
create table nodes (
    id integer primary key,
    name text not null,
    aliases text,                 -- JSON list of other names it's called
    public_key text not null,     -- Ed25519, base64
    status text not null,         -- pending (until confirmed in the panel) or paired
    host text,                    -- its own hostname, as it reported
    os text,
    cores integer,
    ram_gb real,
    tools text,                   -- JSON list of programs found on it
    max_tasks integer,
    version text,                 -- JARVIS commit it runs
    usage text,                   -- JSON: its own Claude account's usage windows, as last reported
    last_seen real,
    created real not null
)"""
_PAIRINGS = """
create table node_pairings (
    code_hash text primary key,
    created real not null,
    expires real not null,
    used real                     -- when a machine redeemed it
)"""


@dataclass(frozen=True)
class Machine:
    id: int
    name: str
    public_key: str
    status: str
    aliases: list[str] = field(default_factory=list)
    host: str = ""
    os: str = ""
    cores: int = 0
    ram_gb: float = 0.0
    tools: list[str] = field(default_factory=list)
    max_tasks: int = 0
    version: str = ""
    usage: dict = field(default_factory=dict)
    last_seen: float = 0.0
    created: float = 0.0

    @property
    def fingerprint(self) -> str:
        return keys.fingerprint(self.public_key)

    @property
    def paired(self) -> bool:
        return self.status == PAIRED


@contextmanager
def _db() -> Iterator[sqlite3.Connection]:
    with database.table("nodes", _NODES) as db:
        database.migrate(db, "node_pairings", [_PAIRINGS])
        yield db


def _machine(row: sqlite3.Row) -> Machine:
    r = dict(row)
    return Machine(r["id"], r["name"], r["public_key"], r["status"], json.loads(r["aliases"] or "[]"), r["host"] or "",
                   r["os"] or "", r["cores"] or 0, r["ram_gb"] or 0.0, json.loads(r["tools"] or "[]"),
                   r["max_tasks"] or 0, r["version"] or "", json.loads(r["usage"] or "{}"), r["last_seen"] or 0.0,
                   r["created"])


def load() -> list[Machine]:
    with _db() as db:
        return [_machine(r) for r in db.execute("select * from nodes order by id")]


def paired() -> list[Machine]:
    return [m for m in load() if m.paired]


def get(node: int) -> Machine | None:
    with _db() as db:
        row = db.execute("select * from nodes where id = ?", (node,)).fetchone()
    return _machine(row) if row else None


def need(node: int) -> Machine:
    if (found := get(node)) is None:
        raise ValueError(f"There's no machine {node}.")
    return found


def _norm(name: str) -> str:
    return " ".join(name.lower().replace("’", "'").split()).removeprefix("the ")


def find(name: str) -> Machine:
    """A paired machine by name or alias (case and "the" ignored). ValueError naming the ones there are."""
    wanted = _norm(name)
    known = paired()
    for m in known:
        if wanted in (_norm(m.name), *map(_norm, m.aliases)):
            return m
    there = ", ".join(m.name for m in known) or "none yet: pair one in Settings › Machines"
    raise ValueError(f"There's no machine called {name}. The machines are: {there}.")


def public_of(node: int) -> str | None:
    """A machine's public key, confirmed or not (the hub turns unconfirmed ones away after they sign in), else None."""
    found = get(node)
    return found.public_key if found else None


# ── This computer's key ─────────────────────────────────────────


def main_key() -> str:
    """The main computer's private key (PEM), made and kept in the vault on first use. VaultError if it can't be."""
    if vault.has(KEY_NAME):
        return vault.get(KEY_NAME)
    vault.ensure()
    made = keys.new()
    vault.put(KEY_NAME, made)
    return made


def main_public() -> str:
    return keys.public(main_key())


# ── Pairing ─────────────────────────────────────────────────────


def _hash(code: str) -> str:
    return hashlib.sha256("".join(c for c in code.upper() if c.isalnum()).encode()).hexdigest()


def new_code(now: float | None = None) -> dict:
    """A one-time pairing code, valid CODE_MINUTES: {"code": "K7QF-3MZD", "expires": ...}."""
    now = now or time.time()
    raw = "".join(secrets.choice(CODE_LETTERS) for _ in range(8))
    code = f"{raw[:4]}-{raw[4:]}"
    with _db() as db:
        db.execute("delete from node_pairings where expires < ?", (now - 86400,))
        db.execute("insert into node_pairings values (?, ?, ?, null)", (_hash(code), now, now + CODE_MINUTES * 60))
    main_key()  # made now, so the machine gets its public half
    return {"code": code, "expires": now + CODE_MINUTES * 60}


def _clean_name(name: str) -> str:
    name = " ".join(str(name).split())[:NAME_MOST]
    if not name:
        raise ValueError("A machine needs a name.")
    if _norm(name) in (HERE, AUTO, "this computer", "this mac", "main"):
        raise ValueError(f"{name} can't name a machine: it means this computer or a setting.")
    return name


def _unique(db: sqlite3.Connection, name: str, node: int | None = None) -> str:
    """`name`, or `name 2`, `name 3`... if another machine is called that."""
    taken = {_norm(r[0]) for r in db.execute("select name from nodes where id != ?", (node or 0,))}
    found, n = name, 1
    while _norm(found) in taken:
        n += 1
        found = f"{name} {n}"
    return found


def redeem(code: str, public_key: str, facts: dict, now: float | None = None) -> Machine:
    """A machine pairs with `code`: recorded as pending until the user confirms its fingerprint in the panel.

    ValueError for a wrong, used or expired code, or a malformed key."""
    now = now or time.time()
    if not keys.valid_public(public_key):
        raise ValueError("That isn't a machine key.")
    with _db() as db:
        db.execute("begin immediate")
        row = db.execute("select * from node_pairings where code_hash = ?", (_hash(code),)).fetchone()
        if row is None or row["used"] or row["expires"] < now:
            db.execute("rollback")
            raise ValueError("That pairing code is wrong, used or expired. Make a new one in Settings › Machines.")
        db.execute("update node_pairings set used = ? where code_hash = ?", (now, row["code_hash"]))
        name = _unique(db, _clean_name(str(facts.get("name") or facts.get("host") or "Machine")))
        cur = db.execute("insert into nodes (name, public_key, status, created) values (?, ?, ?, ?)",
                         (name, public_key, PENDING, now))
        db.execute("commit")
    saw(cur.lastrowid, facts, seen=False)
    return need(cur.lastrowid)


def confirm(node: int) -> Machine:
    """The user checked the fingerprint: the machine may connect. A kind of job with no placement yet then lets
    JARVIS choose, now there's somewhere else to run it."""
    found = need(node)
    with _db() as db:
        db.execute("update nodes set status = ? where id = ?", (PAIRED, node))
    stored = _stored_placement()
    if missing := [k for k in KINDS if k not in stored]:
        settings.put("placement", json.dumps(stored | dict.fromkeys(missing, AUTO)))
    return need(found.id)


def rename(node: int, name: str, aliases: list[str] | None = None) -> Machine:
    need(node)
    with _db() as db:
        fresh = _clean_name(name)
        if _unique(db, fresh, node) != fresh:
            raise ValueError(f"Another machine is called {fresh}.")
        tidy = [a for a in dict.fromkeys(" ".join(str(a).split())[:NAME_MOST] for a in aliases or []) if a]
        db.execute("update nodes set name = ?, aliases = ? where id = ?", (fresh, json.dumps(tidy), node))
    return need(node)


def remove(node: int) -> Machine:
    """Forget a machine: it can't connect again without pairing afresh. Placement naming it goes back to auto (or
    here, with no machine left)."""
    gone = need(node)
    with _db() as db:
        db.execute("delete from nodes where id = ?", (node,))
    stored = _stored_placement()
    fallback = AUTO if paired() else HERE
    if changed := {k: fallback for k, v in stored.items() if v == str(node) or (v == AUTO and fallback == HERE)}:
        settings.put("placement", json.dumps(stored | changed))
    return gone


_FACTS = {"host": str, "os": str, "cores": int, "ram_gb": float, "max_tasks": int, "version": str}


def saw(node: int, facts: dict, seen: bool = True, now: float | None = None) -> None:
    """Record what a machine reported about itself (from its hello or an online check), and when."""
    fields: dict = {}
    for key, kind in _FACTS.items():
        if facts.get(key) is not None:
            try:
                fields[key] = kind(facts[key])
            except (TypeError, ValueError):
                continue
    if isinstance(facts.get("tools"), list):
        fields["tools"] = json.dumps([str(t)[:40] for t in facts["tools"]][:50])
    if isinstance(facts.get("usage"), dict):
        fields["usage"] = json.dumps(facts["usage"])
    if seen:
        fields["last_seen"] = now or time.time()
    if not fields:
        return
    with _db() as db:
        columns = ", ".join(f"{k} = ?" for k in fields)  # keys come from _FACTS, never input
        db.execute(f"update nodes set {columns} where id = ?", (*fields.values(), node))


# ── Where jobs run ──────────────────────────────────────────────


def _stored_placement() -> dict[str, str]:
    try:
        found = json.loads(settings.get("placement") or "{}")
    except ValueError:
        return {}
    return {k: str(v) for k, v in found.items() if k in KINDS} if isinstance(found, dict) else {}


def placement() -> dict[str, str]:
    """Per job kind: HERE, AUTO, or a paired machine's id (as text). A machine since removed reads as AUTO."""
    stored, there = _stored_placement(), {str(m.id) for m in paired()}
    out = {}
    for kind in KINDS:
        value = stored.get(kind, HERE)
        out[kind] = value if value in (HERE, AUTO) or value in there else (AUTO if there else HERE)
    return out


def set_placement(kind: str, value: str) -> dict[str, str]:
    """Set where `kind` runs: HERE, AUTO or a paired machine's id. ValueError otherwise."""
    if kind not in KINDS:
        raise ValueError(f"Job kinds are {', '.join(KINDS)}.")
    value = str(value).strip()
    if value not in (HERE, AUTO) and value not in {str(m.id) for m in paired()}:
        raise ValueError("Choose this computer, let JARVIS choose, or a paired machine.")
    settings.put("placement", json.dumps(_stored_placement() | {kind: value}))
    return placement()


def where(value: str) -> str:
    """A placement as said: 'this computer', 'let JARVIS choose', or the machine's name."""
    if value == HERE:
        return "this computer"
    if value == AUTO:
        return "let JARVIS choose"
    found = get(int(value)) if value.isdigit() else None
    return found.name if found else "this computer"
