"""Control panel actions: Skills: installed, reviewed, switched on and off."""

import asyncio

from jarvis.panel.base import GIT_TIMEOUT, HIDDEN, LOG_LINES, SERVICE_NAMES, _id, _text, action, log  # noqa: F401
from jarvis.skills import home as skills, review

# ── Skills ──────────────────────────────────────────────────────

SKILL_TEXT = 60_000  # max SKILL.md chars shown


def _skill(s: skills.Skill) -> dict:
    return s.brief() | {"enabled": s.enabled, "needs_approval": s.needs_approval, "kind": s.source,
                        "created": s.created, "updated": s.updated}


@action
async def skills_(_: dict) -> list[dict]:
    """All skills with their summary fields."""
    return [_skill(s) for s in await asyncio.to_thread(skills.all_skills)]


def _skill_detail(name: str) -> dict:
    s = skills.get(name)
    text = skills.skill_md(s.folder)
    files = [{"path": f, "size": (s.folder / f).lstat().st_size, "script": f in s.scripts} for f in skills.files(s.folder)]
    found = None
    if s.review:
        found = {k: s.review.get(k) for k in ("summary", "risks", "flags", "verdict", "partial", "at", "model")}
        found["current"] = review.current(s) is not None
    return _skill(s) | {"skill_md": text[:SKILL_TEXT], "cut": len(text) > SKILL_TEXT, "files": files, "review": found}


@action
async def skill(body: dict) -> dict:
    """Full skill: SKILL.md, files (scripts marked) and latest review."""
    return await asyncio.to_thread(_skill_detail, _text(body, "name", 100))


def _switch(name: str, on: bool, approve: bool) -> dict:
    s = skills.get(name)
    if on and s.needs_approval:
        if not (approve and (found := review.current(s))):
            raise ValueError(f"{s.name} has scripts that need their review read and approved first.")
        skills.approve(s.name, found["digest"])
    else:
        skills.switch(s.name, on)
    return _skill_detail(s.name)


@action
async def skill_switch(body: dict) -> dict:
    """Enable/disable. Enabling unapproved scripts needs approve: true, sent after the page shows the current review."""
    return await asyncio.to_thread(_switch, _text(body, "name", 100), bool(body.get("on")), bool(body.get("approve")))


@action
async def skill_review(body: dict) -> dict:
    """Run a script review (one model call) for the page to show before approval."""
    name = _text(body, "name", 100)
    await review.run(name)
    return await asyncio.to_thread(_skill_detail, name)


@action
async def skill_delete(body: dict) -> list[dict]:
    """Delete a skill and its folder (the page confirms first)."""
    await asyncio.to_thread(skills.delete, _text(body, "name", 100))
    return await skills_({})
