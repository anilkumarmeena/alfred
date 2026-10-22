"""Fetch skills from a GitHub repo, a folder in one, an https zip link, or Claude Code's own skills on this computer.

`stage` downloads into data/skills/.staging/<id> (never loaded by sessions), unpacking only the requested folder:
paths escaping the folder are refused, symlinks left out, size capped (MAX_DOWNLOAD, MAX_FILES, MAX_UNPACKED). It
reports each skill found: files, scripts, frontmatter to be stripped, and review.scan flags. Nothing is installed
until the user says yes; `install` then hands one skill to home.install. Stale stages are cleared after STAGING_HOURS.

https only, never local addresses, redirects included (browser's checks). GitHub links map to codeload.github.com
archives of the default branch unless the link names one.

"claude" copies Claude Code's skills instead (`claude_skills`): the user's own, those synced from their Claude
account, and installed plugins'. They're staged and installed the same way: same limits, links left out, frontmatter
stripped, scripts off until reviewed and approved.
"""

import asyncio
import io
import json
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

from alfred import config
from alfred.safety import urls
from alfred.skills import home, review

log = logging.getLogger("alfred.skills.install")

MAX_DOWNLOAD = 25 * 1024 * 1024  # archive bytes
MAX_UNPACKED = 50 * 1024 * 1024  # unpacked bytes
MAX_FILES = 2000
MAX_DEPTH = 6  # max folder depth searched for SKILL.md
TIMEOUT = 60
STAGING_HOURS = 24
SKIPPED = {".git", "__MACOSX", "node_modules", "__pycache__", ".DS_Store"}  # never unpacked
_GITHUB = {"github.com", "www.github.com"}
_SHORT = re.compile(r"([A-Za-z0-9][\w.-]*)/([\w.-]+)")  # owner/repo
CLAUDE = "Claude Code"  # origin of skills copied from Claude Code's folders
_CLAUDE_SOURCES = {"claude", "claude code", "claude-code", "claude skills", "claude code skills"}


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
                                 max_redirects=5, headers={"User-Agent": "Alfred skills"}) as client:
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
    """Fetch `text` (or copy Claude Code's skills, for "claude") into a new stage and find its skills. Raises
    ValueError if the fetch fails or finds none."""
    if is_claude(text):
        return await asyncio.to_thread(stage_claude)
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
        raise ValueError("That fetch isn't one of Alfred's.")
    root = (staging() / sid / "files").resolve()
    where = (root / rel).resolve() if rel else root
    if not where.is_relative_to(root) or not (where / home.SKILL_FILE).is_file():
        raise ValueError("That fetch has been cleared away; fetch the skill again.")
    return where


def is_claude(text: str) -> bool:
    """Whether `text` asks for Claude Code's own skills rather than a link."""
    return " ".join(text.lower().replace("'s", "").split()) in _CLAUDE_SOURCES


def claude_dirs() -> list[Path]:
    """Claude Code's config folders: the one Alfred's login uses and the user's own (~/.claude), each once."""
    found: list[Path] = []
    for d in (Path(config.CLAUDE_CONFIG_DIR), Path.home() / ".claude"):
        if d.is_dir() and d.resolve() not in {f.resolve() for f in found}:
            found.append(d)
    return found


def _json(path: Path) -> dict:
    """A JSON file's object, or {} if it's missing or isn't one."""
    try:
        found = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return found if isinstance(found, dict) else {}


def _synced(base: Path, kind: str) -> list[Path]:
    """The folder synced from the signed-in Claude account (<kind>/synced/<organization>_<account>), as Claude Code
    picks it: other buckets are left from other sign-ins. Every bucket if the sign-in can't be read."""
    kept = [base / ".claude.json"] + ([Path.home() / ".claude.json"] if base == Path.home() / ".claude" else [])
    signed_in = next((a for f in kept if isinstance(a := _json(f).get("oauthAccount"), dict) and a), {})
    buckets = sorted(d for d in (base / kind / "synced").glob("*") if d.is_dir())
    if not (signed_in.get("organizationUuid") and signed_in.get("accountUuid")):
        return buckets
    return [d for d in buckets if d.name == f"{signed_in['organizationUuid']}_{signed_in['accountUuid']}"]


