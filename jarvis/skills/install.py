"""Fetch skills from a GitHub repo, a folder in one, or an https zip link.

`stage` downloads into data/skills/.staging/<id> (never loaded by sessions), unpacking only the requested folder:
paths escaping the folder are refused, symlinks left out, size capped (MAX_DOWNLOAD, MAX_FILES, MAX_UNPACKED). It
reports each skill found: files, scripts, frontmatter to be stripped, and review.scan flags. Nothing is installed
until the user says yes; `install` then hands one skill to home.install. Stale stages are cleared after STAGING_HOURS.

https only, never local addresses, redirects included (browser's checks). GitHub links map to codeload.github.com
archives of the default branch unless the link names one.
"""

import io
import logging
import os
import re
import secrets
import shutil
import stat
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from urllib.parse import quote, urlsplit

import httpx

from jarvis.safety import urls
from jarvis.skills import home, review

log = logging.getLogger("jarvis.skills.install")

MAX_DOWNLOAD = 25 * 1024 * 1024  # archive bytes
MAX_UNPACKED = 50 * 1024 * 1024  # unpacked bytes
MAX_FILES = 2000
MAX_DEPTH = 6  # max folder depth searched for SKILL.md
TIMEOUT = 60
STAGING_HOURS = 24
SKIPPED = {".git", "__MACOSX", "node_modules", "__pycache__", ".DS_Store"}  # never unpacked
_GITHUB = {"github.com", "www.github.com"}
_SHORT = re.compile(r"([A-Za-z0-9][\w.-]*)/([\w.-]+)")  # owner/repo


@dataclass
class Source:
    """The link as given and the (archive URL, folder) pairs to try in order."""

    shown: str
    tries: list[tuple[str, str]]  # (archive URL, folder inside; "" for all)


@dataclass
class Found:
    """A skill found in a fetch."""

    rel: str  # folder relative to the fetch root ("" for the root)
    name: str  # install name
    description: str
    files: list[str]
    scripts: list[str]
    dropped: list[str] = field(default_factory=list)  # frontmatter to be stripped
    problems: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)  # review.scan findings

    def brief(self) -> dict:
        out = {"name": self.name, "description": self.description, "files": len(self.files)}
        for key in ("scripts", "dropped", "problems", "flags"):
            if value := getattr(self, key):
                out[key] = value
        return out


@dataclass
class Staged:
    id: str
    origin: str
    root: Path  # unpacked files
    skills: list[Found]
    left_out: list[str] = field(default_factory=list)  # symlinks skipped while unpacking


def staging() -> Path:
    where = home.home() / ".staging"
    where.mkdir(exist_ok=True)
    return where


def clear_old(now: float | None = None) -> None:
    """Delete stages older than STAGING_HOURS."""
    now = time.time() if now is None else now
    for old in staging().iterdir():
        try:
            if now - old.stat().st_mtime > STAGING_HOURS * 3600:
                shutil.rmtree(old) if old.is_dir() and not old.is_symlink() else old.unlink()
        except OSError:
            log.warning("couldn't clear the staged fetch %s", old.name)


def _github(owner: str, repo: str, rest: list[str], shown: str) -> Source:
    repo = repo.removesuffix(".git")
    archive = f"https://codeload.github.com/{quote(owner)}/{quote(repo)}/zip/"
    if not rest:
        return Source(shown, [(archive + "HEAD", "")])
    if rest[0] not in ("tree", "blob") or len(rest) < 2:
        raise ValueError("That GitHub link isn't a repo or a folder in one: give the repo's link, or the link to the "
                         "skill's folder (…/tree/main/skills/name).")
    if rest[0] == "blob" and rest[-1] != home.SKILL_FILE:
        raise ValueError(f"That's a link to a file; give the link to the skill's folder, or to its {home.SKILL_FILE}.")
    path = rest[1:-1] if rest[0] == "blob" else rest[1:]
    # Branch names may contain slashes (feature/x): try splitting after 1, 2 or 3 path parts
    tries = [(archive + quote("/".join(path[:i]), safe=""), "/".join(path[i:])) for i in range(1, min(len(path), 3) + 1)]
    return Source(shown, tries)


