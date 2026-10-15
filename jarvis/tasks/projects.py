"""Projects code tasks can work on (projects table in data/tasks.db).

Names match ignoring case, spaces and punctuation ("at las" finds "atlas"); aliases cover common mishearings.
code_change tasks edit the project folder in place by default; in_place=false gives each task a worktree and branch.
JARVIS's own repo always uses worktrees (a task must not edit the running code). Folders without commits are always
in place. register/update/remove never touch git; init_git sets it up on request.

    uv run python -m jarvis.tasks.projects                       list the projects
    uv run python -m jarvis.tasks.projects add NAME [--path P] [--aliases "a, b"] [--about T] [--worktrees]
    uv run python -m jarvis.tasks.projects set NAME [--path P] [--aliases "a, b"] [--about T] [--in-place | --worktrees]
    uv run python -m jarvis.tasks.projects remove NAME
"""

import argparse
import json
import re
import sqlite3
import subprocess
import sys
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path

from jarvis.core import database
from jarvis.core.config import DATA_DIR, NEW_PROJECTS_DIR, ROOT, SECRETS_DIR
from jarvis.safety import scan

ALLOWED_ROOT = Path.home()  # projects must live under here
PROTECTED = [DATA_DIR, SECRETS_DIR]  # never a project
JARVIS_REPO = ROOT  # always worktree mode
NO_GIT, NO_COMMITS, GIT = "none", "no commits", "repo"
# Dependency/cache folders, never part of a task's work regardless of .gitignore
CACHE_DIRS = ("node_modules", "venv", ".venv", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".next")
# What a task's commit takes in (git add -A): its folder, less what's excluded even if .gitignore misses it
JUNK = [*(f"{d}/" for d in CACHE_DIRS), "*.pyc", ".DS_Store"]
PATHSPEC = ["--", ".", *(f":(exclude,glob)**/{p}" for p in JUNK)]
DEFAULT_GITIGNORE = "\n".join([
    ".DS_Store", ".env", ".env.*", "!.env.example", *(f"{d}/" for d in CACHE_DIRS),
    "*.pyc", "dist/", "build/", "coverage/", "*.log",
]) + "\n"

IN_PLACE_DEFAULT = True

_SCHEMA = """
create table projects (
    id integer primary key,     -- list order
    name text not null unique,
    path text not null,         -- ~/... when under home
    aliases text not null default '[]',  -- JSON list
    about text not null default '',
    in_place integer not null default 1  -- 0: worktree + branch per task
)"""
_MIGRATIONS = (
    "alter table projects add column node integer",  # 2: the machine it lives on (jarvis.node); null = this computer
)


@dataclass
class Project:
    name: str
    path: Path  # on its machine, for one that lives on another (absolute, as that machine resolved it)
    aliases: list[str]
    about: str
    in_place: bool = IN_PLACE_DEFAULT  # edit the folder directly: no worktree, branch or commit
    node: int | None = None  # the machine it lives on; None = this computer


def is_jarvis(path: Path) -> bool:
    """Whether `path` is JARVIS's own repo."""
    return path.expanduser().resolve() == JARVIS_REPO.resolve()


def _key(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


# ── Table ───────────────────────────────────────────────────────


def _db() -> AbstractContextManager[sqlite3.Connection]:
    """DB connection context, creating the table on first use."""
    return database.table("projects", _SCHEMA, migrations=_MIGRATIONS)


def _project(r: sqlite3.Row) -> Project:
    if r["node"] is not None:  # its path is that machine's
        return Project(r["name"], Path(r["path"]), json.loads(r["aliases"]), r["about"], bool(r["in_place"]), r["node"])
    path = Path(r["path"]).expanduser()
    return Project(r["name"], path, json.loads(r["aliases"]), r["about"], bool(r["in_place"]) and not is_jarvis(path))


def _read(db: sqlite3.Connection) -> list[Project]:
    return [_project(r) for r in db.execute("select * from projects order by id")]


def load() -> list[Project]:
    with _db() as db:
        return _read(db)


def _find(known: list[Project], name: str) -> Project:
    key = _key(name)
    for p in known:
        if key in {_key(p.name), *map(_key, p.aliases)}:
            return p
    raise ValueError(f"Unknown project '{name}'. Known projects: {', '.join(p.name for p in known) or 'none yet'}.")


def find(name: str) -> Project:
    return _find(load(), name)


def living_at(folder: Path) -> Project | None:
    """The project living on another machine whose folder (a path there) holds `folder`, or None."""
    return next((p for p in load() if p.node and folder.is_relative_to(p.path)), None)


# ── Git ─────────────────────────────────────────────────────────


def _git(folder: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(folder), *args], capture_output=True, text=True, timeout=120)


