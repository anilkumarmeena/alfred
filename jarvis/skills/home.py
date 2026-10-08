"""The skills folder (data/skills) and what's known about each skill in it.

A skill is a folder named after it, holding a SKILL.md: YAML frontmatter with its name and a description of when to use
it, then the instructions, and maybe scripts and other files beside it. That's Claude Code's own format, which the
Claude Agent SDK loads from plugins. So each session gets a small plugin of its own (`plugin`): a plugin.json and links
to the skills that are switched on, nothing else. The CLI lists their names and descriptions, and the model loads one
with the Skill tool only when a request calls for it. They go under the plugin's name: jarvis-skills:standup-notes.
The voice brain (jarvis.brain.cli) and background tasks (jarvis.tasks.worker) each make one as they start, so a change
applies from the next conversation or run.

The `skills` table of data/tasks.db keeps, for each one: where it came from (installed from a link, written together
by voice, learned from conversations, or added to the folder by hand), whether it's switched on, and for one with
scripts, the review of them and the digest of the files the user approved. The folder holds the content; a folder
put there by hand is listed, switched off, and one taken away drops out of the table.

The walls, none of which a skill's own text can lower:
- Frontmatter that would change permissions or run something (allowed-tools, hooks, shell, model, context, agent and
  the rest) is taken out when a skill is saved or installed (`sanitize`), and a skill that has it again is skipped as
  sessions start: allowed-tools become allow rules, which would let calls past the brain's gate.
- Sessions set disableAllHooks and disableSkillShellExecution (`FLAGS`), so no hook or !`command` in a skill runs.
- A skill with scripts or other executable files is used only while its files are the ones the user approved after
  hearing a review of them (jarvis.skills.review): changed since, it's skipped until approved again.
- Links in a skill's folder are refused, so it can't point at secrets or anywhere else on the Mac.
- Scripts run like any other command: in the voice shell's sandbox, risky ones only after a yes; in a task's sandbox,
  through its gate. Nothing here reads a secret, and the CLI's deny rules still hide them.
"""

import hashlib
import json
import logging
import os
import re
import shutil
import sqlite3
import stat
import time
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import yaml

from jarvis.core import database, profile
from jarvis.core.files import write_atomic
from jarvis.safety.host import alive

log = logging.getLogger("jarvis.skills")

PLUGIN = "jarvis-skills"  # the plugin each session gets, which its skills are named under
SKILL_FILE = "SKILL.md"
INSTALLED, WRITTEN, LEARNED, MANUAL = "installed", "written", "learned", "manual"
SOURCES = {INSTALLED: "installed", WRITTEN: "written together", LEARNED: "learned", MANUAL: "added by hand"}
NAME = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?")  # Claude Code's rule: lowercase, digits and hyphens, 64 at most
DESCRIPTION_MAX = 1024
INSTRUCTIONS_MAX = 20_000
# Frontmatter a skill keeps. Everything else is taken out: allowed-tools, hooks, shell, model, effort, context, agent,
# paths, mcpServers and the like change permissions, run commands or start other things.
KEPT = ("name", "description", "when_to_use", "argument-hint", "arguments", "version", "license", "compatibility",
        "metadata", "created_by", "improved_by", "user-invocable", "disable-model-invocation")
# Settings every session with skills gets: no hooks from them, and no !`command` in a SKILL.md run as it loads
FLAGS = {"disableAllHooks": True, "disableSkillShellExecution": True}

SCRIPTS = {".sh", ".bash", ".zsh", ".fish", ".command", ".tool", ".py", ".pyw", ".js", ".mjs", ".cjs", ".ts", ".mts",
           ".cts", ".jsx", ".tsx", ".rb", ".pl", ".pm", ".php", ".lua", ".r", ".ps1", ".psm1", ".bat", ".cmd", ".vbs",
           ".applescript", ".scpt", ".jxa", ".swift", ".go", ".rs", ".c", ".cc", ".cpp", ".java", ".kt", ".jar", ".exe",
           ".bin", ".so", ".dylib", ".dll", ".wasm", ".app", ".pkg", ".dmg", ".deb", ".rpm", ".whl", ".egg"}
SCRIPT_NAMES = {"Makefile", "makefile", "GNUmakefile", "Dockerfile", "Justfile", "justfile", "Rakefile", "package.json",
                "pyproject.toml", "setup.py", "requirements.txt"}
# Binary files that are only data (images, fonts, documents): anything else binary counts as a program
MEDIA = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico", ".tif", ".tiff", ".heic", ".pdf", ".ttf", ".otf",
         ".woff", ".woff2", ".mp3", ".wav", ".m4a", ".mp4", ".mov", ".docx", ".xlsx", ".pptx", ".odt", ".zip", ".gz"}

