"""Task tools that fetch whole files and projects from a GitLab connection (MCP server "repo").

A connection's own file reader passes a file through the model's context, which cuts large ones off, and a task can't
clone: it never sees a key. These run in the runner and use the connection's own key, read from the vault here and
sent only to the connection's own address, never shown to the task. They save into the task's downloads folder, where
Read and Grep reach it. They only read (each is one GET), so they need no yes.

  file      one file at a branch, tag or commit, whole
  download  a project, or one folder of it, at a branch, tag or commit, unpacked (an archive: no git history)
"""

import asyncio
import json
import logging
import re
import shutil
import tarfile
import tempfile
from pathlib import Path
from urllib.parse import quote, urljoin

import httpx

from jarvis.auth import vault
from jarvis.integrations import mcp_servers
from jarvis.integrations.mcp_servers import Server
from jarvis.llm.claude import READS
from jarvis.llm.sdk import create_sdk_mcp_server, tool as sdk_tool
from jarvis.tasks.host_tools import task_temp, text

log = logging.getLogger("jarvis.tasks.repo_files")

NAME = "repo"  # its MCP server: tools are mcp__repo__file and mcp__repo__download
TOOLS = [f"mcp__{NAME}__file", f"mcp__{NAME}__download"]
ADDRESS = "GITLAB_API_URL"  # the setting naming a GitLab connection's server (mcp_servers.KNOWN_ADDRESSES)
DEFAULT_API = "https://gitlab.com/api/v4"
KEY_VARS = ("GITLAB_PERSONAL_ACCESS_TOKEN", "GITLAB_TOKEN")  # where GitLab packages take their key
MAX_FILE = 100 * 2**20
MAX_ARCHIVE = 500 * 2**20
MAX_UNPACKED = 2 * 2**30
MAX_FILES = 100_000
REDIRECTS = 5
TIMEOUT = httpx.Timeout(30, read=120)


class Refused(Exception):
    """Why a fetch didn't happen, said to the task."""


def downloads(task_id: int) -> Path:
    """The task's downloads folder, in its temp folder (readable by the task, writable by sandboxed commands)."""
    return task_temp(task_id) / "downloads"


def key_var(server: Server) -> str:
    """The setting holding its key, or ''."""
    return next((v for v in KEY_VARS if v in server.secret_env), "")


def gitlab(servers: list[Server]) -> list[Server]:
    """The GitLab connections among `servers` whose key these tools can use (a stored key, not a sign-in)."""
    return [s for s in servers if ADDRESS in mcp_servers.addresses(s) and key_var(s)]


def api(server: Server) -> str:
    """Its API address, e.g. https://git.example.com/api/v4. Refused if it isn't one keys may be sent to."""
    url = (server.env.get(ADDRESS) or DEFAULT_API).rstrip("/")
    if why := mcp_servers.bad_address(url):
        raise Refused(why)
    return url


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-.")[:80] or "x"


def _size(n: int) -> str:
    return f"{n / 2**20:.1f} MB" if n >= 2**20 else f"{n / 2**10:.0f} KB" if n >= 2**10 else f"{n} bytes"


async def _fetch(server: Server, url: str, into: Path, limit: int) -> int:
    """GET `url` into the file `into`, sending the key only to the connection's own host; returns the size."""
    home = mcp_servers.host_of(api(server))
    key = vault.get(server.secret_env[key_var(server)])
    async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=False) as client:
        for _ in range(REDIRECTS + 1):
            mine = mcp_servers.host_of(url) == home  # a redirect elsewhere (object storage) gets no key
            headers = {"Authorization": f"Bearer {key}"} if mine else {}
            async with client.stream("GET", url, headers=headers) as reply:
                if reply.is_redirect and (to := reply.headers.get("location")):
                    url = urljoin(url, to)
                    continue
                if reply.status_code != 200:
                    said = (await reply.aread())[:300].decode(errors="replace")
                    raise Refused(f"{server.label} said {reply.status_code}: {_message(said)}")
                got = 0
                with into.open("wb") as out:
                    async for chunk in reply.aiter_bytes():
                        got += len(chunk)
                        if got > limit:
                            raise Refused(f"it's over {_size(limit)}, more than these tools fetch.")
                        out.write(chunk)
                return got
    raise Refused(f"{server.label} redirected it too many times.")


def _message(body: str) -> str:
    """GitLab's error message from a reply body."""
    try:
        found = json.loads(body)
        body = str(found.get("message") or found.get("error") or body) if isinstance(found, dict) else body
    except ValueError:
        pass
    return " ".join(body.split())[:200] or "no reason given"


def _unpack(archive: Path, into: Path) -> tuple[int, int]:
    """Unpack a GitLab archive into `into`, without its top folder; returns (files unpacked, entries left out).

    tarfile's data filter leaves out absolute paths, '..', links leading outside and device files."""
    files = left_out = 0

    def keep(member: tarfile.TarInfo, path: str) -> tarfile.TarInfo | None:
        nonlocal files, left_out
        try:
            kept = tarfile.data_filter(member, path)
        except tarfile.FilterError:
            left_out += 1
            return None
        files += kept.isfile()
        return kept

    with tarfile.open(archive, "r:gz") as tar:
        members, total = [], 0
        for member in tar:
            _, _, rest = member.name.partition("/")
            if not rest or member.islnk():  # the top folder itself; hard links would name paths under it
                continue
            members.append(member.replace(name=rest, deep=False))
            total += member.size
            if len(members) > MAX_FILES or total > MAX_UNPACKED:
                raise Refused(f"it unpacks to more than {MAX_FILES} files or {_size(MAX_UNPACKED)}; download one "
                              "folder of it with path.")
        tar.extractall(into, members=members, filter=keep)
    return files, left_out


