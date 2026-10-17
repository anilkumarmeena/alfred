"""Load a project's own Claude Code setup (CLAUDE.md, agents, skills, commands) into a code task's session.

Project settings (.claude/settings*.json) are never loaded: allow rules would bypass the task gate, sandbox/dirs
would widen the sandbox, env could redirect login, and hooks/apiKeyHelper run unsandboxed. Lockdown switches only
apply in managed settings, so setting_sources=[] and the rest is loaded explicitly:
  CLAUDE.md          project folder is an additional dir + CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD
  agents             parsed into AgentDefinitions; hooks, MCP servers, permission modes dropped
  skills, commands   a per-run plugin symlinking the project folders (named e.g. video-agent:scout-ideas)
Every file (and AGENTS.md) is scanned for hidden instructions first; tampered ones are left out and reported
(Setup.flagged). No project hooks run (disableAllHooks). Skills' allowed-tools become allow rules, so the worker
re-checks its gate in a PreToolUse hook (worker.TaskGate).
"""

import json
import logging
import re
import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from jarvis.llm.sdk import AgentDefinition
from jarvis.safety import scan

log = logging.getLogger("jarvis.tasks.project_claude")

# Agent frontmatter kept; everything else (hooks, mcpServers, permissionMode, ...) could run commands,
# add servers or change permissions.
_AGENT_FIELDS = ("tools", "disallowedTools", "model", "skills", "maxTurns", "effort")
_LISTS = {"tools", "disallowedTools", "skills"}
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")  # safe in a Skill(name) rule or as an agent name
_FRONTMATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*(?:\n|\Z)(.*)", re.DOTALL)
_LOOSE_LINE = re.compile(r"([A-Za-z][\w-]*):(?:\s+(.*)|$)")
CLAUDE_MD_ENV = {"CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD": "1"}


@dataclass
class Setup:
    """What a task session loads from its project."""

    name: str  # plugin name prefixing skills and commands
    agents: dict[str, AgentDefinition] = field(default_factory=dict)
    skills: list[str] = field(default_factory=list)  # plugin-qualified: video-agent:scout-ideas
    plugin: Path | None = None  # per-run plugin dir
    claude_md: bool = True  # False if any CLAUDE.md looked tampered
    flagged: list[str] = field(default_factory=list)  # 'AGENTS.md (reason)' for files left out


# Project context files: the CLI loads the CLAUDE.md ones; a task may read AGENTS.md itself
CLAUDE_MD_FILES = ("CLAUDE.md", "CLAUDE.local.md", ".claude/CLAUDE.md")
CONTEXT_FILES = (*CLAUDE_MD_FILES, "AGENTS.md")
MAX_SKILL_FILES = 50  # a skill with more .md files than this is left out


def tampered(path: Path) -> str:
    """Why `path` looks like hidden instructions, or '' (also if unreadable)."""
    try:
        found = scan.scan_file(path, scan.INSTRUCTIONS)
    except OSError:
        return ""
    return found.reason() if found.suspicious else ""


def _loose(text: str) -> dict:
    """Parse non-YAML frontmatter like Claude Code: `key: value` per line (value may contain colons), indented lines
    continue the previous value."""
    meta: dict = {}
    key = None
    for line in text.splitlines():
        if (m := _LOOSE_LINE.match(line)) and not line[:1].isspace():
            key, value = m.group(1), (m.group(2) or "").strip()
            meta[key] = int(value) if value.isdigit() else value
        elif key and line.strip():
            meta[key] = f"{meta[key]} {line.strip()}".strip()
    return meta


