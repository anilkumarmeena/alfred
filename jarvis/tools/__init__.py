"""Tool registry.

A tool is a plain function (sync or async) registered with @tool, or a tool from an AI Armory tool set registered
with register_toolsets (the Google tools: jarvis.tools.google; the Mac tools: jarvis.tools.mac). A tool with a switch
(`enabled`, such as the web browser's: jarvis.tools.browser) is offered only while it's on, as each session starts. The brain serves
`all_tools()` to Claude as an MCP server (jarvis.brain.sdk) and calls `run()` for each call. A tool returns text, or
something sent as JSON; one whose result holds pictures for Claude to look at (a screenshot: jarvis.tools.screen; a
document's: jarvis.tools.files) returns it as `WithPictures`.
"""

import asyncio
import base64
import inspect
import json
import logging
from dataclasses import dataclass
from typing import Any, Callable

import ai_armory

log = logging.getLogger("jarvis.tools")


@dataclass
class Tool:
    name: str
    description: str
    properties: dict[str, dict]
    required: list[str]
    fn: Callable[..., Any]
    gated: bool = False  # runs only after the user's spoken yes, which the brain's gate asks for (draft_gate)
    armory: ai_armory.Tool | None = None  # an AI Armory tool, which checks its own input and fills in its defaults
    # It changes something, so the control panel's bell lists it once it has run (jarvis.state.activity): as `said`,
    # filled in from its arguments, or else as its own result says it
    changes: bool = False
    said: str = ""
    enabled: Callable[[], bool] | None = None  # its setting, read as a session starts (sdk.jarvis_server) and each call
    # Its calls may carry a shell command (a scheduled command job), so each meets the voice shell's gate, which weighs
    # it as one run there (jarvis.safety.shell.ShellGate), rather than being allowed outright
    shell: bool = False
    # It only reads, so the CLI may run it alongside the other calls of the same step (its MCP readOnlyHint, which
    # jarvis.brain.sdk gives it): a reply needing a few lookups waits for the slowest, not their sum
    reads: bool = False
    before: Callable[[], None] | None = None  # an AI Armory tool's: brings its tool set's settings up to date first

    def on(self) -> bool:
        return self.enabled is None or self.enabled()


@dataclass
class WithPictures:
    """A tool's result (text, or anything that becomes JSON) with pictures alongside, each (media type, bytes)."""
    result: Any
    pictures: list[tuple[str, bytes]]


_REGISTRY: dict[str, Tool] = {}


def tool(name: str, description: str, properties: dict[str, dict] | None = None, required: list[str] | None = None,
         gated: bool = False, changes: bool = False, said: str = "", enabled: Callable[[], bool] | None = None,
         shell: bool = False, reads: bool = False):
    def register(fn: Callable[..., Any]) -> Callable[..., Any]:
        _REGISTRY[name] = Tool(name, description, properties or {}, required or [], fn, gated,
                               changes=changes or bool(said), said=said, enabled=enabled, shell=shell, reads=reads)
        return fn

    return register


def register_toolsets(toolsets: list[ai_armory.ToolSet], notes: dict[str, str] | None = None,
                      before: Callable[[], None] | None = None) -> None:
    """AI Armory's tools, under their own names. Those it says need the user's go-ahead are gated, and those it doesn't
    call read-only change something; the rest only read. `notes` adds JARVIS's own guidance to a tool's description,
    after AI Armory's; `before` runs ahead of each call."""
    for t in ai_armory.all_tools(toolsets):
        schema = t.input_schema
        description = f"{t.description} {notes[t.name]}" if notes and t.name in notes else t.description
        _REGISTRY[t.name] = Tool(t.name, description, schema.get("properties", {}), schema.get("required", []),
                                 t.handler, t.needs_confirmation, t, changes=not t.read_only,
                                 reads=t.read_only and not t.needs_confirmation, before=before)


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
    """Check inputs against the schema before running anything."""
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
    """The input with each optional field left out that has a "default" in its schema filled in from it."""
    if not isinstance(args, dict):
        return args
    return {k: spec["default"] for k, spec in t.properties.items()
            if "default" in spec and k not in t.required} | args


def _text(result: Any) -> str:
    return result if isinstance(result, str) else json.dumps(result, default=str, ensure_ascii=False)


async def run(name: str, args: Any) -> tuple[str | list[dict], bool]:
    """Returns (content, is_error). The content is text, or MCP content blocks when the result has pictures."""
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
        # AI Armory checks the input itself, after taking an account named by its email or in other capitals.
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
    except Exception as e:  # tools talk to the outside world; report failures to the model
        log.exception("tool %s failed", name)
        return f"{type(e).__name__}: {e}", True
    if isinstance(result, WithPictures):
        return [{"type": "text", "text": _text(result.result)},
                *({"type": "image", "data": base64.b64encode(data).decode(), "mimeType": mime}
                  for mime, data in result.pictures)], False
    return _text(result), False


from . import skills  # noqa: E402,F401  (skills: jarvis.skills)
from . import connections  # noqa: E402,F401  (MCP servers: jarvis.integrations.mcp_servers)

# Importing the modules registers their tools; google registers the Google tools only when there's an account.
from . import browser, core, google, heartbeat, mac, schedules, screen, tasks  # noqa: E402,F401
from . import files  # noqa: E402,F401  (after google: its Gmail attachment tools need an account)
from . import audio  # noqa: E402,F401  (after files, whose way of finding a document it uses)
from . import side  # noqa: E402,F401  (background_lookup: jarvis.brain.side)
