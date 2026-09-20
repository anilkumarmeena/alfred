"""Projects that code tasks can work on, kept in the projects table of data/tasks.db beside the to-do list.

Names are matched ignoring case, spaces and punctuation, so "at las" from speech
recognition still finds "atlas". Aliases cover names Whisper tends to mishear.
code_change tasks work in the project's own folder by default (in place); in_place false
opts a project into a separate git worktree and branch per task instead. JARVIS's own repo
always works that way, whatever its entry says: a task editing JARVIS in place would be
editing the code that's running it.

register() adds a project: it makes the folder if needed and adds an entry, without
touching git. A folder without git history is always in_place, since worktrees need commits.
update() changes a project's folder, aliases, about or in_place; remove() takes one off the list.
init_git() sets git up later, on request: git init, a default .gitignore and a first commit.

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

ALLOWED_ROOT = Path.home()  # new projects live somewhere under here
PROTECTED = [DATA_DIR, SECRETS_DIR]  # JARVIS's own folders, never a project
JARVIS_REPO = ROOT  # JARVIS itself: a project whose code tasks always get a worktree and branch
NO_GIT, NO_COMMITS, GIT = "none", "no commits", "repo"
# Dependency and cache folders: never part of a task's work, whatever a project's .gitignore says
CACHE_DIRS = ("node_modules", "venv", ".venv", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".next")
DEFAULT_GITIGNORE = "\n".join([
    ".DS_Store", ".env", ".env.*", "!.env.example", *(f"{d}/" for d in CACHE_DIRS),
    "*.pyc", "dist/", "build/", "coverage/", "*.log",
]) + "\n"

IN_PLACE_DEFAULT = True  # for projects registered without saying

_SCHEMA = """
create table projects (
    id integer primary key,     -- the list's order
    name text not null unique,
    path text not null,         -- as given: ~/... for a folder under the home folder
    aliases text not null default '[]',  -- JSON list of other names speech recognition might hear
    about text not null default '',
    in_place integer not null default 1  -- 0: each code_change task gets its own worktree and branch
)"""


@dataclass
class Project:
    name: str
    path: Path
    aliases: list[str]
    about: str
    in_place: bool = IN_PLACE_DEFAULT  # code_change tasks edit the project's folder directly: no worktree, branch or commit


def is_jarvis(path: Path) -> bool:
    """Whether `path` is JARVIS's own repo."""
    return path.expanduser().resolve() == JARVIS_REPO.resolve()


def _key(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


# ── The table ───────────────────────────────────────────────────


def _db() -> AbstractContextManager[sqlite3.Connection]:
    """The database, with the table made the first time; closed after."""
    return database.table("projects", _SCHEMA)


def _project(r: sqlite3.Row) -> Project:
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


# ── Git ─────────────────────────────────────────────────────────


def _git(folder: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(folder), *args], capture_output=True, text=True, timeout=120)


def git_state(folder: Path) -> str:
    """NO_GIT, NO_COMMITS (git init but nothing committed yet) or GIT."""
    if not folder.is_dir() or _git(folder, "rev-parse", "--git-dir").returncode:
        return NO_GIT
    return GIT if _git(folder, "rev-parse", "--verify", "-q", "HEAD").returncode == 0 else NO_COMMITS


def worktree_problem(p: Project) -> str | None:
    """Why a code_change task can't have its own worktree and branch in this project, if it can't."""
    state = git_state(p.path)
    if state == GIT:
        return None
    why = "isn't a git repository" if state == NO_GIT else "has no git commits yet"
    return (f"{p.name} {why}, so a task can't work on its own branch there. "
            "Set in_place to true for it, or set up git for it first.")


def _enclosing_repo(path: Path) -> Path | None:
    """The top of a git repo that `path` sits inside of, other than `path` itself."""
    existing = next(p for p in (path, *path.parents) if p.exists())
    out = _git(existing, "rev-parse", "--show-toplevel")
    if out.returncode:
        return None
    top = Path(out.stdout.strip()).resolve()
    return None if top == path else top


def init_git(name: str) -> dict:
    """Put a registered project under git: git init, a default .gitignore if it has none, and a
    first commit of its current files (whatever .gitignore allows)."""
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