def git_state(folder: Path) -> str:
    """NO_GIT, NO_COMMITS or GIT."""
    if not folder.is_dir() or _git(folder, "rev-parse", "--git-dir").returncode:
        return NO_GIT
    return GIT if _git(folder, "rev-parse", "--verify", "-q", "HEAD").returncode == 0 else NO_COMMITS


def worktree_problem(p: Project) -> str | None:
    """Why worktree mode can't work for `p`, or None."""
    state = git_state(p.path)
    if state == GIT:
        return None
    why = "isn't a git repository" if state == NO_GIT else "has no git commits yet"
    return (f"{p.name} {why}, so a task can't work on its own branch there. "
            "Set in_place to true for it, or set up git for it first.")


def _enclosing_repo(path: Path) -> Path | None:
    """Top of an enclosing git repo other than `path` itself, or None."""
    existing = next(p for p in (path, *path.parents) if p.exists())
    out = _git(existing, "rev-parse", "--show-toplevel")
    if out.returncode:
        return None
    top = Path(out.stdout.strip()).resolve()
    return None if top == path else top


def init_git(name: str) -> dict:
    """git init a registered project, add a default .gitignore if missing, and commit its current files."""
    p = find(name)
    folder = _check_folder(str(p.path))
    if not folder.is_dir():
        raise ValueError(f"{p.name}'s folder {p.path} doesn't exist.")
    if top := _enclosing_repo(folder):
        raise ValueError(f"{p.name}'s folder is inside another git repository, at {top}, so it can't get its own.")
    state = git_state(folder)
    if state == GIT:
        raise ValueError(f"{p.name} already has git set up, with commits.")
    if state == NO_GIT:
        _run_git(folder, "init", "-q")
    gitignore = folder / ".gitignore"
    added_gitignore = not gitignore.exists()
    if added_gitignore:
        gitignore.write_text(DEFAULT_GITIGNORE)
    _run_git(folder, "add", "-A")
    _run_git(folder, "commit", "-q", "--no-verify", "--allow-empty", "-m", "Initial commit")
    files = len(_run_git(folder, "ls-files").splitlines())
    branch = _run_git(folder, "symbolic-ref", "--short", "HEAD")
    return {"name": p.name, "path": str(folder), "branch": branch, "files_committed": files,
            "added_default_gitignore": added_gitignore, "in_place": p.in_place}


def _run_git(folder: Path, *args: str) -> str:
    out = _git(folder, *args)
    if out.returncode:
        raise RuntimeError(f"git {args[0]} failed: {out.stderr.strip()}"[:300])
    return out.stdout.strip()


# ── Register ────────────────────────────────────────────────────


def _check_folder(path: str) -> Path:
    """Resolve `path`; ValueError unless it's an allowed project location."""
    given = Path(path).expanduser()
    if not given.is_absolute():
        raise ValueError(f"The path must be absolute or start with ~, not '{path}'.")
    folder, root = given.resolve(), ALLOWED_ROOT.resolve()
    if not folder.is_relative_to(root):
        raise ValueError(f"Projects must be inside {root}, and {folder} isn't.")
    if folder == root:
        raise ValueError(f"A project can't be the whole of {root}.")
    parts = folder.relative_to(root).parts
    if parts[0].lower() == "library":
        raise ValueError(f"Projects can't go in {root / parts[0]}.")
    if any(part.startswith(".") for part in parts):
        raise ValueError(f"Projects can't go in hidden folders, and {folder} is one.")
    for protected in map(Path.resolve, PROTECTED):
        if folder.is_relative_to(protected):
            raise ValueError(f"{folder} is inside JARVIS's own {protected.name} folder.")
    if folder.exists() and not folder.is_dir():
        raise ValueError(f"{folder} is a file, not a folder.")
    return folder


def _default_path(name: str) -> Path:
    slug = re.sub(r"[^\w.-]+", "-", name.strip().lower()).strip("-.")
    if not slug:
        raise ValueError(f"Give a folder for '{name}': its name doesn't make a folder name.")
    return NEW_PROJECTS_DIR / slug


def shown(folder: Path) -> str:
    """Path as stored: ~/... when under home."""
    home = Path.home().resolve()
    return f"~/{folder.relative_to(home)}" if folder.is_relative_to(home) else str(folder)


def _tidy_aliases(aliases: object) -> list[str]:
    if not (isinstance(aliases, list) and all(isinstance(a, str) for a in aliases)):
        raise ValueError("aliases must be a list of text.")
    return [a for a in (scan.clean(a).strip() for a in aliases) if a]


