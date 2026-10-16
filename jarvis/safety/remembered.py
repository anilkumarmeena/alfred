"""Remembered approvals: standing yeses for risky shell commands (task host commands and the voice shell).

  session  rest of the asking task or conversation; ends with it, or after SESSION_HOURS
  always   all tasks and the voice shell, any folder, until withdrawn (panel or approval_rule_forget)

A rule matches one risky piece (commands.risky_pieces) by program+subcommand and risk kind, plus either the exact
words (`exact`; pids match any) or any words (`kind`). A command runs only if every risky piece has a rule. Only
REMEMBERABLE kinds qualify. Rules come only from the user's own answers and are built from the command, never a model.
"""

import json
import logging
import re
import time
from pathlib import Path

from jarvis.core import database
from jarvis.safety.commands import Own, names_secret, program, reads_environment, risky_pieces

log = logging.getLogger("jarvis.safety.remembered")

SCOPES = ("session", "always")
BREADTHS = ("exact", "kind")
SESSION_HOURS = 12
# Risk kinds a rule may cover; every other kind always asks
REMEMBERABLE = {"installs software", "stops a process", "changes permissions", "changes the iPhone", "changes a simulator"}
_NUMBERED = {"stops a process"}  # pids vary per run: exact rules match any number
ANY_NUMBER = "#"
_NOT_LITERAL = re.compile(r"[$`*?\[\]{}\\\n]")  # the shell could expand it

_SCHEMA = """create table approval_rules (
    id integer primary key,
    scope text not null,        -- session or always
    session text,               -- task:ID or voice:ID, for session rules
    program text not null,      -- e.g. brew install
    why text not null,          -- risk kind, e.g. installs software
    words text,                 -- JSON exact argv ("#" = any number), or null for any
    example text not null,      -- source command
    via text not null,          -- voice, panel or assistant
    created real not null,
    expires real,               -- session rules lapse
    uses integer not null default 0,
    last_used real
)"""


class NotRememberable(ValueError):
    """The command's risk kind always asks."""


def _db():
    return database.table("approval_rules", _SCHEMA)


# ── What a command offers ───────────────────────────────────────


def _words(argv: list[str], why: str) -> list[str] | None:
    """Words for an exact rule, or None if any could expand at run time."""
    words = [Path(argv[0].lstrip("\\")).name, *argv[1:]]
    if any(_NOT_LITERAL.search(w) for w in words):
        return None
    return [ANY_NUMBER if why in _NUMBERED and w.isdigit() else w for w in words]


def pieces(command: str, own: Own | None = None, cwd: Path | None = None) -> dict:
    """{"pieces": [{program, why, words}], "never": reason it can't be remembered, or ''}.

    Stored with the request by whoever checked it. A safe command has no pieces. A risk found in a script's code is
    never remembered: the script can change.
    """
    found = risky_pieces(command, own, cwd)
    if names_secret(command) or reads_environment(command):
        return {"pieces": [], "never": "anything touching secrets asks every time"}
    never = next((why for argv, why in found if why not in REMEMBERABLE or not argv), "")
    if never:
        return {"pieces": [], "never": f"a command that {never} asks every time"}
    return {"pieces": [{"program": program(argv), "why": why, "words": _words(argv, why)} for argv, why in found],
            "never": ""}


def candidates(offered: list[dict], breadth: str = "exact") -> list[dict]:
    """Rules covering every command in `offered` (pieces() dicts) at `breadth`. Raises NotRememberable."""
    if breadth not in BREADTHS:
        raise ValueError(f"breadth must be one of {', '.join(BREADTHS)}.")
    rules: list[dict] = []
    for o in offered:
        if not o or o.get("never") or not o.get("pieces"):
            raise NotRememberable(o.get("never") if o else "that command can't be remembered")
        for p in o["pieces"]:
            words = p["words"] if breadth == "exact" else None
            if breadth == "exact" and words is None:
                raise NotRememberable(f"“{p['program']}” there has words that can change when it runs, so only "
                                      "any command of that kind can be remembered")
            rule = {"program": p["program"], "why": p["why"], "words": words}
            if rule not in rules:
                rules.append(rule)
    return rules


def fitting(offered: list[dict], breadth: str) -> tuple[list[dict], str] | str:
    """(rules, breadth) for a session/always yes, falling back to 'kind'; or why they can't be remembered."""
    why = ""
    for b in dict.fromkeys([breadth, "kind"]):
        try:
            return candidates(offered, b), b
        except NotRememberable as e:
            why = str(e)
    return why


def offer(offered: list[dict]) -> dict:
    """{"never", "exact", "kind"}: why not, or each breadth's rules in words (`exact` may be '')."""
    try:
        kind = candidates(offered, "kind")
    except NotRememberable as e:
        return {"never": str(e), "exact": "", "kind": ""}
    try:
        exact = covers(candidates(offered, "exact"))
    except NotRememberable:
        exact = ""
    return {"never": "", "exact": exact, "kind": covers(kind)}


# ── Saying what a rule covers ───────────────────────────────────


def _shown(words: list[str]) -> str:
    return " ".join("<any number>" if w == ANY_NUMBER else w for w in words)


def cover(rule: dict) -> str:
    """One rule in words: '“brew install ffmpeg” exactly (installs software)'."""
    if rule["words"] is None:
        return f"any “{rule['program']}” command ({rule['why']})"
    return f"“{_shown(rule['words'])}” exactly ({rule['why']})"