_SCHEMA = """
create table skills (
    name text primary key,      -- its folder in data/skills, and its name in SKILL.md
    source text not null,       -- installed, written (together, by voice), learned (the learning review), manual
    origin text,                -- for an installed one, the link it came from
    enabled integer not null default 0,
    scripts text,               -- JSON list of its scripts and other executable files, as last looked at
    digest text,                -- of its files other than SKILL.md, as last looked at
    approved text,              -- the digest the user approved its scripts at, after hearing the review
    review text,                -- JSON: the latest review of its scripts (jarvis.skills.review)
    created real not null,
    updated real not null
)"""


class NeedsApproval(ValueError):
    """The skill has scripts the user hasn't approved, as they are now."""


def _db() -> AbstractContextManager[sqlite3.Connection]:
    return database.table("skills", _SCHEMA)


def home() -> Path:
    """The profile's skills folder."""
    (found := profile.current().skills).mkdir(parents=True, exist_ok=True)
    return found


def folder(name: str) -> Path:
    return profile.current().skills / name


# ── SKILL.md ────────────────────────────────────────────────────

_FRONTMATTER = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)(.*)", re.DOTALL)
INLINE = re.compile(r"!`[^`\n]+`")  # a !`command` line, which Claude Code would run as the skill loads


def split(text: str) -> tuple[dict, str]:
    """A SKILL.md's frontmatter and body. ValueError if it has no frontmatter, or it isn't YAML."""
    m = _FRONTMATTER.match(text.lstrip("﻿"))
    if not m:
        raise ValueError("its SKILL.md has no frontmatter (a --- block with its name and description)")
    try:
        meta = yaml.safe_load(m.group(1))
    except yaml.YAMLError:
        raise ValueError("its SKILL.md's frontmatter isn't valid YAML") from None
    if not isinstance(meta, dict):
        raise ValueError("its SKILL.md's frontmatter isn't a set of fields")
    return meta, m.group(2).strip()


def compose(meta: dict, body: str) -> str:
    front = yaml.safe_dump(meta, sort_keys=False, allow_unicode=True, width=10_000).strip()
    return f"---\n{front}\n---\n\n{body.strip()}\n"


def sanitize(text: str, name: str | None = None) -> tuple[str, list[str]]:
    """The SKILL.md with the frontmatter that could change permissions or run something taken out, and the name set to
    `name` if given; and the fields taken out. Unchanged text when there's nothing to take out."""
    meta, body = split(text)
    dropped = [str(k) for k in meta if k not in KEPT]
    if not dropped and (name is None or meta.get("name") == name):
        return text, []
    kept = {k: v for k, v in meta.items() if k in KEPT}
    if name is not None:
        kept = {"name": name} | kept
    return compose(kept, body), dropped


def one_line(text: str) -> str:
    return " ".join(str(text or "").split())


def render(name: str, description: str, instructions: str, keep: dict | None = None) -> str:
    """A SKILL.md for a skill written by voice or learned: its name, description and instructions, keeping any other
    allowed frontmatter of the version it replaces (`keep`)."""
    meta = {"name": name, "description": one_line(description)}
    meta |= {k: v for k, v in (keep or {}).items() if k in KEPT and k not in meta}
    return compose(meta, instructions)


def check_text(name: str, description: str, instructions: str) -> None:
    """ValueError, saying what to fix, for a skill that can't be saved like this."""
    if not NAME.fullmatch(name):
        raise ValueError(f"'{name}' can't be a skill's name: use lowercase letters, digits and hyphens, at most 64, "
                         "like standup-notes.")
    if not (description := one_line(description)):
        raise ValueError("Give the skill a description: what it does and when to use it.")
    if len(description) > DESCRIPTION_MAX:
        raise ValueError(f"The description is {len(description)} characters; keep it under {DESCRIPTION_MAX}.")
    if not instructions.strip():
        raise ValueError("The skill has no instructions.")
    if len(instructions) > INSTRUCTIONS_MAX:
        raise ValueError(f"The instructions are {len(instructions):,} characters; keep them under {INSTRUCTIONS_MAX:,}.")
    if INLINE.search(instructions):
        raise ValueError("Instructions can't hold !`command` lines that run as the skill loads; say to run the "
                         "command instead.")


def slug(text: str) -> str:
    """A spoken name as a skill's name: 'Standup Notes' -> standup-notes."""
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", str(text).lower())).strip("-")[:64].strip("-")