def _check_names(others: list[Project], names: list[str]) -> None:
    """Check each name is matchable and doesn't clash with other projects."""
    taken = {k: p.name for p in others for k in (_key(p.name), *map(_key, p.aliases))}
    for said in names:
        if not _key(said):
            raise ValueError(f"The alias '{said}' needs letters or digits in it.")
        if _key(said) in taken:
            raise ValueError(f"'{said}' would clash with the project {taken[_key(said)]}.")


def _check_own_repo(folder: Path) -> None:
    """Reject a folder inside another git repo. Call before taking the write lock (runs git)."""
    if top := _enclosing_repo(folder):
        raise ValueError(f"{folder} is inside the git repository at {top}. Register that folder instead, or pick another.")


def _check_free(others: list[Project], folder: Path) -> None:
    """Reject a folder another project already uses."""
    for p in others:
        if p.path.resolve() == folder:
            raise ValueError(f"{folder} is already registered, as the project {p.name}.")


def mode(in_place: bool) -> str:
    """Short label for a project's task mode."""
    return "in place" if in_place else "with worktrees"


def _clean_entry(name: object, about: object, aliases: object) -> tuple[str, str, list[str]]:
    if not (isinstance(name, str) and isinstance(about, str)):
        raise ValueError("name, path and about must be text.")
    name, about = scan.clean(name).strip(), scan.clean(about).strip()  # strip invisible characters
    if not _key(name):
        raise ValueError("The project needs a name with letters or digits in it.")
    return name, about, _tidy_aliases(aliases or [])


def register_there(name: str, node: int, folder: str, state: str, aliases: list[str] | None = None, about: str = "",
                   in_place: bool = IN_PLACE_DEFAULT) -> dict:
    """Add a project that lives on machine `node`, at `folder` as that machine checked it (absolute, in its home;
    `state` its git state there)."""
    name, about, aliases = _clean_entry(name, about, aliases)
    in_place = in_place or state != GIT
    with _db() as db:
        db.execute("begin immediate")
        known = _read(db)
        if any(p.node == node and str(p.path) == folder for p in known):
            raise ValueError(f"{folder} on that machine is already registered.")
        _check_names(known, [name, *aliases])
        db.execute("insert into projects (name, path, aliases, about, in_place, node) values (?, ?, ?, ?, ?, ?)",
                   (name, folder, json.dumps(aliases, ensure_ascii=False), about, int(in_place), node))
        db.execute("commit")
    return {"name": name, "path": folder, "in_place": in_place, "node": node,
            "git": {NO_GIT: "none", NO_COMMITS: "initialised, no commits yet", GIT: "existing repo, left as is"}[state]}


def register(name: str, path: str | None = None, aliases: list[str] | None = None, about: str = "",
             in_place: bool = IN_PLACE_DEFAULT) -> dict:
    """Add a project, creating its folder if missing. All checks run before any write; git untouched."""
    if not (path is None or isinstance(path, str)):
        raise ValueError("name, path and about must be text.")
    if not isinstance(in_place, bool):
        raise ValueError("in_place must be true or false.")
    name, about, aliases = _clean_entry(name, about, aliases)
    folder = _check_folder(path or str(_default_path(name)))
    _check_own_repo(folder)
    state = git_state(folder)
    in_place = (in_place or state != GIT) and not is_jarvis(folder)  # worktrees need commits; JARVIS never in place

    with _db() as db:
        db.execute("begin immediate")  # checks + insert atomically
        known = _read(db)
        _check_free(known, folder)
        _check_names(known, [name, *aliases])

        created = not folder.exists()
        folder.mkdir(parents=True, exist_ok=True)
        try:
            db.execute("insert into projects (name, path, aliases, about, in_place) values (?, ?, ?, ?, ?)",
                       (name, shown(folder), json.dumps(aliases, ensure_ascii=False), about, int(in_place)))
            db.execute("commit")
        except BaseException:
            if created:
                folder.rmdir()
            raise
    return {"name": name, "path": str(folder), "created_folder": created, "in_place": in_place,
            "git": {NO_GIT: "none", NO_COMMITS: "initialised, no commits yet", GIT: "existing repo, left as is"}[state]}