def parse(text: str) -> Source:
    """Parse owner/repo, a GitHub repo/tree/blob(SKILL.md) link, or an https .zip link. Raises ValueError otherwise."""
    text = text.strip().strip("<>")
    if m := _SHORT.fullmatch(text):
        return _github(m[1], m[2], [], f"github.com/{text}")
    if re.match(r"^(www\.)?github\.com/", text, re.IGNORECASE):
        text = "https://" + text
    parts = urlsplit(text)
    if parts.scheme.lower() != "https":
        raise ValueError("Skills are fetched only over https: give a GitHub link or an https link to a zip.")
    try:
        urls.check_url(text)
    except urls.Refused as e:
        raise ValueError(str(e)) from None
    host = (parts.hostname or "").lower()
    if host in _GITHUB:
        segments = [s for s in parts.path.split("/") if s]
        if len(segments) < 2:
            raise ValueError("That GitHub link doesn't name a repo.")
        return _github(segments[0], segments[1], segments[2:], f"github.com{parts.path.rstrip('/')}")
    if parts.path.lower().endswith(".zip"):
        return Source(text, [(text, "")])
    raise ValueError("Skills come from a GitHub repo, a folder in one, or an https link to a .zip.")


async def _guard(request: httpx.Request) -> None:
    """httpx hook for every request incl. redirects: https only, no local addresses."""
    if request.url.scheme != "https":
        raise urls.Refused("A redirect left https.")
    urls.check_url(str(request.url))
    await urls.address(request.url.host, request.url.port or 443)


class NotFound(Exception):
    pass


async def download(url: str, transport: httpx.AsyncBaseTransport | None = None) -> bytes:
    """Download up to MAX_DOWNLOAD bytes. Raises NotFound on 404, ValueError on other failures."""
    hooks = {"request": [_guard]} if transport is None else {}
    async with httpx.AsyncClient(follow_redirects=True, timeout=TIMEOUT, transport=transport, event_hooks=hooks,
                                 max_redirects=5, headers={"User-Agent": "JARVIS skills"}) as client:
        try:
            async with client.stream("GET", url) as r:
                if r.status_code == 404:
                    raise NotFound(url)
                if r.status_code >= 400:
                    raise ValueError(f"The download failed: {r.status_code} from {r.url.host}.")
                data = bytearray()
                async for chunk in r.aiter_bytes():
                    data += chunk
                    if len(data) > MAX_DOWNLOAD:
                        raise ValueError(f"The download is over {MAX_DOWNLOAD // 2**20} MB, too big for a skill.")
                return bytes(data)
        except urls.Refused as e:
            raise ValueError(str(e)) from None
        except httpx.HTTPError as e:
            raise ValueError(f"The download failed: {type(e).__name__}.") from None


def _top(names: list[str]) -> str:
    """Common top folder with trailing slash (GitHub's repo-branch/), or ''."""
    tops = {n.split("/", 1)[0] for n in names}
    return f"{tops.pop()}/" if len(tops) == 1 and all("/" in n for n in names) else ""