def as_name(text: str) -> str:
    """A skill's name as given, if it is one, else made into one (`slug`)."""
    return text if NAME.fullmatch(text) else slug(text)


# ── What a skill's folder holds ─────────────────────────────────


def files(where: Path) -> list[str]:
    """Every file in the folder, as relative paths, sorted. Links are listed too, to be refused."""
    found = []
    for root, dirs, names in os.walk(where):
        dirs.sort()
        for n in [*names, *(d for d in dirs if (Path(root) / d).is_symlink())]:
            found.append((Path(root) / n).relative_to(where).as_posix())
    return sorted(found)


def links(where: Path) -> list[str]:
    return [f for f in files(where) if (where / f).is_symlink()]


def is_script(path: Path) -> bool:
    """Whether a file is a script or another program: marked executable, named like one, starting with #!, or binary
    but not a picture, font or document."""
    try:
        mode = path.lstat().st_mode
    except OSError:
        return False
    if not stat.S_ISREG(mode):
        return False
    if mode & 0o111 or path.suffix.lower() in SCRIPTS or path.name in SCRIPT_NAMES:
        return True
    try:
        with open(path, "rb") as f:
            start = f.read(8192)
    except OSError:
        return False
    return start.startswith(b"#!") or (path.suffix.lower() not in MEDIA and b"\0" in start)


def scripts(where: Path) -> list[str]:
    return [f for f in files(where) if f != SKILL_FILE and is_script(where / f)]


def digest(where: Path) -> str:
    """A digest of the folder's files other than SKILL.md: what the user approves when they approve its scripts. The
    instructions can change (by voice, with a yes) without the scripts needing approval again."""
    h = hashlib.sha256()
    for rel in files(where):
        if rel == SKILL_FILE:
            continue
        path = where / rel
        h.update(rel.encode() + b"\0")
        h.update(b"link:" + os.readlink(path).encode() if path.is_symlink() else path.read_bytes())
        h.update(b"\0")
    return h.hexdigest()


