"""Copying a project's files to a machine and bringing what changed back, rsync-like, over the link.

A manifest maps each file's path (relative to the folder) to its size, SHA-256 and executable bit, or a symlink to its
target. Hashes are cached by size and modification time, so only changed files are read again. The side that wants
files asks for those whose entry differs, and they come as one tar.

Never copied, either way: secrets (secret folders, .env files, keys, credential files, anything `commands.secret_path`
would hide), the .git folder (history travels as a git bundle instead), caches that can't move between folders
(Python virtualenvs, bytecode, test and type-check caches), and symlinks that point outside the folder or anywhere
absolute. Unpacking checks every name again, so a damaged or hostile archive can't write outside the folder or
plant a secret-named file.
"""

import hashlib
import json
import os
import shutil
import stat
import tarfile
from pathlib import Path, PurePosixPath

from jarvis.core.files import write_atomic
from jarvis.safety.commands import SECRET_DIRS, SECRET_FILES

# Not copied: git's own folder, and caches tied to the folder they were made in
SKIP_DIRS = {".git", "venv", ".venv", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox"}
SKIP_FILES = {".DS_Store"}
LINK = "link"


def _secret(rel: PurePosixPath) -> bool:
    return bool(SECRET_DIRS.intersection(rel.parts)) or bool(SECRET_FILES.fullmatch(rel.name))


def allowed(rel: str) -> bool:
    """Whether a relative path may be copied: plain (no absolute or .. parts), not skipped, not secret."""
    path = PurePosixPath(rel)
    if not rel or path.is_absolute() or any(p in ("", ".", "..") for p in rel.split("/")):
        return False
    return not (SKIP_DIRS.intersection(path.parts[:-1]) or path.name in SKIP_FILES or path.name in SKIP_DIRS
                or _secret(path))


def _inside_link(folder: Path, rel: str, target: str) -> bool:
    """A relative symlink whose target stays inside `folder`."""
    if os.path.isabs(target):
        return False
    resolved = os.path.normpath(os.path.join(os.path.dirname(rel), target))
    return not resolved.startswith("..") and not os.path.isabs(resolved)


def _hash(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while block := f.read(1 << 20):
            h.update(block)
    return h.hexdigest()


def manifest(folder: Path, cache: Path | None = None) -> dict[str, list]:
    """{rel: [size, sha256, executable]} for files, {rel: ["link", target]} for symlinks kept inside `folder`.

    `cache` (a JSON file) keeps hashes by (size, mtime) between calls."""
    try:
        known = json.loads(cache.read_text()) if cache and cache.exists() else {}
    except (OSError, ValueError):
        known = {}
    found: dict[str, list] = {}
    kept: dict[str, list] = {}
    for top, dirs, names in os.walk(folder):
        links = [d for d in dirs if os.path.islink(os.path.join(top, d))]  # copied as links, never followed
        dirs[:] = sorted(d for d in dirs if d not in links and d not in SKIP_DIRS and d not in SECRET_DIRS)
        for name in sorted(names + links):
            path = os.path.join(top, name)
            rel = os.path.relpath(path, folder).replace(os.sep, "/")
            if not allowed(rel):
                continue
            try:
                st = os.lstat(path)
            except OSError:
                continue
            if stat.S_ISLNK(st.st_mode):
                target = os.readlink(path)
                if _inside_link(folder, rel, target):
                    found[rel] = [LINK, target]
                continue
            if not stat.S_ISREG(st.st_mode):
                continue
            seen = known.get(rel)
            if seen and seen[0] == st.st_size and seen[1] == st.st_mtime_ns:
                digest = seen[2]
            else:
                try:
                    digest = _hash(Path(path))
                except OSError:
                    continue
            kept[rel] = [st.st_size, st.st_mtime_ns, digest]
            found[rel] = [st.st_size, digest, bool(st.st_mode & 0o111)]
    if cache:
        cache.parent.mkdir(parents=True, exist_ok=True)
        write_atomic(cache, json.dumps(kept))
    return found


def entry(folder: Path, rel: str) -> list | None:
    """One path's manifest entry as it is now (hashed afresh), or None if it's missing or not copied."""
    path = folder / rel
    if not allowed(rel):
        return None
    if path.is_symlink():
        target = os.readlink(path)
        return [LINK, target] if _inside_link(folder, rel, target) else None
    if not path.is_file():
        return None
    st = path.stat()
    return [st.st_size, _hash(path), bool(st.st_mode & 0o111)]


def differing(wanted: dict[str, list], have: dict[str, list]) -> list[str]:
    """Paths in `wanted` that `have` lacks or holds differently."""
    return sorted(rel for rel, entry in wanted.items() if have.get(rel) != entry)


def total(entries: dict[str, list], rels: list[str]) -> int:
    return sum(entries[r][0] for r in rels if entries.get(r) and entries[r][0] != LINK)


def pack(folder: Path, rels: list[str], into: Path) -> Path:
    """A tar of `rels` from `folder` (each checked again; links as links). Missing ones are left out."""
    into.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(into, "w", format=tarfile.PAX_FORMAT) as tar:
        for rel in rels:
            path = folder / rel
            if not allowed(rel) or not (path.is_symlink() or path.is_file()):
                continue
            if path.is_symlink() and not _inside_link(folder, rel, os.readlink(path)):
                continue
            tar.add(path, arcname=rel, recursive=False)
    return into


def _safe(folder: Path, rel: str) -> Path | None:
    """Where `rel` lands in `folder`, or None if anything on the way is a symlink or it would leave the folder."""
    if not allowed(rel):
        return None
    target = folder / rel
    parent = folder
    for part in PurePosixPath(rel).parts[:-1]:
        parent = parent / part
        if parent.is_symlink() or (parent.exists() and not parent.is_dir()):
            return None
    return target


def unpack(archive: Path, folder: Path) -> list[str]:
    """Write an archive's regular files and inside-pointing symlinks into `folder`; returns the paths written.

    Anything else (devices, hard links, absolute or escaping names, secrets) is skipped."""
    written = []
    with tarfile.open(archive, "r") as tar:
        for member in tar:
            rel = member.name
            if (target := _safe(folder, rel)) is None:
                continue
            if member.issym():
                if not _inside_link(folder, rel, member.linkname):
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.is_symlink() or target.is_file():
                    target.unlink()
                elif target.is_dir():
                    shutil.rmtree(target)
                target.symlink_to(member.linkname)
            elif member.isfile():
                source = tar.extractfile(member)
                if source is None:
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.is_symlink():
                    target.unlink()
                elif target.is_dir():
                    shutil.rmtree(target)
                partial = target.with_name(f".{target.name}.jarvis-part")
                with open(partial, "wb") as out:
                    shutil.copyfileobj(source, out)
                os.chmod(partial, 0o755 if member.mode & 0o111 else 0o644)
                os.utime(partial, (member.mtime, member.mtime))
                os.replace(partial, target)
            else:
                continue
            written.append(rel)
    return written


def remove(folder: Path, rels: list[str]) -> list[str]:
    """Delete these files or links from `folder` (never folders, never outside it), then any folders left empty."""
    gone = []
    for rel in rels:
        if (target := _safe(folder, rel)) is None or not (target.is_symlink() or target.is_file()):
            continue
        target.unlink()
        gone.append(rel)
        parent = target.parent
        while parent != folder and parent.is_dir() and not any(parent.iterdir()):
            parent.rmdir()
            parent = parent.parent
    return gone