def update(name: str, path: str | None = None, aliases: list[str] | None = None, about: str | None = None,
           in_place: bool | None = None) -> dict:
    """Change an existing project's folder, aliases (full list), about or in_place; None leaves a field as is.

    The name is immutable (tasks reference it). Returns the updated project.
    """
    if path is None and aliases is None and about is None and in_place is None:
        raise ValueError("Say what to change: path, aliases, about or in_place.")
    if not (path is None or isinstance(path, str)) or not (about is None or isinstance(about, str)):
        raise ValueError("path and about must be text.")
    if in_place is not None and not isinstance(in_place, bool):
        raise ValueError("in_place must be true or false.")
    if about is not None:
        about = scan.clean(about)
    if aliases is not None:
        aliases = _tidy_aliases(aliases)
    if path is not None and find(name).node:
        raise ValueError(f"{name}'s folder is on another machine: register it again with the new folder there.")
    folder = _check_folder(path) if path is not None else None
    if folder and not folder.is_dir():
        raise ValueError(f"{folder} doesn't exist. Make it, or move the project there, first.")
    if folder:
        _check_own_repo(folder)

    with _db() as db:
        db.execute("begin immediate")
        known = _read(db)
        p = _find(known, name)
        others = [k for k in known if k.name != p.name]
        row = db.execute("select * from projects where name = ?", (p.name,)).fetchone()
        if folder:
            _check_free(others, folder)
        if aliases is not None:
            _check_names(others, aliases)
        where = folder or p.path
        wanted = bool(row["in_place"]) if in_place is None else in_place
        if is_jarvis(where):
            if in_place:
                raise ValueError("JARVIS's own project always works on a branch of its own, never in place.")
            wanted = False
        elif not wanted and p.node is None and git_state(where) != GIT:  # a machine's folder is checked there
            if in_place is False:
                raise ValueError(worktree_problem(Project(p.name, where, [], "")))
            wanted = True  # no commits: worktrees impossible
        db.execute("update projects set path = ?, aliases = ?, about = ?, in_place = ? where name = ?",
                   (shown(folder) if folder else row["path"],
                    row["aliases"] if aliases is None else json.dumps(aliases, ensure_ascii=False),
                    row["about"] if about is None else about.strip(), int(wanted), p.name))
        db.execute("commit")
    return {"name": p.name, "path": str(Path(shown(folder)).expanduser()) if folder else str(p.path),
            "aliases": p.aliases if aliases is None else aliases, "about": p.about if about is None else about.strip(),
            "in_place": wanted}


def remove(name: str) -> str:
    """Remove a project from the list; its folder is untouched."""
    with _db() as db:
        p = _find(_read(db), name)
        db.execute("delete from projects where name = ?", (p.name,))
    return f"Removed {p.name} from the project list; its folder {p.path} is as it was."


# ── CLI ─────────────────────────────────────────────────────────


def show(known: list[Project]) -> str:
    if not known:
        return "No projects yet. Add one with: uv run python -m jarvis.tasks.projects add NAME"
    lines = []
    for p in known:
        lines.append(f"{p.name}: {shown(p.path)}, {'in place' if p.in_place else 'a worktree and branch per task'}")
        if p.aliases:
            lines.append(f"  aliases: {', '.join(p.aliases)}")
        if p.about:
            lines.append(f"  about: {p.about}")
    return "\n".join(lines)


def _aliases_arg(text: str | None) -> list[str] | None:
    return None if text is None else text.split(",")  # tidied by register and update


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m jarvis.tasks.projects",
                                     description="The projects JARVIS's code tasks can work on.")
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("list", help="list the projects (the default)")
    add = commands.add_parser("add", help="add a project, making its folder if it's missing")
    change = commands.add_parser("set", help="change a project's folder, aliases, about or in place")
    for sub in (add, change):
        sub.add_argument("name")
        sub.add_argument("--path", help="its folder, absolute or ~/...")
        sub.add_argument("--aliases", help='other names speech recognition might hear, comma-separated ("" for none)')
        sub.add_argument("--about", help="what it is, and whether it's work or personal")
    add.add_argument("--worktrees", action="store_true", help="a worktree and branch per task, not in place")
    how = change.add_mutually_exclusive_group()
    how.add_argument("--in-place", dest="in_place", action="store_true", default=None, help="tasks work in its folder")
    how.add_argument("--worktrees", dest="in_place", action="store_false", help="a worktree and branch per task")
    commands.add_parser("remove", help="take a project off the list, leaving its folder").add_argument("name")
    args = parser.parse_args(argv)

    try:
        match args.command or "list":
            case "list":
                print(show(load()))
            case "add":
                out = register(args.name, args.path, _aliases_arg(args.aliases), args.about or "", not args.worktrees)
                print(f"Added {out['name']} at {out['path']}, {mode(out['in_place'])}.")
            case "set":
                print(show([find(update(args.name, args.path, _aliases_arg(args.aliases), args.about,
                                        args.in_place)["name"])]))
            case "remove":
                print(remove(args.name))
    except (ValueError, sqlite3.Error) as e:
        sys.exit(str(e))
    return 0


if __name__ == "__main__":
    sys.exit(main())