def unpack(data: bytes, into: Path, inside: str = "") -> tuple[int, list[str]]:
    """Unpack folder `inside` ('' = all) into `into`. Returns (file count, skipped symlinks). Raises ValueError if not
    a zip, too big, or a path escapes `into`."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise ValueError("What was downloaded isn't a zip archive.") from None
    inside = inside.strip("/")
    count, total, left_out = 0, 0, []
    with archive:
        infos = archive.infolist()
        strip = _top([i.filename for i in infos])
        for info in infos:
            name = info.filename.removeprefix(strip)
            if inside:
                if not name.startswith(inside + "/"):
                    continue
                name = name[len(inside) + 1:]
            if info.is_dir() or not name:
                continue
            parts = PurePosixPath(name).parts
            if name.startswith("/") or "\\" in name or ".." in parts or ":" in parts[0]:
                raise ValueError(f"The archive holds a file that would land outside its folder ({info.filename}).")
            if any(p in SKIPPED for p in parts):
                continue
            mode = info.external_attr >> 16
            if stat.S_ISLNK(mode):
                left_out.append(name)
                continue
            count += 1
            total += info.file_size
            if count > MAX_FILES or total > MAX_UNPACKED:
                raise ValueError(f"It's too big for a skill: over {MAX_FILES} files or {MAX_UNPACKED // 2**20} MB.")
            target = into / name
            target.parent.mkdir(parents=True, exist_ok=True)
            written = 0
            with archive.open(info) as src, open(target, "xb") as dst:
                while chunk := src.read(65536):
                    written += len(chunk)
                    if written > info.file_size or total - info.file_size + written > MAX_UNPACKED:
                        raise ValueError("The archive's sizes don't add up; it wasn't unpacked.")
                    dst.write(chunk)
            os.chmod(target, 0o755 if mode & 0o111 else 0o644)
    if not count:
        raise ValueError(f"There's nothing{f' in {inside}' if inside else ''} in that archive.")
    return count, left_out


def find(root: Path) -> list[Path]:
    """Skill folders: `root` if it has SKILL.md, else each outermost folder with one."""
    if (root / home.SKILL_FILE).is_file():
        return [root]
    found: list[Path] = []
    for where, dirs, names in os.walk(root):
        depth = len(Path(where).relative_to(root).parts)
        dirs[:] = sorted(d for d in dirs if not d.startswith(".") and depth < MAX_DEPTH)
        if home.SKILL_FILE in names and Path(where) != root:
            found.append(Path(where))
            dirs[:] = []  # don't look for skills nested inside a skill
    return found


def describe(where: Path, rel: str, left_out: list[str]) -> Found:
    """Summarise a fetched skill and anything that blocks installing it."""
    meta: dict = {}
    problems: list[str] = []
    try:
        meta, _ = home.split((where / home.SKILL_FILE).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        problems.append("its SKILL.md can't be read as text")
    except ValueError as e:
        problems.append(str(e))
    named = str(meta.get("name") or "").strip() or where.name
    name = home.as_name(named)
    if not home.NAME.fullmatch(name):
        problems.append(f"'{named}' can't be made into a skill's name")
    description = home.one_line(meta.get("description"))
    if not description:
        problems.append("it has no description")
    links = [f for f in left_out if f == rel or not rel or f.startswith(rel + "/")]
    if links:
        problems.append(f"it holds links ({', '.join(links[:3])}), which could point anywhere on the computer")
    listed = home.files(where)
    return Found(rel, name, description, listed, home.scripts(where), [str(k) for k in meta if k not in home.KEPT],
                 problems, review.scan(where))


async def stage(text: str, transport: httpx.AsyncBaseTransport | None = None) -> Staged:
    """Fetch `text` into a new stage and find its skills. Raises ValueError if the fetch fails or finds none."""
    source = parse(text)
    clear_old()
    sid = secrets.token_hex(4)
    root = staging() / sid / "files"
    root.mkdir(parents=True)
    left_out: list[str] = []
    try:
        for i, (url, inside) in enumerate(source.tries):
            try:
                data = await download(url, transport)
            except NotFound:
                if i == len(source.tries) - 1:
                    raise ValueError(f"Nothing was found at {source.shown}: check the link, and that the repo is "
                                     "public.") from None
                continue
            _, left_out = unpack(data, root, inside)
            break
        skills = [describe(where, where.relative_to(root).as_posix() if where != root else "", left_out)
                  for where in find(root)]
        if not skills:
            raise ValueError(f"There's no skill at {source.shown}: no folder with a {home.SKILL_FILE} in it.")
    except BaseException:
        shutil.rmtree(staging() / sid, ignore_errors=True)
        raise
    log.info("staged %s from %s: %s", sid, source.shown, ", ".join(s.name for s in skills))
    return Staged(sid, source.shown, root, skills, left_out)


def staged_skill(sid: str, rel: str) -> Path:
    """Path of a staged skill, confined to its stage. Raises ValueError if the stage is gone."""
    if not re.fullmatch(r"[0-9a-f]{8}", sid):
        raise ValueError("That fetch isn't one of JARVIS's.")
    root = (staging() / sid / "files").resolve()
    where = (root / rel).resolve() if rel else root
    if not where.is_relative_to(root) or not (where / home.SKILL_FILE).is_file():
        raise ValueError("That fetch has been cleared away; fetch the skill again.")
    return where


def install(sid: str, rel: str, name: str, origin: str) -> home.Skill:
    """Sanitize and rename the staged SKILL.md, install it as `name`, then delete the stage."""
    where = staged_skill(sid, rel)
    skill_md = where / home.SKILL_FILE
    text, _ = home.sanitize(skill_md.read_text(encoding="utf-8"), name)
    skill_md.write_text(text, encoding="utf-8")
    try:
        return home.install(where, name, origin)
    finally:
        shutil.rmtree(staging() / sid, ignore_errors=True)