def _plugin_skills(base: Path) -> list[Path]:
    """Skills folders of the plugins Claude Code has installed and switched on, then of those synced from the Claude
    account that its manifest lists (in <name>~g<generation> for later generations)."""
    on = {k for k, v in (_json(base / "settings.json").get("enabledPlugins") or {}).items() if v}
    found = [Path(i["installPath"]) / "skills"
             for key, installs in (_json(base / "plugins" / "installed_plugins.json").get("plugins") or {}).items()
             if key in on and isinstance(installs, list)
             for i in installs if isinstance(i, dict) and isinstance(i.get("installPath"), str)]
    for bucket in _synced(base, "plugins"):
        for plugin in _json(bucket / "manifest.json").get("plugins") or []:
            if isinstance(plugin, dict) and isinstance(name := plugin.get("name"), str) and "/" not in name:
                found.append(bucket / (f"{name}~g{plugin['generation']}" if plugin.get("generation") else name) / "skills")
    return found


def claude_skills() -> dict[str, Path]:
    """Skill folders Claude Code loads, by folder name: the user's own (skills/), those synced from their Claude
    account, then plugins'. The first of each name wins, as the user's own come first."""
    found: dict[str, Path] = {}
    for base in claude_dirs():
        for group in [base / "skills", *_synced(base, "skills"), *_plugin_skills(base)]:
            if not group.is_dir():
                continue
            for where in sorted(group.iterdir()):
                if not where.name.startswith(".") and where.is_dir() and (where / home.SKILL_FILE).is_file():
                    found.setdefault(where.name, where.resolve())  # a skill linked in by hand is copied from its folder
    return found


def copy(found: dict[str, Path], into: Path) -> list[str]:
    """Copy each skill folder into `into`/<name>, as `unpack` would: SKIPPED folders and links left out (returned, as
    <name>/<path>), executable bits kept. Raises ValueError if they come to more than MAX_FILES or MAX_UNPACKED."""
    count, total, left_out = 0, 0, []
    for name, where in found.items():
        for rel in home.files(where):
            src = where / rel
            if any(p in SKIPPED for p in PurePosixPath(rel).parts):
                continue
            if src.is_symlink():
                left_out.append(f"{name}/{rel}")
                continue
            if not src.is_file():
                continue
            count += 1
            total += (size := src.stat()).st_size
            if count > MAX_FILES or total > MAX_UNPACKED:
                raise ValueError(f"Claude Code's skills are too many to copy at once: over {MAX_FILES} files or "
                                 f"{MAX_UNPACKED // 2**20} MB.")
            target = into / name / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, target)
            os.chmod(target, 0o755 if size.st_mode & 0o111 else 0o644)
    return left_out


def stage_claude() -> Staged:
    """Copy Claude Code's skills into a new stage. Raises ValueError if it has none."""
    found = claude_skills()
    if not found:
        raise ValueError("Claude Code has no skills on this computer: none of its own, synced from the Claude account, "
                         "or in its plugins.")
    clear_old()
    sid = secrets.token_hex(4)
    root = staging() / sid / "files"
    root.mkdir(parents=True)
    try:
        left_out = copy(found, root)
        skills = [describe(root / name, name, left_out) for name in found]
    except BaseException:
        shutil.rmtree(staging() / sid, ignore_errors=True)
        raise
    log.info("staged %s from %s: %s", sid, CLAUDE, ", ".join(s.name for s in skills))
    return Staged(sid, CLAUDE, root, skills, left_out)


def unchanged(sid: str, skill: Found) -> bool:
    """Whether the staged skill, once installed, would be the same as the one of its name already here."""
    where, have = staged_skill(sid, skill.rel), home.folder(skill.name)
    if not (have / home.SKILL_FILE).is_file():
        return False
    try:
        text, _ = home.sanitize((where / home.SKILL_FILE).read_text(encoding="utf-8"), skill.name)
    except (OSError, UnicodeDecodeError, ValueError):
        return False
    return text == home.skill_md(have) and home.digest(where) == home.digest(have)


def install(sid: str, rel: str, name: str, origin: str) -> home.Skill:
    """Sanitize and rename the staged SKILL.md, install it as `name`, then delete the stage."""
    return install_many(sid, [(rel, name)], origin)[0]


def install_many(sid: str, picks: list[tuple[str, str]], origin: str) -> list[home.Skill]:
    """Install each staged (rel, name) as `install` does, then delete the stage."""
    try:
        return [_install(staged_skill(sid, rel), name, origin) for rel, name in picks]
    finally:
        shutil.rmtree(staging() / sid, ignore_errors=True)


def _install(where: Path, name: str, origin: str) -> home.Skill:
    skill_md = where / home.SKILL_FILE
    text, _ = home.sanitize(skill_md.read_text(encoding="utf-8"), name)
    skill_md.write_text(text, encoding="utf-8")
    return home.install(where, name, origin)
