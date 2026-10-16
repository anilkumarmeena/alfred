"""Skills folder (data/skills) and the `skills` table that tracks each skill.

A skill is a folder with a SKILL.md (YAML frontmatter: name, description; then instructions) plus optional files, in
Claude Code's format. Each session gets its own minimal plugin (`plugin`: plugin.json plus links to enabled skills),
so skills appear as jarvis-skills:<name> and load on demand. Changes apply from the next conversation or task run.

The table records source (installed, written, learned, manual), enabled, and for skills with scripts the review and
the approved digest. Folders added by hand appear disabled; removed folders drop out.

Safety (a skill's own text can't lower any of these):
- Frontmatter outside KEPT (allowed-tools, hooks, model, ...) is stripped on save/install (`sanitize`); a skill that
  regains it is skipped. allowed-tools would bypass the brain's gate.
- Sessions set FLAGS: no hooks, no !`command` execution.
- Skills with scripts are used only while their files match the digest the user approved after a review.
- Symlinks inside a skill folder are refused.
- Scripts run like any other command, inside the same sandbox and approval gates.
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

PLUGIN = "jarvis-skills"  # per-session plugin name; skills are <PLUGIN>:<name>
SKILL_FILE = "SKILL.md"
INSTALLED, WRITTEN, LEARNED, MANUAL = "installed", "written", "learned", "manual"
SOURCES = {INSTALLED: "installed", WRITTEN: "written together", LEARNED: "learned", MANUAL: "added by hand"}
NAME = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?")  # Claude Code's rule: [a-z0-9-], max 64
DESCRIPTION_MAX = 1024
INSTRUCTIONS_MAX = 20_000
# Allowed frontmatter. Anything else (allowed-tools, hooks, shell, model, agent, mcpServers, ...) can change
# permissions or run things, so it is stripped.
KEPT = ("name", "description", "when_to_use", "argument-hint", "arguments", "version", "license", "compatibility",
        "metadata", "created_by", "improved_by", "user-invocable", "disable-model-invocation")
# Session settings: no skill hooks, no !`command` execution on load
FLAGS = {"disableAllHooks": True, "disableSkillShellExecution": True}

SCRIPTS = {".sh", ".bash", ".zsh", ".fish", ".command", ".tool", ".py", ".pyw", ".js", ".mjs", ".cjs", ".ts", ".mts",
           ".cts", ".jsx", ".tsx", ".rb", ".pl", ".pm", ".php", ".lua", ".r", ".ps1", ".psm1", ".bat", ".cmd", ".vbs",
           ".applescript", ".scpt", ".jxa", ".swift", ".go", ".rs", ".c", ".cc", ".cpp", ".java", ".kt", ".jar", ".exe",
           ".bin", ".so", ".dylib", ".dll", ".wasm", ".app", ".pkg", ".dmg", ".deb", ".rpm", ".whl", ".egg"}
SCRIPT_NAMES = {"Makefile", "makefile", "GNUmakefile", "Dockerfile", "Justfile", "justfile", "Rakefile", "package.json",
                "pyproject.toml", "setup.py", "requirements.txt"}
# Binary data formats; any other binary file counts as a program
MEDIA = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico", ".tif", ".tiff", ".heic", ".pdf", ".ttf", ".otf",
         ".woff", ".woff2", ".mp3", ".wav", ".m4a", ".mp4", ".mov", ".docx", ".xlsx", ".pptx", ".odt", ".zip", ".gz"}

_SCHEMA = """
create table skills (
    name text primary key,      -- folder name and SKILL.md name
    source text not null,       -- installed, written, learned, manual
    origin text,                -- installed: source link
    enabled integer not null default 0,
    scripts text,               -- JSON list of executable files, as last scanned
    digest text,                -- of files other than SKILL.md, as last scanned
    approved text,              -- digest the user approved after the review
    review text,                -- JSON: latest script review
    created real not null,
    updated real not null
)"""


class NeedsApproval(ValueError):
    """The skill's current scripts aren't approved."""


def _db() -> AbstractContextManager[sqlite3.Connection]:
    return database.table("skills", _SCHEMA)


def home() -> Path:
    """The profile's skills folder (created if missing)."""
    (found := profile.current().skills).mkdir(parents=True, exist_ok=True)
    return found