def covers(rules: list[dict]) -> str:
    return "; ".join(cover(r) for r in rules)


def who(rule: dict) -> str:
    """Whose commands a rule covers, and for how long."""
    if rule["scope"] == "always":
        return "for background tasks and JARVIS's own shell, in any folder, until withdrawn"
    if (rule.get("session") or "").startswith("task:"):
        return f"for the rest of task {rule['session'].removeprefix('task:')}, at most {SESSION_HOURS} hours"
    return f"for the rest of the conversation, at most {SESSION_HOURS} hours"


def preview(rules: list[dict], scope: str = "always") -> str:
    """Spoken preview of saving these rules."""
    whom = "background tasks and my own shell, in any folder" if scope == "always" else "the rest of this session"
    return f"From now on, {covers(rules)} would run without asking, for {whom}"


# ── Keeping them ────────────────────────────────────────────────


def _rule(row) -> dict:
    r = dict(row)
    r["words"] = json.loads(r["words"]) if r["words"] is not None else None
    return r | {"covers": cover(r), "who": who(r)}


def save(rules: list[dict], scope: str, example: str, via: str, session: str | None = None) -> list[dict]:
    """Store rules (from candidates()), deduplicated; returns them as stored. Session rules need `session`."""
    if scope not in SCOPES:
        raise ValueError(f"scope must be one of {', '.join(SCOPES)}.")
    if scope == "session" and not session:
        raise ValueError("A session rule needs its session.")
    now, kept = time.time(), []
    expires = now + SESSION_HOURS * 3600 if scope == "session" else None
    with _db() as db:
        for r in rules:
            if r["why"] not in REMEMBERABLE:
                raise NotRememberable(f"a command that {r['why']} asks every time")
            words = json.dumps(r["words"], ensure_ascii=False) if r["words"] is not None else None
            row = db.execute("select * from approval_rules where scope = ? and session is ? and program = ? and why = ?"
                             " and words is ?", (scope, session, r["program"], r["why"], words)).fetchone()
            if row is None:
                cur = db.execute("insert into approval_rules (scope, session, program, why, words, example, via, created,"
                                 " expires) values (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                                 (scope, session, r["program"], r["why"], words, example[:500], via, now, expires))
                row = db.execute("select * from approval_rules where id = ?", (cur.lastrowid,)).fetchone()
                log.warning("remembered (%s, %s, by %s): %s", scope, session or "everywhere", via, cover(_rule(row)))
            kept.append(_rule(row))
    return kept


def _drop_lapsed(db) -> None:
    db.execute("delete from approval_rules where expires is not null and expires < ?", (time.time(),))


def rules(sessions: tuple[str, ...] | list[str] = ()) -> list[dict]:
    """Rules in force: all "always" ones plus these sessions'. Prunes lapsed ones."""
    with _db() as db:
        _drop_lapsed(db)
        marks = ", ".join("?" * len(sessions))
        where = f"scope = 'always' or session in ({marks})" if sessions else "scope = 'always'"
        return [_rule(r) for r in db.execute(f"select * from approval_rules where {where} order by id", tuple(sessions))]


def listed() -> list[dict]:
    """Every rule in force, oldest first, for the panel and approval_rules."""
    with _db() as db:
        _drop_lapsed(db)
        return [_rule(r) for r in db.execute("select * from approval_rules order by id")]


def forget(rule_id: int) -> dict:
    """Delete a rule; returns it. Raises ValueError if unknown."""
    with _db() as db:
        row = db.execute("select * from approval_rules where id = ?", (int(rule_id),)).fetchone()
        if row is None:
            raise ValueError(f"There's no remembered approval {rule_id}.")
        db.execute("delete from approval_rules where id = ?", (int(rule_id),))
    gone = _rule(row)
    log.warning("withdrew remembered approval %s: %s", rule_id, gone["covers"])
    return gone


def end_session(session: str) -> None:
    """Drop a finished conversation's or discarded task's session rules."""
    with _db() as db:
        db.execute("delete from approval_rules where session = ?", (session,))


# ── Matching ────────────────────────────────────────────────────


def _matches(rule: dict, piece: dict) -> bool:
    if (rule["program"], rule["why"]) != (piece["program"], piece["why"]):
        return False
    if rule["words"] is None:
        return True
    return piece["words"] == rule["words"]  # pids are ANY_NUMBER in both


def covering(offered: dict, sessions: tuple[str, ...] | list[str] = ()) -> list[dict] | None:
    """Rules covering every risky piece of a command (pieces()), or None. Records a use on each rule returned."""
    if not offered or offered.get("never") or not offered.get("pieces"):
        return None
    found = rules(sessions)
    used = []
    for piece in offered["pieces"]:
        if (rule := next((r for r in found if _matches(r, piece)), None)) is None:
            return None
        if rule not in used:
            used.append(rule)
    with _db() as db:
        db.executemany("update approval_rules set uses = uses + 1, last_used = ? where id = ?",
                       [(time.time(), r["id"]) for r in used])
    return used


def told(rules_used: list[dict]) -> str:
    """Log text: which rules a command ran under."""
    return "; ".join(f"rule {r['id']}, {r['covers']}" for r in rules_used)