# ── Registering ─────────────────────────────────────────────────


def _check_folder(path: str) -> Path:
    """The folder `path` names, resolved, if it's somewhere a project may live."""
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
    """The path as the project list keeps it: ~/... when it's under the home folder."""
    home = Path.home().resolve()
    return f"~/{folder.relative_to(home)}" if folder.is_relative_to(home) else str(folder)


def _tidy_aliases(aliases: object) -> list[str]:
    if not (isinstance(aliases, list) and all(isinstance(a, str) for a in aliases)):
        raise ValueError("aliases must be a list of text.")
    return [a for a in (scan.clean(a).strip() for a in aliases) if a]


def _check_names(others: list[Project], names: list[str]) -> None:
    """That each name or alias has something to match on, and matches none of the other projects' names."""
    taken = {k: p.name for p in others for k in (_key(p.name), *map(_key, p.aliases))}
    for said in names:
        if not _key(said):
            raise ValueError(f"The alias '{said}' needs letters or digits in it.")
        if _key(said) in taken:
            raise ValueError(f"'{said}' would clash with the project {taken[_key(said)]}.")


def _check_own_repo(folder: Path) -> None:
    """That the folder isn't inside another git repo. Before the write lock: it runs git."""
    if top := _enclosing_repo(folder):
        raise ValueError(f"{folder} is inside the git repository at {top}. Register that folder instead, or pick another.")


def _check_free(others: list[Project], folder: Path) -> None:
    """That no other project has the folder."""
    for p in others:
        if p.path.resolve() == folder:
            raise ValueError(f"{folder} is already registered, as the project {p.name}.")


def mode(in_place: bool) -> str:
    """How a project's code tasks work, in a few words."""
    return "in place" if in_place else "with worktrees"


def register(name: str, path: str | None = None, aliases: list[str] | None = None, about: str = "",
             in_place: bool = IN_PLACE_DEFAULT) -> dict:
    """Add a project: make its folder if it's missing, and add it to the end of the list.
    Everything is checked before anything is written; git is left alone."""
    if not (isinstance(name, str) and isinstance(about, str) and (path is None or isinstance(path, str))):
        raise ValueError("name, path and about must be text.")
    if not isinstance(in_place, bool):
        raise ValueError("in_place must be true or false.")
    name, about = scan.clean(name).strip(), scan.clean(about).strip()  # no invisible characters (jarvis.safety.scan)
    aliases = _tidy_aliases(aliases or [])
    if not _key(name):
        raise ValueError("The project needs a name with letters or digits in it.")
    folder = _check_folder(path or str(_default_path(name)))
    _check_own_repo(folder)
    state = git_state(folder)
    in_place = (in_place or state != GIT) and not is_jarvis(folder)  # worktrees need git history; JARVIS always has one

    with _db() as db:
        db.execute("begin immediate")  # the checks and the entry together, so two at once can't both pass them
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
    """Change a project's folder (one that exists), aliases (the whole list), about or in_place; what's left out
    stays as it was. Its name stays: tasks are kept under it. Returns the project as it is now."""
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
        elif not wanted and git_state(where) != GIT:
            if in_place is False:
                raise ValueError(worktree_problem(Project(p.name, where, [], "")))
            wanted = True  # moved to a folder without git history: worktrees need one, as when registering
        db.execute("update projects set path = ?, aliases = ?, about = ?, in_place = ? where name = ?",
                   (shown(folder) if folder else row["path"],
                    row["aliases"] if aliases is None else json.dumps(aliases, ensure_ascii=False),
                    row["about"] if about is None else about.strip(), int(wanted), p.name))
        db.execute("commit")
    return {"name": p.name, "path": str(Path(shown(folder)).expanduser()) if folder else str(p.path),
            "aliases": p.aliases if aliases is None else aliases, "about": p.about if about is None else about.strip(),
            "in_place": wanted}


def remove(name: str) -> str:
    """Take a project off the list. Its folder stays as it is."""
    with _db() as db:
        p = _find(_read(db), name)
        db.execute("delete from projects where name = ?", (p.name,))
    return f"Removed {p.name} from the project list; its folder {p.path} is as it was."


# ── From the command line ───────────────────────────────────────


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

