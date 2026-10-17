"""Model ids by role, from config/models.toml: code asks for a role ("announce"), config says which model.

A role names a tier ("fast"), another role ("task"), or a model id. The environment variable beside each role in
models.toml (JARVIS_TASK_MODEL, ...) overrides it.
"""

import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from jarvis.config.files import ConfigError

# Role -> the environment variable that overrides it
ENV = {
    "voice": "JARVIS_CLI_MODEL", "voice_quick": "JARVIS_VOICE_QUICK_MODEL", "lookup": "JARVIS_LOOKUP_MODEL",
    "side": "JARVIS_SIDE_MODEL", "consent": "JARVIS_CONSENT_MODEL", "task": "JARVIS_TASK_MODEL",
    "task_search": "JARVIS_TASK_SEARCH_MODEL", "task_helper": "JARVIS_TASK_HELPER_MODEL", "goal": "JARVIS_GOAL_MODEL",
    "announce": "JARVIS_ANNOUNCE_MODEL", "placement": "JARVIS_PLACEMENT_MODEL", "schedule": "JARVIS_SCHEDULE_MODEL",
    "verdict": "JARVIS_VERDICT_MODEL", "command_summary": "JARVIS_COMMAND_SUMMARY_MODEL",
    "command_triage": "JARVIS_COMMAND_TRIAGE_MODEL", "learning": "JARVIS_LEARNING_MODEL",
    "todo_match": "JARVIS_TODO_MATCH_MODEL", "skill_review": "JARVIS_SKILL_REVIEW_MODEL",
}  # injection's override is the injection_model preference (jarvis.state.preferences)


@dataclass(frozen=True)
class Family:
    name: str  # "opus"
    match: str  # found in its model ids
    window: str = ""  # a usage window limiting this family alone, e.g. "seven_day_opus"
    lighter: str = ""  # the model used while that window is used up

    @property
    def spoken(self) -> str:
        return self.name.capitalize()


class Models:
    def __init__(self, table: Mapping[str, Any], env: Mapping[str, str] = os.environ) -> None:
        self.tiers: dict[str, str] = dict(table.get("tiers", {}))
        self._roles: dict[str, str] = dict(table.get("roles", {}))
        self._choices: dict[str, list[str]] = dict(table.get("choices", {}))
        self._env = env
        for tier in ("deep", "balanced", "fast"):
            if not self.tiers.get(tier):
                raise ConfigError(f"models.toml needs a model for the {tier} tier")
        self.families = tuple(
            Family(name, f.get("match", name), f.get("window", ""), self.resolve(f.get("lighter", "")))
            for name, f in table.get("families", {}).items())

    def tier(self, name: str) -> str:
        if name not in self.tiers:
            raise ConfigError(f"models.toml has no tier {name}")
        return self.tiers[name]

    def resolve(self, name: str, seen: frozenset[str] = frozenset()) -> str:
        """A tier, a role or a model id, followed to the model id ("" stays "")."""
        if not name or name in self.tiers.values():
            return name
        if name in self.tiers:
            return self.tiers[name]
        if name in self._roles:
            if name in seen:
                raise ConfigError(f"models.toml roles loop back to {name}")
            return self.role(name, seen | {name})
        return name  # a model id

    def role(self, name: str, seen: frozenset[str] = frozenset()) -> str:
        """The model for a role: its environment variable, else models.toml."""
        if name not in self._roles:
            raise ConfigError(f"models.toml has no role {name}")
        given = self._env.get(ENV[name], "").strip() if name in ENV else ""
        return self.resolve(given or self._roles[name], seen | {name})

    def choices(self, name: str) -> tuple[str, ...]:
        return tuple(self.resolve(c) for c in self._choices.get(name, ()))

    def family(self, model: str) -> Family | None:
        return next((f for f in self.families if f.match in model.lower()), None)