def folder(name: str) -> Path:
    return profile.current().skills / name


# ── SKILL.md ────────────────────────────────────────────────────

_FRONTMATTER = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)(.*)", re.DOTALL)
INLINE = re.compile(r"!`[^`\n]+`")  # !`command`, which Claude Code runs on load


def split(text: str) -> tuple[dict, str]:
    """(frontmatter, body). Raises ValueError if frontmatter is missing or not a YAML mapping."""
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
    """Strip non-KEPT frontmatter and optionally set `name`. Returns (text, dropped fields); text is unchanged if
    nothing needed changing."""
    meta, body = split(text)
    dropped = [str(k) for k in meta if k not in KEPT]
    if not dropped and (name is None or meta.get("name") == name):
        return text, []
    kept = {k: v for k, v in meta.items() if k in KEPT}
    if name is not None:
        kept = {"name": name} | kept
    return compose(kept, body), dropped


def parse(text: str) -> tuple[dict, str]:
    """(frontmatter, body) of a SKILL.md that may have no frontmatter ({} then). Raises ValueError if it starts a
    frontmatter block that isn't valid."""
    if not text.lstrip("﻿").startswith("---"):
        return {}, text.strip()
    return split(text)


def one_line(text: str) -> str:
    return " ".join(str(text or "").split())


def render(name: str, description: str, instructions: str, keep: dict | None = None) -> str:
    """Build a SKILL.md, preserving other allowed frontmatter from the replaced version (`keep`)."""
    meta = {"name": name, "description": one_line(description)}
    meta |= {k: v for k, v in (keep or {}).items() if k in KEPT and k not in meta}
    return compose(meta, instructions)


def check_text(name: str, description: str, instructions: str) -> None:
    """Raise ValueError saying what to fix if the skill can't be saved."""
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
    """'Standup Notes' -> standup-notes."""
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", str(text).lower())).strip("-")[:64].strip("-")


def as_name(text: str) -> str:
    """`text` if already a valid name, else its slug."""
    return text if NAME.fullmatch(text) else slug(text)


# ── Folder contents ─────────────────────────────────────────────


def files(where: Path) -> list[str]:
    """Sorted relative paths of all files, including symlinks (so they can be refused)."""
    found = []
    for root, dirs, names in os.walk(where):
        dirs.sort()
        for n in [*names, *(d for d in dirs if (Path(root) / d).is_symlink())]:
            found.append((Path(root) / n).relative_to(where).as_posix())
    return sorted(found)


def links(where: Path) -> list[str]:
    return [f for f in files(where) if (where / f).is_symlink()]


def is_script(path: Path) -> bool:
    """Executable bit, script-like name, #! shebang, or non-media binary."""
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
    """SHA-256 over all files except SKILL.md: what script approval covers. Instruction edits don't need
    re-approval."""
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
    """Reasons the folder can't be used; empty if fine."""
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
        found.append(f"it holds links ({', '.join(linked[:3])}), which could point anywhere on the computer")
    return found


def skill_md(where: Path) -> str:
    """Current SKILL.md text, or ""."""
    path = where / SKILL_FILE
    return path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""


def _meta(where: Path) -> dict:
    """SKILL.md frontmatter, or {} if unreadable."""
    try:
        return split((where / SKILL_FILE).read_text(encoding="utf-8"))[0]
    except (OSError, UnicodeDecodeError, ValueError):
        return {}


def description_of(where: Path) -> str:
    return one_line(_meta(where).get("description"))


# ── Table ───────────────────────────────────────────────────────


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
    where: Path | None = None  # its folder, when not in the profile's skills folder (one sent to a machine)

    @property
    def folder(self) -> Path:
        return self.where or folder(self.name)

    @property
    def needs_approval(self) -> bool:
        """Has scripts whose current digest isn't approved."""
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
        """Summary for the voice tools and panel."""
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
    """All skills by name, synced with disk: new folders get a disabled row, rows without folders are dropped, and
    scripts/digests are refreshed."""
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
                 if r["name"] in on_disk]  # skip rows another process added meanwhile
    return found


def get(name: str) -> Skill:
    """Find by exact name, slug ('standup notes'), or unique substring. Raises ValueError listing skills otherwise."""
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
    """Return `skill`, or raise ValueError if it has problems."""
    if skill.problems:
        raise ValueError(f"The skill {skill.name} can't be switched on: {'; '.join(skill.problems)}.")
    return skill