def problems(where: Path, name: str | None = None) -> list[str]:
    """Why a skill's folder can't be used, if anything."""
    skill = where / SKILL_FILE
    if not skill.is_file() or skill.is_symlink():
        return ["it has no SKILL.md"]
    try:
        meta, body = split(skill.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        return ["its SKILL.md can't be read as text"]
    except ValueError as e:
        return [str(e)]
    found = []
    named = str(meta.get("name") or "").strip()
    if not NAME.fullmatch(named):
        found.append("its name isn't lowercase letters, digits and hyphens")
    elif name and named != name:
        found.append(f"its SKILL.md names it {named}, not {name}")
    if not one_line(meta.get("description")):
        found.append("it has no description")
    elif len(one_line(meta.get("description"))) > DESCRIPTION_MAX:
        found.append("its description is too long")
    if not body:
        found.append("it has no instructions")
    if extra := [str(k) for k in meta if k not in KEPT]:
        found.append(f"its frontmatter has {', '.join(extra)}, which JARVIS doesn't allow")
    if linked := links(where):
        found.append(f"it holds links ({', '.join(linked[:3])}), which could point anywhere on the Mac")
    return found


def skill_md(where: Path) -> str:
    """The folder's SKILL.md as it is now: "" if there's none."""
    path = where / SKILL_FILE
    return path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""


def _meta(where: Path) -> dict:
    """The frontmatter of the folder's SKILL.md: empty if it can't be read."""
    try:
        return split((where / SKILL_FILE).read_text(encoding="utf-8"))[0]
    except (OSError, UnicodeDecodeError, ValueError):
        return {}


def description_of(where: Path) -> str:
    return one_line(_meta(where).get("description"))


# ── The table ───────────────────────────────────────────────────


@dataclass
class Skill:
    name: str
    description: str
    source: str
    origin: str
    enabled: bool
    scripts: list[str]
    digest: str
    approved: str
    review: dict | None
    created: float
    updated: float
    problems: list[str] = field(default_factory=list)

    @property
    def folder(self) -> Path:
        return folder(self.name)

    @property
    def needs_approval(self) -> bool:
        """It has scripts the user hasn't approved as they are now."""
        return bool(self.scripts) and self.approved != self.digest

    @property
    def usable(self) -> bool:
        return self.enabled and not self.problems and not self.needs_approval

    @property
    def state(self) -> str:
        if self.problems:
            return "broken"
        if self.needs_approval:
            return "changed since approved" if self.approved else "needs approval"
        return "on" if self.enabled else "off"

    def brief(self) -> dict:
        """As the voice tools and the panel list it."""
        out = {"name": self.name, "description": self.description, "source": SOURCES.get(self.source, self.source),
               "state": self.state, "added": f"{datetime.fromtimestamp(self.created):%Y-%m-%d}"}
        if self.updated - self.created > 60:
            out["changed"] = f"{datetime.fromtimestamp(self.updated):%Y-%m-%d}"
        if self.scripts:
            out["scripts"] = self.scripts
        if self.origin:
            out["from"] = self.origin
        if self.problems:
            out["problems"] = self.problems
        return out


def _row(r: sqlite3.Row, where: Path) -> Skill:
    return Skill(r["name"], description_of(where), r["source"], r["origin"] or "", bool(r["enabled"]),
                 json.loads(r["scripts"] or "[]"), r["digest"] or "", r["approved"] or "",
                 json.loads(r["review"]) if r["review"] else None, r["created"], r["updated"], problems(where, r["name"]))


def all_skills() -> list[Skill]:
    """Every skill in the folder, by name, with what's on disk looked at afresh: a folder with no row yet (put there by
    hand) gets one, switched off; a row whose folder has gone is dropped; scripts and digests are brought up to date."""
    base = home()
    on_disk = {p.name: p for p in base.iterdir() if p.is_dir() and not p.name.startswith(".") and not p.is_symlink()}
    found: list[Skill] = []
    now = time.time()
    with _db() as db:
        db.execute("begin immediate")
        rows = {r["name"]: r for r in db.execute("select * from skills")}
        for gone in set(rows) - set(on_disk):
            db.execute("delete from skills where name = ?", (gone,))
        for name, where in sorted(on_disk.items()):
            listed, made = json.dumps(scripts(where)), digest(where)
            if (r := rows.get(name)) is None:
                db.execute("insert into skills (name, source, scripts, digest, created, updated) values (?, ?, ?, ?, ?, ?)",
                           (name, MANUAL, listed, made, now, now))
            elif (r["scripts"], r["digest"]) != (listed, made):
                db.execute("update skills set scripts = ?, digest = ? where name = ?", (listed, made, name))
        db.execute("commit")
        found = [_row(r, on_disk[r["name"]]) for r in db.execute("select * from skills order by name")
                 if r["name"] in on_disk]  # not one another process added meanwhile
    return found


def get(name: str) -> Skill:
    """The skill by its exact name, else by its spoken one ('standup notes'), else the one whose name contains it.
    ValueError, listing them, if none or several match."""
    listed = all_skills()
    by_name = {s.name: s for s in listed}
    wanted = slug(name)
    if found := by_name.get(name.strip()) or by_name.get(wanted):
        return found
    loose = [s for s in listed if wanted and wanted in s.name]
    if len(loose) == 1:
        return loose[0]
    names = ", ".join(by_name) or "none yet"
    raise ValueError(f"{'Several skills match' if loose else 'There is no skill called'} '{name}'. The skills: {names}.")


def _update(name: str, **fields) -> None:
    sets = ", ".join(f"{k} = ?" for k in fields)
    with _db() as db:
        if db.execute(f"update skills set {sets}, updated = ? where name = ?", (*fields.values(), time.time(), name)).rowcount != 1:
            raise ValueError(f"There is no skill called {name}; it may have been deleted.")


def refuse_broken(skill: Skill) -> Skill:
    """The skill, unless it's broken: then ValueError, saying why it can't be switched on."""
    if skill.problems:
        raise ValueError(f"The skill {skill.name} can't be switched on: {'; '.join(skill.problems)}.")
    return skill


def switch(name: str, on: bool) -> Skill:
    """Switch a skill on or off. On is refused for a broken skill, and for one whose scripts aren't approved as they
    are now (NeedsApproval): that takes a review and the user's yes (approve)."""
    skill = get(name)
    if on and refuse_broken(skill).needs_approval:
        raise NeedsApproval(f"The skill {skill.name} has scripts ({', '.join(skill.scripts[:5])}) that need a review and "
                            f"{'approving again, since they changed' if skill.approved else 'approving'} before it can "
                            "be switched on.")
    _update(skill.name, enabled=int(on))
    return get(skill.name)


def set_review(name: str, review: dict) -> Skill:
    _update(name, review=json.dumps(review))
    return get(name)


def approve(name: str, reviewed: str) -> Skill:
    """The user approved the skill's scripts as the review found them (`reviewed`, that review's digest): switched on.
    Refused if the files have changed since that review."""
    skill = refuse_broken(get(name))
    if reviewed != skill.digest:
        raise ValueError(f"The skill {skill.name}'s files have changed since they were reviewed; review them again.")
    _update(skill.name, approved=reviewed, enabled=1)
    return get(skill.name)


def delete(name: str) -> str:
    """Delete a skill: its folder and its row."""
    skill = get(name)
    where = skill.folder
    if where.is_dir() and not where.is_symlink():
        shutil.rmtree(where)
    with _db() as db:
        db.execute("delete from skills where name = ?", (skill.name,))
    return skill.name


def save(name: str, description: str, instructions: str, source: str = WRITTEN, enable: bool = True) -> tuple[Skill, bool]:
    """Write a skill's SKILL.md: a new skill (switched on unless `enable` is false), or a new version of one, which
    keeps its source, its other files and whether it's on. Returns it, and whether it's new."""
    check_text(name, description, instructions)
    where, now = home() / name, time.time()
    new = not where.exists()
    keep = {} if new else _meta(where)
    where.mkdir(exist_ok=True)
    write_atomic(where / SKILL_FILE, render(name, description, instructions, keep), mode=0o644)
    with _db() as db:
        if not new and db.execute("select 1 from skills where name = ?", (name,)).fetchone():
            db.execute("update skills set updated = ? where name = ?", (now, name))
        else:  # a row left from a folder taken away by hand is replaced
            db.execute("insert or replace into skills (name, source, enabled, scripts, digest, created, updated) "
                       "values (?, ?, ?, ?, ?, ?, ?)", (name, source, int(enable), json.dumps(scripts(where)),
                                                        digest(where), now, now))
    return get(name), new


def install(staged: Path, name: str, origin: str) -> Skill:
    """Move a fetched skill's folder (jarvis.skills.install) into the skills folder as `name`, replacing one of that
    name. Switched on only if it holds no scripts; one with scripts waits for a review and the user's approval."""
    if problem := problems(staged, name):
        raise ValueError(f"It can't be installed: {'; '.join(problem)}.")
    where, now = home() / name, time.time()
    with _db() as db:
        old = db.execute("select created from skills where name = ?", (name,)).fetchone()
    trash = profile.current().skills / f".trash-{name}-{os.getpid()}-{int(now * 1000)}"
    if where.exists():
        where.rename(trash)
    try:
        shutil.move(str(staged), str(where))
    except BaseException:
        if trash.exists():
            trash.rename(where)
        raise
    shutil.rmtree(trash, ignore_errors=True)
    listed = scripts(where)
    with _db() as db:
        db.execute("insert or replace into skills (name, source, origin, enabled, scripts, digest, approved, review, "
                   "created, updated) values (?, ?, ?, ?, ?, ?, null, null, ?, ?)",
                   (name, INSTALLED, origin, int(not listed), json.dumps(listed), digest(where),
                    old["created"] if old else now, now))
    return get(name)


# ── Sessions ────────────────────────────────────────────────────


def usable() -> list[Skill]:
    """The skills a session gets: switched on, not broken, and scripts (if any) approved as they are. The rest that
    are switched on are logged with why they're left out."""
    out = []
    for s in all_skills():
        if s.usable:
            out.append(s)
        elif s.enabled:
            log.warning("skill %s left out: %s", s.name, "; ".join(s.problems) or s.state)
    return out


def qualified(skill: Skill) -> str:
    return f"{PLUGIN}:{skill.name}"


def _clear(where: Path) -> None:
    if where.is_symlink() or where.is_file():
        where.unlink()
    elif where.exists():
        shutil.rmtree(where)


def plugin(where: Path) -> list[Skill]:
    """Make the session's plugin at `where`: a plugin.json and links to the usable skills, nothing else (no hooks,
    agents, commands or MCP servers, which a plugin could hold). Returns those skills; none, and no plugin, if there
    are none. Their names for the SDK's `skills` option are `qualified`."""
    found = usable()
    _clear(where)
    if not found:
        return []
    (where / ".claude-plugin").mkdir(parents=True)
    (where / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": PLUGIN, "description": "JARVIS's skills that are switched on"}))
    (where / "skills").mkdir()
    for s in found:
        (where / "skills" / s.name).symlink_to(s.folder, target_is_directory=True)
    return found


def voice_plugin() -> Path:
    """Where this voice process's plugin goes, with those of voice processes that have ended cleared away."""
    sessions = home() / ".sessions"
    sessions.mkdir(exist_ok=True)
    for old in sessions.glob("voice-*"):
        pid = old.name.removeprefix("voice-")
        if pid.isdigit() and int(pid) != os.getpid() and not alive(int(pid)):
            _clear(old)
    return sessions / f"voice-{os.getpid()}"


def listing(found: list[Skill]) -> str:
    """The skills a session has, a line each, for its prompt."""
    return "\n".join(f"- {qualified(s)}: {s.description}" for s in found)