def _front(path: Path) -> tuple[dict, str] | None:
    """(frontmatter, body) of a markdown file, or None without frontmatter."""
    try:
        m = _FRONTMATTER.match(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as e:
        log.warning("skipping %s: %s", path, e)
        return None
    if not m:
        return None
    try:
        meta = yaml.safe_load(m.group(1))
    except yaml.YAMLError:
        meta = _loose(m.group(1))
    return (meta, m.group(2).strip()) if isinstance(meta, dict) else None


def _as_list(value) -> list[str] | None:
    if isinstance(value, str):
        return [t.strip() for t in value.split(",") if t.strip()]
    if isinstance(value, list):
        return [str(t).strip() for t in value if str(t).strip()]
    return None


def _agent(path: Path) -> tuple[str, AgentDefinition] | None:
    parsed = _front(path)
    if parsed is None:
        return None
    meta, body = parsed
    name = str(meta.get("name") or path.stem).strip()
    description = str(meta.get("description") or "").strip()
    if not _NAME.fullmatch(name) or not description or not body:
        log.warning("skipping agent %s: it needs a plain name, a description and a prompt", path)
        return None
    kept = {}
    for key in _AGENT_FIELDS:
        value = meta.get(key)
        if key in _LISTS:
            value = _as_list(value)
        elif key == "maxTurns" or (key == "effort" and isinstance(value, int)):
            value = value if isinstance(value, int) and not isinstance(value, bool) else None
        elif value is not None:
            value = str(value).strip() or None
        if value is not None:
            kept[key] = value
    return name, AgentDefinition(description=description, prompt=body, **kept)


def agents(folder: Path, keep: Callable[[Path], bool] = lambda path: True) -> dict[str, AgentDefinition]:
    """Project subagents from .claude/agents (recursive), by name, filtered by `keep`."""
    found: dict[str, AgentDefinition] = {}
    root = folder / ".claude" / "agents"
    for path in sorted(root.rglob("*.md")) if root.is_dir() else []:
        if (agent := _agent(path)) and agent[0] not in found and keep(path):
            found[agent[0]] = agent[1]
    return found


def _skills(folder: Path) -> list[str]:
    root = folder / ".claude" / "skills"
    names = [d.name for d in sorted(root.iterdir()) if (d / "SKILL.md").is_file()] if root.is_dir() else []
    return [n for n in names if _NAME.fullmatch(n)]


def _commands(folder: Path) -> list[str]:
    root = folder / ".claude" / "commands"
    names = [p.stem for p in sorted(root.glob("*.md"))] if root.is_dir() else []
    return [n for n in names if _NAME.fullmatch(n)]


def plugin_name(project: str) -> str:
    """Plugin name for a project, e.g. the "video-agent" in video-agent:scout-ideas."""
    return re.sub(r"[^a-z0-9]+", "-", project.lower()).strip("-") or "project"


def _make_plugin(folder: Path, name: str, where: Path, only: dict[str, list[str] | None]) -> None:
    """Create a plugin at `where` symlinking the project's skills and commands.

    Whole folders are linked unless `only[sub]` names the kept items, then each is linked individually.
    Holds no hooks, MCP servers or settings.
    """
    if where.is_symlink() or where.is_file():
        where.unlink()
    elif where.exists():
        shutil.rmtree(where)
    (where / ".claude-plugin").mkdir(parents=True)
    (where / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": name, "description": "The project's own skills and slash commands"}))
    for sub in ("skills", "commands"):
        source = folder / ".claude" / sub
        if not source.is_dir():
            continue
        if (kept := only.get(sub)) is None:
            (where / sub).symlink_to(source, target_is_directory=True)
            continue
        (where / sub).mkdir()
        for n in kept:  # skills are folders, commands files
            item = source / n if sub == "skills" else source / f"{n}.md"
            (where / sub / item.name).symlink_to(item, target_is_directory=item.is_dir())


def load(folder: Path, project: str, plugin_dir: Path) -> Setup:
    """Load the project's agents, skills and commands, building the plugin at `plugin_dir` if needed.

    Tampered files are left out and listed in `flagged`.
    """
    flagged: list[str] = []

    def keep(path: Path) -> bool:
        if why := tampered(path):
            flagged.append(f"{path.relative_to(folder)} ({why})")
            log.warning("left out of %s's setup, as hidden instructions: %s: %s", project, path, why)
            return False
        return True

    claude_md = all([keep(folder / f) for f in CLAUDE_MD_FILES if (folder / f).is_file()])  # list: scan all
    for f in set(CONTEXT_FILES) - set(CLAUDE_MD_FILES):
        if (folder / f).is_file():
            keep(folder / f)
    setup = Setup(name=plugin_name(project), agents=agents(folder, keep), claude_md=claude_md, flagged=flagged)
    found = {"skills": _skills(folder), "commands": _commands(folder)}
    root = folder / ".claude"
    kept = {"skills": [n for n in found["skills"] if (files := sorted((root / "skills" / n).rglob("*.md")))
                       and len(files) <= MAX_SKILL_FILES and all(keep(p) for p in files)],
            "commands": [n for n in found["commands"] if keep(root / "commands" / f"{n}.md")]}
    listed = list(dict.fromkeys(kept["skills"] + kept["commands"]))
    if listed:
        try:
            _make_plugin(folder, setup.name, plugin_dir, {s: None if kept[s] == found[s] else kept[s] for s in kept})
        except OSError as e:
            log.warning("couldn't make %s's plugin at %s: %s", project, plugin_dir, e)
        else:
            setup.plugin, setup.skills = plugin_dir, [f"{setup.name}:{n}" for n in listed]
    return setup