def switch(name: str, on: bool) -> Skill:
    """Enable or disable. Enabling raises ValueError if broken, NeedsApproval if scripts aren't approved."""
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
    """Record approval of the reviewed digest and enable. Raises ValueError if files changed since the review."""
    skill = refuse_broken(get(name))
    if reviewed != skill.digest:
        raise ValueError(f"The skill {skill.name}'s files have changed since they were reviewed; review them again.")
    _update(skill.name, approved=reviewed, enabled=1)
    return get(skill.name)


def delete(name: str) -> str:
    """Delete the skill's folder and row."""
    skill = get(name)
    where = skill.folder
    if where.is_dir() and not where.is_symlink():
        shutil.rmtree(where)
    with _db() as db:
        db.execute("delete from skills where name = ?", (skill.name,))
    return skill.name


def save(name: str, description: str, instructions: str, source: str = WRITTEN, enable: bool = True,
         meta: dict | None = None) -> tuple[Skill, bool]:
    """Write SKILL.md for a new skill (enabled per `enable`) or update an existing one (keeps source, files, state).
    `meta` is other frontmatter to set (KEPT fields only), over what the replaced version had. Returns (skill, is_new)."""
    check_text(name, description, instructions)
    where, now = home() / name, time.time()
    new = not where.exists()
    keep = ({} if new else _meta(where)) | (meta or {})
    where.mkdir(exist_ok=True)
    write_atomic(where / SKILL_FILE, render(name, description, instructions, keep), mode=0o644)
    with _db() as db:
        if not new and db.execute("select 1 from skills where name = ?", (name,)).fetchone():
            db.execute("update skills set updated = ? where name = ?", (now, name))
        else:  # replaces a stale row whose folder was removed by hand
            db.execute("insert or replace into skills (name, source, enabled, scripts, digest, created, updated) "
                       "values (?, ?, ?, ?, ?, ?, ?)", (name, source, int(enable), json.dumps(scripts(where)),
                                                        digest(where), now, now))
    return get(name), new


def install(staged: Path, name: str, origin: str) -> Skill:
    """Move a staged skill folder into place as `name`, replacing any existing one. Enabled only if it has no
    scripts; otherwise it awaits review and approval."""
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
    """Enabled, unbroken skills with approved scripts. Enabled ones left out are logged with the reason."""
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


def whole_digest(where: Path) -> str:
    """SHA-256 over every file, SKILL.md included: one version of the skill, as sent to another machine."""
    return hashlib.sha256(f"{skill_md(where)}\0{digest(where)}".encode()).hexdigest()


def shipped(folders: list[str]) -> list[Skill]:
    """Skills sent from the main computer for a run on a machine (jarvis.node.work): switched on, and their scripts
    approved, there. Their files are checked again here; a broken one is left out."""
    found = []
    for path in map(Path, folders):
        name = str(_meta(path).get("name") or "")
        if broken := problems(path, name or None):
            log.warning("sent skill %s left out: %s", path.name, "; ".join(broken))
            continue
        approved, now = digest(path), time.time()  # approved on the main computer
        found.append(Skill(name, description_of(path), INSTALLED, "", True, scripts(path), approved, approved, None, now,
                           now, [], path))
    return found


def plugin(where: Path, found: list[Skill] | None = None) -> list[Skill]:
    """Build the session plugin at `where`: plugin.json plus links to usable skills (or `found`, e.g. skills sent from
    the main computer), nothing else (no hooks, agents, commands or MCP servers). Returns the skills; no plugin is made
    if there are none. Use `qualified` for SDK names."""
    found = usable() if found is None else found
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
    """Plugin path for this voice process; clears plugins of dead voice processes."""
    sessions = home() / ".sessions"
    sessions.mkdir(exist_ok=True)
    for old in sessions.glob("voice-*"):
        pid = old.name.removeprefix("voice-")
        if pid.isdigit() and int(pid) != os.getpid() and not alive(int(pid)):
            _clear(old)
    return sessions / f"voice-{os.getpid()}"


def listing(found: list[Skill]) -> str:
    """Prompt listing, one line per skill."""
    return "\n".join(f"- {qualified(s)}: {s.description}" for s in found)
