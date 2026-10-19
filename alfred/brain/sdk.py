"""Sessions that carry Alfred's own tools, for the voice brain and background tasks: the tools as an in-process MCP
server, and session options with their results screened for hidden instructions (alfred.gates.injection). Talking
to Claude itself is alfred.llm.claude."""

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING

from alfred import tools
from alfred.config import INJECTION_TASK_SECONDS
from alfred.gates import injection
from alfred.llm import claude
from alfred.llm.claude import READS
from alfred.llm.sdk import ClaudeAgentOptions, create_sdk_mcp_server, tool as sdk_tool
from alfred.state.preferences import injection_check_on

if TYPE_CHECKING:
    from alfred.brain.kept import Kept

log = logging.getLogger("alfred.brain.sdk")


def _sdk_tool(t: tools.Tool, kept: "Kept | None" = None):
    async def handler(args: dict) -> dict:
        log.info("tool %s %s", t.name, args)
        content, is_error = await (kept.run(t, args) if kept is not None else tools.run(t.name, args))
        blocks = content if isinstance(content, list) else [{"type": "text", "text": content}]  # list: may hold images
        return {"content": blocks, "is_error": is_error}

    return sdk_tool(t.name, t.description, tools.input_schema(t), READS if t.reads else None)(handler)


def alfred_server(names: list[str] | Callable[[tools.Tool], bool] | None = None,
                  kept: "Kept | None" = None) -> tuple[dict, list[str]]:
    """Alfred's enabled tools (or just `names`, or those `names` picks) as an MCP server, plus their allowed_tools
    entries. Google accounts changed since the last session are taken first, so the choice sees their tools.

    Gated and shell-carrying tools are not in allowed_tools, so each call goes through can_use_tool. With `kept`
    (the voice brain), slow calls carry on outside the turn.
    """
    from alfred.tools import google

    google.fresh()
    picks = names if callable(names) else (lambda t: names is None or t.name in names)
    chosen = [t for t in tools.all_tools() if picks(t) and t.on()]
    server = create_sdk_mcp_server("alfred", tools=[_sdk_tool(t, kept) for t in chosen])
    return server, [f"mcp__alfred__{t.name}" for t in chosen if not (t.gated or t.shell)]


def sdk_options(env: dict[str, str] | None = None, screen_seconds: float = INJECTION_TASK_SECONDS,
                login: str | None = None, **options) -> ClaudeAgentOptions:
    """claude.sdk_options, plus: with tools and the injection check on, tool results are screened, `screen_seconds`
    per check."""
    if (options.get("tools") or options.get("mcp_servers")) and injection_check_on():
        options["hooks"] = injection.with_screening(options.get("hooks"), screen_seconds)
    return claude.sdk_options(env, login, **options)
