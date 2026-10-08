"""Remembered approvals: standing answers to risky shell commands, so the user needn't say yes to the same thing every
time. They cover background tasks' host commands (jarvis.tasks.worker) and the voice shell's own (jarvis.safety.shell).

A yes to a risky command can go just this once (as before), for this session, or always:

  session  for the rest of the task that asked (its Claude session, through every run of it), or of the conversation
           when the voice shell asked. Gone when the task is discarded or the conversation ends, and after
           SESSION_HOURS whatever happens.
  always   for background tasks and the voice shell alike, in any folder, until the user withdraws it: in the control
           panel's Settings, or by voice (approval_rules, approval_rule_forget).

A rule covers one kind of risky piece of a command line (commands.risky_pieces), matched on the program and its
subcommand ('brew install') and the kind of risk, and either the exact words that followed (`exact`: options and
arguments as they were, a process number standing for any) or any at all (`kind`). A command runs under rules only if
every risky piece in it has one; anything else in it still asks. What a rule would cover is always shown before an
"always" is saved: read out, with the question to save it, or shown in the panel beside the button.

Some commands are never remembered, whatever the answer, and ask every time: deleting files (a task deletes in its own
folders without asking already), git that discards work, pushing, sending data off the Mac, changing system settings,
code the check can't see into, anything touching secrets or credentials. Only REMEMBERABLE kinds can be.

Only the user's own answers make a rule: their words straight after JARVIS's question, the panel's buttons, or their
explicit words to the brain (task_approve). Nothing a task, an email or a web page says can, and nothing here takes
a rule's text from a model: each is built from the command itself, by the process that checked it.
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
# The kinds of risk a rule can cover. Every other kind (deleting, discarding work, pushing, sending data off the Mac,
# changing the system, running as root, code it can't see, credentials, ...) asks every time.
REMEMBERABLE = {"installs software", "stops a process", "changes permissions", "changes the iPhone", "changes a simulator"}
_NUMBERED = {"stops a process"}  # process numbers change from run to run: an exact rule takes any number there
ANY_NUMBER = "#"
_NOT_LITERAL = re.compile(r"[$`*?\[\]{}\\\n]")  # a word the shell could make into something else

_SCHEMA = """create table approval_rules (
    id integer primary key,
    scope text not null,        -- session or always
    session text,               -- a session rule's: task:ID, or voice:ID for a conversation
    program text not null,      -- the program and its subcommand: brew install
    why text not null,          -- the kind of risk it covers: installs software
    words text,                 -- JSON list: the exact command it covers ("#" any number), or null for any words
    example text not null,      -- the command it was made from
    via text not null,          -- how the user said so: voice, panel, assistant
    created real not null,
    expires real,               -- session rules lapse
    uses integer not null default 0,
    last_used real
)"""


class NotRememberable(ValueError):
    """A command whose kind always asks."""


def _db():
    return database.table("approval_rules", _SCHEMA)


# ── What a command offers ───────────────────────────────────────


def _words(argv: list[str], why: str) -> list[str] | None:
    """A risky piece's words as an exact rule keeps them, or None if one could be something else when it runs."""
    words = [Path(argv[0].lstrip("\\")).name, *argv[1:]]
    if any(_NOT_LITERAL.search(w) for w in words):
        return None
    return [ANY_NUMBER if why in _NUMBERED and w.isdigit() else w for w in words]


def pieces(command: str, own: Own | None = None) -> dict:
    """What a command offers to be remembered, checked by whoever decides it (a task's gate, the voice shell) and
    kept with its request: each risky piece's program, kind and exact words, and `never`, why it can't be remembered
    at all ('' if it can). A command that isn't risky has no pieces."""
    found = risky_pieces(command, own)
    if names_secret(command) or reads_environment(command):
        return {"pieces": [], "never": "anything touching secrets asks every time"}
    never = next((why for argv, why in found if why not in REMEMBERABLE or not argv), "")
    if never:
        return {"pieces": [], "never": f"a command that {never} asks every time"}
    return {"pieces": [{"program": program(argv), "why": why, "words": _words(argv, why)} for argv, why in found],
            "never": ""}


