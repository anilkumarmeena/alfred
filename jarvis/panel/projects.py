"""Control panel actions: The project list."""

import asyncio
from pathlib import Path

from jarvis import projects
from jarvis.node import machines
from jarvis.panel.base import GIT_TIMEOUT, HIDDEN, LOG_LINES, SERVICE_NAMES, _id, _text, action, log  # noqa: F401
from jarvis.tasks import client

# ── Projects ────────────────────────────────────────────────────


async def _changed(folder: Path) -> int | None:
    """Number of uncommitted changes, or None if git fails or times out."""
    try:
        proc = await asyncio.create_subprocess_exec("git", "-C", str(folder), "status", "--porcelain",
                                                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await asyncio.wait_for(proc.communicate(), GIT_TIMEOUT)
    except (OSError, TimeoutError):
        return None
    return len(out.splitlines()) if proc.returncode == 0 else None


async def _project(p: projects.Project) -> dict:
    if p.node:  # its folder is on that machine
        return {"name": p.name, "path": str(p.path), "aliases": p.aliases, "about": p.about, "in_place": p.in_place,
                "jarvis": False, "exists": True, "git": None, "changed": None, "machine": machines.where(str(p.node))}
    state = await asyncio.to_thread(projects.git_state, p.path)
    return {"name": p.name, "path": projects.shown(p.path), "aliases": p.aliases, "about": p.about,
            "in_place": p.in_place, "jarvis": projects.is_jarvis(p.path), "exists": p.path.is_dir(), "git": state,
            "changed": await _changed(p.path) if state == projects.GIT else None}


@action
async def projects_(_: dict) -> list[dict]:
    return list(await asyncio.gather(*(_project(p) for p in await asyncio.to_thread(projects.load))))


def _aliases(body: dict) -> list[str] | None:
    value = body.get("aliases")
    if value is None:
        return None
    if isinstance(value, str):
        value = value.split(",")
    if not isinstance(value, list):
        raise ValueError("aliases must be a list.")
    return [str(a) for a in value]


@action
async def project_save(body: dict) -> list[dict]:
    """Update aliases, about and mode. The folder can't be changed here."""
    in_place = body.get("in_place")
    await asyncio.to_thread(projects.update, _text(body, "name"), None, _aliases(body),
                            _text(body, "about", 300) if "about" in body else None,
                            None if in_place is None else bool(in_place))
    return await projects_({})


@action
async def project_add(body: dict) -> list[dict]:
    """Register a project at an existing folder, or a new one under the projects folder if no path."""
    await client.register_project(_text(body, "name", 60), _text(body, "path", 300) or None, _aliases(body) or [],
                                  _text(body, "about", 300), bool(body.get("in_place", projects.IN_PLACE_DEFAULT)))
    return await projects_({})


@action
async def project_remove(body: dict) -> list[dict]:
    await asyncio.to_thread(projects.remove, _text(body, "name"))
    return await projects_({})


@action
async def project_git(body: dict) -> list[dict]:
    await client.git_init(_text(body, "name"))
    return await projects_({})
