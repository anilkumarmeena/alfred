"""Tool registry.

Tools are plain functions (sync or async) registered with @tool, or AI Armory tool sets via register_toolsets.
A tool with `enabled` is offered only while on. The brain serves `all_tools()` as an MCP server and calls `run()`.
Results are text, JSON-able values, or `WithPictures` for images.
"""

import asyncio
import base64
import inspect
import json
import logging
from dataclasses import dataclass
from collections.abc import Iterable
from typing import Any, Callable

import ai_armory

from jarvis.tasks.client import RunnerUnavailable

log = logging.getLogger("jarvis.tools")


@dataclass
class Tool:
    name: str
    description: str
    properties: dict[str, dict]
    required: list[str]
    fn: Callable[..., Any]
    gated: bool = False  # runs only after the user's spoken yes
    armory: ai_armory.Tool | None = None  # validates its own input and fills defaults
    # Listed in the panel's bell after running: as `said` (filled from args), else by its result
    changes: bool = False
    said: str = ""
    enabled: Callable[[], bool] | None = None  # checked at session start and on each call
    # May carry a shell command, so each call goes through the shell gate instead of being allowed outright
    shell: bool = False
    # Read-only (MCP readOnlyHint): the CLI may run it in parallel with other calls in the same step
    reads: bool = False
    before: Callable[[], None] | None = None  # AI Armory: refresh tool-set settings before each call
    # Answers at once or starts its own background work: never carried on outside the voice turn (jarvis.brain.kept)
    stays: bool = False
    # What of its result the injection check screens: all of it (outside content, the default, so new tools are
    # covered), none (False: JARVIS's own data), or just the result's JSON fields named (task-written text; a reply
    # that isn't JSON, such as an error, is screened whole)
    screened: bool | tuple[str, ...] = True

    def on(self) -> bool:
        return self.enabled is None or self.enabled()


@dataclass
class WithPictures:
    """A tool result plus images, each (media type, bytes)."""
    result: Any
    pictures: list[tuple[str, bytes]]


_REGISTRY: dict[str, Tool] = {}


def tool(name: str, description: str, properties: dict[str, dict] | None = None, required: list[str] | None = None,
         gated: bool = False, changes: bool = False, said: str = "", enabled: Callable[[], bool] | None = None,
         shell: bool = False, reads: bool = False, stays: bool = False, screened: bool | tuple[str, ...] = True):
    def register(fn: Callable[..., Any]) -> Callable[..., Any]:
        _REGISTRY[name] = Tool(name, description, properties or {}, required or [], fn, gated,
                               changes=changes or bool(said), said=said, enabled=enabled, shell=shell, reads=reads,
                               stays=stays, screened=screened)
        return fn

    return register


def register_toolsets(toolsets: list[ai_armory.ToolSet], notes: dict[str, str] | None = None,
                      before: Callable[[], None] | None = None) -> None:
    """Register AI Armory tools: needs_confirmation -> gated, not read_only -> changes, else reads.

    `notes` appends JARVIS guidance to a description; `before` runs ahead of each call.
    """
    for t in ai_armory.all_tools(toolsets):
        schema = t.input_schema
        description = f"{t.description} {notes[t.name]}" if notes and t.name in notes else t.description
        _REGISTRY[t.name] = Tool(t.name, description, schema.get("properties", {}), schema.get("required", []),
                                 t.handler, t.needs_confirmation, t, changes=not t.read_only,
                                 reads=t.read_only and not t.needs_confirmation, before=before)


def unregister(names: Iterable[str]) -> None:
    """Drop tools, e.g. a tool set's before registering it again for changed accounts."""
    for name in list(names):
        _REGISTRY.pop(name, None)


def get(name: str) -> Tool | None:
    return _REGISTRY.get(name)


def all_tools() -> list[Tool]:
    return sorted(_REGISTRY.values(), key=lambda t: t.name)


def input_schema(t: Tool) -> dict:
    if t.armory:
        return t.armory.input_schema
    return {"type": "object", "properties": t.properties, "required": t.required, "additionalProperties": False}


_TYPES = {"string": str, "integer": int, "number": (int, float), "boolean": bool, "array": list, "object": dict}


def _validate(t: Tool, args: Any) -> str | None:
    """Check args against the schema; an error message or None."""
    if not isinstance(args, dict):
        return "input must be an object"
    for key in t.required:
        if key not in args:
            return f"missing required field '{key}'"
    for key, value in args.items():
        spec = t.properties.get(key)
        if spec is None:
            return f"unknown field '{key}'"
        expected = _TYPES.get(spec.get("type", ""))
        if expected and (not isinstance(value, expected) or (spec["type"] == "integer" and isinstance(value, bool))):
            return f"field '{key}' must be {spec['type']}"
        if "enum" in spec and value not in spec["enum"]:
            return f"field '{key}' must be one of {spec['enum']}"
    return None


def _prepare(t: Tool, args: Any) -> Any:
    """Fill omitted optional fields from their schema defaults."""
    if not isinstance(args, dict):
        return args
    return {k: spec["default"] for k, spec in t.properties.items()
            if "default" in spec and k not in t.required} | args


def _text(result: Any) -> str:
    return result if isinstance(result, str) else json.dumps(result, default=str, ensure_ascii=False)


async def run(name: str, args: Any) -> tuple[str | list[dict], bool]:
    """Returns (content, is_error); content is text, or MCP content blocks when the result has pictures."""
    t = _REGISTRY.get(name)
    if t is None:
        return f"Unknown tool: {name}", True
    if not t.on():
        return f"{name} is turned off in JARVIS's settings.", True
    if t.armory:
        if not isinstance(args, dict):
            return json.dumps({"INVALID_INPUT": "input must be an object", "input": args}), True
        if t.before:
            t.before()
        # AI Armory validates input itself (and normalizes account names)
        result = await ai_armory.call(t.armory, args)
        if result.is_error:
            log.warning("tool %s failed: %s", name, result.text)
        return result.text, result.is_error
    args = _prepare(t, args)
    if problem := _validate(t, args):
        return json.dumps({"INVALID_INPUT": problem, "input": args}), True
    try:
        if inspect.iscoroutinefunction(t.fn):
            result = await t.fn(**args)
        else:
            result = await asyncio.to_thread(t.fn, **args)
    except RunnerUnavailable as e:  # expected when only the voice side runs: one line, no traceback
        log.warning("tool %s: %s", name, e)
        return f"{type(e).__name__}: {e}", True
    except Exception as e:  # report failures to the model
        log.exception("tool %s failed", name)
        return f"{type(e).__name__}: {e}", True
    if isinstance(result, WithPictures):
        return [{"type": "text", "text": _text(result.result)},
                *({"type": "image", "data": base64.b64encode(data).decode(), "mimeType": mime}
                  for mime, data in result.pictures)], False
    return _text(result), False


from . import skills  # noqa: E402,F401
from . import connections  # noqa: E402,F401

# Importing registers tools; google registers only when an account exists, mac only on macOS, desktop only on Linux.
from . import browser, core, desktop, google, mac, machines, schedules, screen, tasks  # noqa: E402,F401
from . import files  # noqa: E402,F401  (after google: Gmail attachment tools need an account)
from . import audio  # noqa: E402,F401  (after files: uses its document lookup)
from . import side  # noqa: E402,F401
