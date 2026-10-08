"""Fetching skills from elsewhere: a GitHub repo, a folder in one, or a link to a zip.

`stage` downloads the archive into a staging folder (data/skills/.staging/<id>), which no session ever loads. It unpacks
only the part asked for, refusing anything that would land outside the folder (absolute paths, ..), leaving out links,
and stopping at MAX_DOWNLOAD, MAX_FILES and MAX_UNPACKED. Then it finds the skills in it (each folder with a SKILL.md)
and says what each holds: its files, which are scripts or other programs, the frontmatter JARVIS takes out, and what a
first scan of the files flags (jarvis.skills.review.scan). Nothing is installed until the user says yes; then
`install` moves the one skill into the skills folder (jarvis.skills.home.install), switched on only if it's
instructions alone. Staged fetches are cleared after STAGING_HOURS.

Only https, and never this Mac or its local network, also after a redirect (the browser's own checks,
jarvis.integrations.browser). GitHub links become codeload.github.com archives, of the default branch unless the link
names one.
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

from jarvis.integrations import browser
from jarvis.skills import home, review

log = logging.getLogger("jarvis.skills.install")

MAX_DOWNLOAD = 25 * 1024 * 1024  # bytes of archive
MAX_UNPACKED = 50 * 1024 * 1024  # bytes of the files unpacked from it
MAX_FILES = 2000
MAX_DEPTH = 6  # folders deep a SKILL.md is looked for
TIMEOUT = 60
STAGING_HOURS = 24
SKIPPED = {".git", "__MACOSX", "node_modules", "__pycache__", ".DS_Store"}  # never unpacked
_GITHUB = {"github.com", "www.github.com"}
_SHORT = re.compile(r"([A-Za-z0-9][\w.-]*)/([\w.-]+)")  # owner/repo


@dataclass
class Source:
    """Where a skill comes from: the link as given, and the archives (with the folder in each) to try in order."""

    shown: str
    tries: list[tuple[str, str]]  # (archive URL, folder inside it, "" for all of it)


@dataclass
class Found:
    """A skill found in a fetch."""

    rel: str  # its folder, relative to the fetch's root ("" for the root itself)
    name: str  # what it would be installed as
    description: str
    files: list[str]
    scripts: list[str]
    dropped: list[str] = field(default_factory=list)  # frontmatter JARVIS takes out
    problems: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)  # what the scan flags

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
    root: Path  # what was unpacked
    skills: list[Found]
    left_out: list[str] = field(default_factory=list)  # links in the archive, never unpacked


def staging() -> Path:
    where = home.home() / ".staging"
    where.mkdir(exist_ok=True)
    return where


def clear_old(now: float | None = None) -> None:
    """Fetches staged more than STAGING_HOURS ago."""
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
    # A branch name can hold slashes (feature/x), so the folder starts after one, two or three parts of it.
    tries = [(archive + quote("/".join(path[:i]), safe=""), "/".join(path[i:])) for i in range(1, min(len(path), 3) + 1)]
    return Source(shown, tries)


def parse(text: str) -> Source:
    """Where to fetch from: owner/repo, a GitHub repo's link or a folder's (…/tree/<branch>/<folder>, or a SKILL.md's
    …/blob/ link), or an https link to a zip. ValueError otherwise."""
    text = text.strip().strip("<>")
    if m := _SHORT.fullmatch(text):
        return _github(m[1], m[2], [], f"github.com/{text}")
    if re.match(r"^(www\.)?github\.com/", text, re.IGNORECASE):
        text = "https://" + text
    parts = urlsplit(text)
    if parts.scheme.lower() != "https":
        raise ValueError("Skills are fetched only over https: give a GitHub link or an https link to a zip.")
    try:
        browser.check_url(text)
    except browser.Refused as e:
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
    """Each request, redirects included: only https, never this Mac or its network."""
    if request.url.scheme != "https":
        raise browser.Refused("A redirect left https.")
    browser.check_url(str(request.url))
    await browser.address(request.url.host, request.url.port or 443)


class NotFound(Exception):
    pass


async def download(url: str, transport: httpx.AsyncBaseTransport | None = None) -> bytes:
    """The archive at `url`, at most MAX_DOWNLOAD bytes. NotFound for a 404; ValueError for anything else wrong."""
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
        except browser.Refused as e:
            raise ValueError(str(e)) from None
        except httpx.HTTPError as e:
            raise ValueError(f"The download failed: {type(e).__name__}.") from None


def _top(names: list[str]) -> str:
    """The one folder everything in an archive sits in (GitHub's repo-branch/), with its slash; '' if there isn't one."""
    tops = {n.split("/", 1)[0] for n in names}
    return f"{tops.pop()}/" if len(tops) == 1 and all("/" in n for n in names) else ""


def unpack(data: bytes, into: Path, inside: str = "") -> tuple[int, list[str]]:
    """Unpack the archive's folder `inside` ('' for all of it) into `into`. Returns how many files, and the links it
    left out. ValueError for an archive that isn't a zip, is too big, or would write outside `into`."""
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
    """The skill folders under `root`: itself if it has a SKILL.md, else each folder with one (not inside another)."""
    if (root / home.SKILL_FILE).is_file():
        return [root]
    found: list[Path] = []
    for where, dirs, names in os.walk(root):
        depth = len(Path(where).relative_to(root).parts)
        dirs[:] = sorted(d for d in dirs if not d.startswith(".") and depth < MAX_DEPTH)
        if home.SKILL_FILE in names and Path(where) != root:
            found.append(Path(where))
            dirs[:] = []  # a skill's own subfolders hold its files, not more skills
    return found


def describe(where: Path, rel: str, left_out: list[str]) -> Found:
    """What a fetched skill holds, and what would stop it being installed."""
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
        problems.append(f"it holds links ({', '.join(links[:3])}), which could point anywhere on the Mac")
    listed = home.files(where)
    return Found(rel, name, description, listed, home.scripts(where), [str(k) for k in meta if k not in home.KEPT],
                 problems, review.scan(where))


async def stage(text: str, transport: httpx.AsyncBaseTransport | None = None) -> Staged:
    """Fetch what `text` points at into a new staging folder and find the skills in it. ValueError, saying why, if it
    can't be fetched or holds none."""
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
    """A staged skill's folder, checked to be inside that fetch. ValueError once it's been cleared away."""
    if not re.fullmatch(r"[0-9a-f]{8}", sid):
        raise ValueError("That fetch isn't one of JARVIS's.")
    root = (staging() / sid / "files").resolve()
    where = (root / rel).resolve() if rel else root
    if not where.is_relative_to(root) or not (where / home.SKILL_FILE).is_file():
        raise ValueError("That fetch has been cleared away; fetch the skill again.")
    return where


def install(sid: str, rel: str, name: str, origin: str) -> home.Skill:
    """Install a staged skill as `name`, its frontmatter cleaned (home.sanitize) and named, then clear the fetch."""
    where = staged_skill(sid, rel)
    skill_md = where / home.SKILL_FILE
    text, _ = home.sanitize(skill_md.read_text(encoding="utf-8"), name)
    skill_md.write_text(text, encoding="utf-8")
    try:
        return home.install(where, name, origin)
    finally:
        shutil.rmtree(staging() / sid, ignore_errors=True)
