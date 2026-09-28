"""A project's own Claude Code setup, for the background tasks that work in it.

A code task loads the project's CLAUDE.md, its subagents (.claude/agents), its skills (.claude/skills)
and its slash commands (.claude/commands), so a workflow like video-agent's scout-ideas, which
launches the project's own scout, claim-checker and evaluator agents, runs as it would in Claude Code.

It never loads the project's settings (.claude/settings.json, settings.local.json). Their allow rules
would let tool calls past the task's gate, their sandbox settings and extra directories would widen
the sandbox, their env could point the CLI's login at another server, and their hooks and helper
commands (apiKeyHelper and the like) run outside the sandbox, with the runner's own rights. Claude
Code's lockdown switches (allowManagedPermissionRulesOnly and friends) only count in managed settings,
so the settings can't be loaded and then tamed. The CLI keeps setting_sources=[] and gets the rest
another way:

  CLAUDE.md       the project folder is also an additional directory, and
                  CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD has the CLI read its CLAUDE.md.
  subagents       read here into AgentDefinitions, under their own names. Their hooks, MCP servers and
                  permission modes are dropped, as Claude Code does for plugin agents.
  skills and      a small plugin made for the run, linking to the project's own folders. The CLI names
  commands        what's in it after the plugin: video-agent:scout-ideas.

Each of these files, AGENTS.md too, is scanned for hidden instructions first (jarvis.safety.scan): text that tries to
override the assistant's rules, keep things from the user or send data away, or invisible Unicode. One that looks
tampered with is left out (a CLAUDE.md, by not loading any; a skill or command, by linking the others one by one),
and the task and the user are told (Setup.flagged).

Project hooks don't run, wherever they're declared: settings aren't loaded, agents lose theirs here, and
the session's disableAllHooks turns off those in skills and commands. SDK callback hooks, which carry the
task's own gate and nudges, still run. Skills' and commands' allowed-tools do become allow rules, so the
task worker checks its gate again in a PreToolUse hook, ahead of them (worker.TaskGate).
"""

import json
import logging
import re
import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from claude_agent_sdk import AgentDefinition

from jarvis.safety import scan

log = logging.getLogger("jarvis.tasks.project_claude")

# What a task keeps from an agent's frontmatter. The rest (hooks, mcpServers, permissionMode, memory,
# isolation, background, color) is dropped: it would run commands, add servers or change permissions.
_AGENT_FIELDS = ("tools", "disallowedTools", "model", "skills", "maxTurns", "effort")
_LISTS = {"tools", "disallowedTools", "skills"}
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")  # what can ride in a Skill(name) rule, or name an agent
_FRONTMATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*(?:\n|\Z)(.*)", re.DOTALL)
_LOOSE_LINE = re.compile(r"([A-Za-z][\w-]*):(?:\s+(.*)|$)")
CLAUDE_MD_ENV = {"CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD": "1"}


@dataclass
class Setup:
    """What a task session loads from its project."""

    name: str  # what the skills and commands go under
    agents: dict[str, AgentDefinition] = field(default_factory=dict)
    skills: list[str] = field(default_factory=list)  # plugin-qualified: video-agent:scout-ideas
    plugin: Path | None = None  # the run's plugin, holding the skills and commands
    claude_md: bool = True  # whether its CLAUDE.md may load: not when one looks like hidden instructions
    flagged: list[str] = field(default_factory=list)  # files left out as hidden instructions: 'AGENTS.md (it ...)'


# Read as the project's context: the CLI loads the CLAUDE.md files, and a task may read AGENTS.md on its own
CLAUDE_MD_FILES = ("CLAUDE.md", "CLAUDE.local.md", ".claude/CLAUDE.md")
CONTEXT_FILES = (*CLAUDE_MD_FILES, "AGENTS.md")
MAX_SKILL_FILES = 50  # of a skill's markdown files scanned; one with more is left out


def tampered(path: Path) -> str:
    """Why one of the project's files looks like hidden instructions (jarvis.safety.scan), or '' if it doesn't, or
    can't be read."""
    try:
        found = scan.scan_file(path, scan.INSTRUCTIONS)
    except OSError:
        return ""
    return found.reason() if found.suspicious else ""


def _loose(text: str) -> dict:
    """Frontmatter that isn't valid YAML, read as Claude Code does: one `key: value` per line, the value
    everything after the first colon (so a description may hold colons of its own), indented lines joined on."""
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
    """A markdown file's frontmatter and body, or None if it has none."""
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
    """The project's own subagents, by name, from .claude/agents (subfolders included): those `keep` lets through."""
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
    """The name the project's skills and commands go under, as in video-agent:scout-ideas."""
    return re.sub(r"[^a-z0-9]+", "-", project.lower()).strip("-") or "project"


def _make_plugin(folder: Path, name: str, where: Path, only: dict[str, list[str] | None]) -> None:
    """A plugin at `where` that is just the project's skills and commands folders, linked in, or where some were left
    out (`only` lists the rest, by name), each one kept. No hooks, MCP servers or settings: those would come from files
    a plugin can hold that aren't made here."""
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
        for n in kept:  # a skill is a folder, a command a file
            item = source / n if sub == "skills" else source / f"{n}.md"
            (where / sub / item.name).symlink_to(item, target_is_directory=item.is_dir())


def load(folder: Path, project: str, plugin_dir: Path) -> Setup:
    """The project's subagents, skills and commands, with its plugin made at `plugin_dir` if it has any. Each file
    is scanned for hidden instructions first (jarvis.safety.scan), its CLAUDE.md and AGENTS.md too; any that look
    tampered with are left out, and listed in `flagged` for the user to hear about."""
    flagged: list[str] = []

    def keep(path: Path) -> bool:
        if why := tampered(path):
            flagged.append(f"{path.relative_to(folder)} ({why})")
            log.warning("left out of %s's setup, as hidden instructions: %s: %s", project, path, why)
            return False
        return True

    claude_md = all([keep(folder / f) for f in CLAUDE_MD_FILES if (folder / f).is_file()])  # each is scanned
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