def candidates(offered: list[dict], breadth: str = "exact") -> list[dict]:
    """The rules that would cover every command in `offered` (each one's pieces()), at that breadth. Raises
    NotRememberable, saying why, if any of them can't be remembered."""
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
    """For a yes that said "session" or "always": the rules that would remember these commands (each one's pieces()),
    and their breadth (any command of the kind, when the exact words can't be pinned down); or why they can't be
    remembered."""
    why = ""
    for b in dict.fromkeys([breadth, "kind"]):
        try:
            return candidates(offered, b), b
        except NotRememberable as e:
            why = str(e)
    return why


def offer(offered: list[dict]) -> dict:
    """What the user can be offered for a request's commands: why they can't be remembered (`never`), else each
    breadth's rules as said (`exact` empty when the words can't be pinned down)."""
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
    """What one rule lets through, in words: '“brew install ffmpeg” exactly (installs software)'."""
    if rule["words"] is None:
        return f"any “{rule['program']}” command ({rule['why']})"
    return f"“{_shown(rule['words'])}” exactly ({rule['why']})"


def covers(rules: list[dict]) -> str:
    return "; ".join(cover(r) for r in rules)


def who(rule: dict) -> str:
    """Whose commands and for how long."""
    if rule["scope"] == "always":
        return "for background tasks and JARVIS's own shell, in any folder, until withdrawn"
    if (rule.get("session") or "").startswith("task:"):
        return f"for the rest of task {rule['session'].removeprefix('task:')}, at most {SESSION_HOURS} hours"
    return f"for the rest of the conversation, at most {SESSION_HOURS} hours"


def preview(rules: list[dict], scope: str = "always") -> str:
    """What saving these rules would do, said before the user's yes."""
    whom = "background tasks and my own shell, in any folder" if scope == "always" else "the rest of this session"
    return f"From now on, {covers(rules)} would run without asking, for {whom}"


# ── Keeping them ────────────────────────────────────────────────


def _rule(row) -> dict:
    r = dict(row)
    r["words"] = json.loads(r["words"]) if r["words"] is not None else None
    return r | {"covers": cover(r), "who": who(r)}


def save(rules: list[dict], scope: str, example: str, via: str, session: str | None = None) -> list[dict]:
    """Keep rules (from candidates()), unless an identical one is kept already; returns them as kept. A session rule
    names its session: task:ID or voice:ID."""
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
    """The rules in force for these sessions: every "always" one, and those sessions' own. Lapsed ones are dropped."""
    with _db() as db:
        _drop_lapsed(db)
        marks = ", ".join("?" * len(sessions))
        where = f"scope = 'always' or session in ({marks})" if sessions else "scope = 'always'"
        return [_rule(r) for r in db.execute(f"select * from approval_rules where {where} order by id", tuple(sessions))]


def listed() -> list[dict]:
    """Every rule in force, sessions' too, oldest first: what the control panel and approval_rules show."""
    with _db() as db:
        _drop_lapsed(db)
        return [_rule(r) for r in db.execute("select * from approval_rules order by id")]


def forget(rule_id: int) -> dict:
    """Withdraw a rule; returns it as it was. Raises ValueError if there's none by that number."""
    with _db() as db:
        row = db.execute("select * from approval_rules where id = ?", (int(rule_id),)).fetchone()
        if row is None:
            raise ValueError(f"There's no remembered approval {rule_id}.")
        db.execute("delete from approval_rules where id = ?", (int(rule_id),))
    gone = _rule(row)
    log.warning("withdrew remembered approval %s: %s", rule_id, gone["covers"])
    return gone


def end_session(session: str) -> None:
    """A conversation is over, or a task discarded: its session's rules go."""
    with _db() as db:
        db.execute("delete from approval_rules where session = ?", (session,))


# ── Matching ────────────────────────────────────────────────────


def _matches(rule: dict, piece: dict) -> bool:
    if (rule["program"], rule["why"]) != (piece["program"], piece["why"]):
        return False
    if rule["words"] is None:
        return True
    return piece["words"] == rule["words"]  # a process number is ANY_NUMBER in both


def covering(offered: dict, sessions: tuple[str, ...] | list[str] = ()) -> list[dict] | None:
    """The rules that let a command (its pieces()) run without asking: one for each of its risky pieces, from those
    in force for these sessions. None if it can't run under rules: a piece has none, or its kind always asks. Each
    rule used counts the use."""
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
    """For a log: which rules a command ran under."""
    return "; ".join(f"rule {r['id']}, {r['covers']}" for r in rules_used)