def _fresh(folder: Path, root: Path) -> Path:
    """`folder` emptied for a new download; it must lie inside `root`."""
    if not folder.resolve().is_relative_to(root.resolve()) or folder.resolve() == root.resolve():
        raise Refused("that path leads outside the downloads folder.")
    if folder.is_dir() and not folder.is_symlink():
        shutil.rmtree(folder)
    elif folder.exists() or folder.is_symlink():
        folder.unlink()
    folder.mkdir(parents=True)
    return folder


def repo_server(task_id: int, servers: list[Server]):
    """The "repo" MCP server for a task with these GitLab connections."""
    return create_sdk_mcp_server(NAME, tools=_tools(task_id, {s.name: s for s in servers}))


def _tools(task_id: int, servers: dict[str, Server]) -> list:
    root = downloads(task_id)
    names = ", ".join(f"{s.name} ({s.label})" for s in servers.values())

    def pick(args: dict) -> Server:
        name = str(args.get("connection") or "").strip()
        if not name and len(servers) == 1:
            return next(iter(servers.values()))
        if name not in servers:
            raise Refused(f"Name the connection: one of {names}.")
        return servers[name]

    def where(args: dict) -> tuple[str, str]:
        project, ref = str(args.get("project") or "").strip(), str(args.get("ref") or "").strip()
        if not project or not ref:
            raise Refused("Give the project (its path, like group/name, or its id) and ref (a branch, tag or commit).")
        return project, ref

    connection = {"type": "string", "description": f"The connection: {names}. Optional when there's only one."}
    project = {"type": "string", "description": "The project's path (group/name) or numeric id."}
    ref = {"type": "string", "description": "A branch, tag or commit sha, e.g. a merge request's head sha "
                                            "(diff_refs.head_sha)."}

    @sdk_tool(
        "file",
        "Save one file of a GitLab project, whole however long, at a branch, tag or commit, into your downloads "
        f"folder ({root}), then read it with Read (offset and limit for long ones) or Grep. Use it when the "
        "connection's own file reader cuts a file off.",
        {"type": "object", "properties": {"connection": connection, "project": project, "ref": ref,
                                          "path": {"type": "string", "description": "The file's path in the project."}},
         "required": ["project", "ref", "path"]},
        READS,
    )
    async def file(args: dict) -> dict:
        try:
            server, (name, at) = pick(args), where(args)
            path = str(args.get("path") or "").strip().lstrip("/")
            if not path:
                raise Refused("Give the file's path in the project.")
            folder = root / f"{_slug(name)}@{_slug(at)}"
            dest = folder / path
            if not dest.resolve().is_relative_to(folder.resolve()):
                raise Refused("that path leads outside the downloads folder.")
            dest.parent.mkdir(parents=True, exist_ok=True)
            url = f"{api(server)}/projects/{quote(name, safe='')}/repository/files/{quote(path, safe='')}/raw?ref={quote(at, safe='')}"
            size = await _fetch(server, url, dest, MAX_FILE)
        except (Refused, vault.VaultError, httpx.HTTPError, OSError) as e:
            return text(f"Not fetched: {e}", error=True)
        log.info("task %s fetched %s from %s at %s (%s)", task_id, path, name, at, _size(size))
        return text(f"Saved {path} ({_size(size)}) as {dest}. Read it with Read, or search it with Grep.")

    @sdk_tool(
        "download",
        "Download a GitLab project at a branch, tag or commit (or just one folder of it, with path) and unpack it in "
        f"your downloads folder ({root}), to read and search with Read, Glob and Grep, or run with your commands. "
        "It's the files only, without git history: get diffs and history from the connection's own tools.",
        {"type": "object", "properties": {"connection": connection, "project": project, "ref": ref,
                                          "path": {"type": "string", "description": "Optional: one folder of the "
                                                   "project to download instead of all of it."}},
         "required": ["project", "ref"]},
        READS,
    )
    async def download(args: dict) -> dict:
        try:
            server, (name, at) = pick(args), where(args)
            path = str(args.get("path") or "").strip().strip("/")
            root.mkdir(parents=True, exist_ok=True)
            folder = await asyncio.to_thread(  # emptying an earlier download can take a while
                _fresh, root / (f"{_slug(name)}@{_slug(at)}" + (f"~{_slug(path)}" if path else "")), root)
            url = (f"{api(server)}/projects/{quote(name, safe='')}/repository/archive.tar.gz?sha={quote(at, safe='')}"
                   + (f"&path={quote(path, safe='')}" if path else ""))
            with tempfile.TemporaryDirectory(dir=root) as temp:
                archive = Path(temp) / "archive.tar.gz"
                size = await _fetch(server, url, archive, MAX_ARCHIVE)
                count, left_out = await asyncio.to_thread(_unpack, archive, folder)
        except (Refused, vault.VaultError, httpx.HTTPError, OSError, tarfile.TarError) as e:
            return text(f"Not downloaded: {e}", error=True)
        log.info("task %s downloaded %s at %s (%s, %d files)", task_id, name, at, _size(size), count)
        skipped = f" Left out {left_out} entries leading outside it (links or paths)." if left_out else ""
        return text(f"Unpacked {count} files of {name} at {at} into {folder}.{skipped} Read and search them there.")

    return [file, download]
