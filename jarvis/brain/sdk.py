"""What the voice brain and the background tasks share on the Claude Agent SDK."""

import asyncio
import json
import logging
import time
from contextlib import aclosing

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    RateLimitEvent,
    RateLimitInfo,
    ToolAnnotations,
    create_sdk_mcp_server,
    tool as sdk_tool,
)

from jarvis import tools
from jarvis.brain import retry, usage
from jarvis.core.config import (
    CLAUDE_CLI,
    CLAUDE_CONFIG_DIR,
    DATA_DIR,
    INJECTION_TASK_SECONDS,
    TOOL_SEARCH,
    injection_check_on,
)
from jarvis.safety import injection

log = logging.getLogger("jarvis.brain.sdk")
TOOL_SEARCH_TOOL = "ToolSearch"  # the CLI's own tool that finds deferred tools and loads them

# Claude Code's built-in subagents, which inherit every tool the session has. Sessions that
# allow the Agent tool deny these, so only the subagents JARVIS defines (with narrower tools) run.
# The bundled CLI still reports that tool as "Task", its old name, so rules name it both ways.
BUILTIN_AGENTS_DENIED = [f"{tool}({a})" for tool in ("Agent", "Task")
                         for a in ("general-purpose", "claude", "Explore", "Plan", "statusline-setup", "claude-code-guide")]

# The largest single message the SDK will read from the CLI. Its 1 MB default killed tasks with CLIJSONDecodeError
# when one message (a large tool result, say) ran past it.
MAX_BUFFER_SIZE = 20 * 1024 * 1024
ASK_SECONDS = 120  # the longest one short call (`ask`) may take, unless its caller says otherwise
# A tool marked so only reads: the CLI runs such calls of one step side by side, and any other one by one
READS = ToolAnnotations(readOnlyHint=True)


def _sdk_tool(t: tools.Tool):
    async def handler(args: dict) -> dict:
        log.info("tool %s %s", t.name, args)
        content, is_error = await tools.run(t.name, args)
        blocks = content if isinstance(content, list) else [{"type": "text", "text": content}]  # pictures too
        return {"content": blocks, "is_error": is_error}

    return sdk_tool(t.name, t.description, tools.input_schema(t), READS if t.reads else None)(handler)


def jarvis_server(names: list[str] | None = None) -> tuple[dict, list[str]]:
    """JARVIS's tools (or just `names`) as an MCP server, plus their allowed_tools entries. Gated tools
    (chat_send, docs_delete, ...) and those that may carry a shell command (schedule_add) aren't allowed outright, so
    each call meets the session's can_use_tool gate. Tools switched off in the settings (the browser's) are left out."""
    chosen = [t for t in tools.all_tools() if (names is None or t.name in names) and t.on()]
    server = create_sdk_mcp_server("jarvis", tools=[_sdk_tool(t) for t in chosen])
    return server, [f"mcp__jarvis__{t.name}" for t in chosen if not (t.gated or t.shell)]


def on_demand(options: dict) -> dict:
    """`options` with connections' tools loaded on demand. JARVIS's own in-process servers (jarvis, shell, task,
    host) always load. A connection marked deferred (alwaysLoad false: jarvis.integrations.mcp_servers.session) has
    only its tool names in context until the model looks one up with the CLI's ToolSearch, which the session then has
    and may call freely: it only loads tools, and a call to one still meets the same allow and deny rules and gate.
    With no deferred connection, every tool loads up front, as before."""
    if not (servers := options.get("mcp_servers")):
        return options
    servers = servers | {name: c | {"alwaysLoad": True} for name, c in servers.items() if c.get("type") == "sdk"}
    if not any(c.get("alwaysLoad") is False for c in servers.values()):
        return options | {"mcp_servers": servers}
    return options | {"mcp_servers": servers, "tools": [*options["tools"], TOOL_SEARCH_TOOL],
                      "allowed_tools": [*options.get("allowed_tools", []), TOOL_SEARCH_TOOL],
                      "env": options["env"] | {"ENABLE_TOOL_SEARCH": TOOL_SEARCH}}


def sdk_options(env: dict[str, str] | None = None, screen_seconds: float = INJECTION_TASK_SECONDS,
                **options) -> ClaudeAgentOptions:
    """CLI options on JARVIS's own login, with no user or project settings or other MCP servers, and no skills
    unless `skills` names some. Settings stay out because their allow rules would bypass permission_mode and
    can_use_tool (tasks get a project's agents and skills without them: jarvis.tasks.project_claude). Connections'
    tools load on demand (`on_demand`). A session with tools has their results screened for hidden instructions
    while that check is on, each check given `screen_seconds` (jarvis.safety.injection)."""
    options = on_demand(options | {"env": {"CLAUDE_CONFIG_DIR": CLAUDE_CONFIG_DIR} | (env or {})})
    if (options.get("tools") or options.get("mcp_servers")) and injection_check_on():
        options["hooks"] = injection.with_screening(options.get("hooks"), screen_seconds)
    return ClaudeAgentOptions(
        cli_path=CLAUDE_CLI or None,
        strict_mcp_config=True,
        setting_sources=[],
        **{"skills": [], "max_buffer_size": MAX_BUFFER_SIZE} | options,
    )


def short_options(system_prompt: str, model: str, **options) -> ClaudeAgentOptions:
    """Options for one short call with no tools (ask, and jarvis.safety.consent's standby session). A lighter model
    stands in while the model's own limit is used up (jarvis.brain.usage)."""
    return sdk_options(model=usage.available(model), system_prompt=system_prompt, tools=[], allowed_tools=[],
                       max_turns=1, cwd=str(DATA_DIR), **options)


async def ask(prompt: str, system_prompt: str, model: str, timeout: float = ASK_SECONDS,
              **options) -> tuple[str, float]:
    """One short call with no tools, whose reply is only read: the reply and what it cost. retry.CallFailed (a
    RuntimeError) if the call fails, with the kind of failure (jarvis.brain.retry); TimeoutError if it takes over
    `timeout` seconds. `options` go to sdk_options (effort, say)."""
    import claude_agent_sdk  # looked up at the call, so a test's fake query is used

    options = short_options(system_prompt, model, **options)
    reply, cost, rejected, error = "", 0.0, False, None
    async with asyncio.timeout(timeout), aclosing(claude_agent_sdk.query(prompt=prompt, options=options)) as messages:
        async for m in messages:
            if isinstance(m, RateLimitEvent):
                usage.record(m.rate_limit_info)
                rejected = m.rate_limit_info.status == "rejected"
            elif isinstance(m, AssistantMessage):
                error = m.error or error
            elif isinstance(m, claude_agent_sdk.ResultMessage):
                cost = m.total_cost_usd or 0.0
                if m.is_error:
                    kind = retry.result_kind(m.api_error_status, m.terminal_reason, rejected, error)
                    raise retry.CallFailed(f"the model call failed: {m.subtype}", kind)
                reply = m.result or ""
    return reply, cost


def json_reply(reply: str, schema: dict) -> dict | None:
    """The JSON object a reply holds, if it matches `schema`: the whole reply, or failing that the part from its first {
    to its last (a reply in a code fence, or with a word before it); None if there's none, or it doesn't match."""
    from jsonschema import Draft202012Validator

    text = (reply or "").strip()
    for candidate in (text, text[text.find("{"):text.rfind("}") + 1]):
        try:
            value = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(value, dict) and Draft202012Validator(schema).is_valid(value):
            return value
    return None


async def ask_json(prompt: str, schema: dict, system_prompt: str, model: str, **options) -> tuple[dict | None, float]:
    """One short call (`ask`) whose reply is a JSON object matching `schema`, a JSON schema put to the model in its
    system prompt: the object and what the call cost. A reply that isn't such an object is None, for the caller's
    fallback, and logged without its words (they may quote the user's data). RuntimeError if the call fails."""
    system_prompt += ("\n\nReply with only a JSON object, with nothing before or after it, that matches this JSON "
                      f"schema:\n{json.dumps(schema)}")
    reply, cost = await ask(prompt, system_prompt, model, **options)
    if (value := json_reply(reply, schema)) is None:
        log.warning("a %s reply wasn't JSON matching its schema (%d characters)", model, len(reply))
    return value, cost


async def ask_flag(prompt: str, field: str, system_prompt: str, model: str, timeout: float = 60,
                   **options) -> bool | None:
    """One short call (`ask_json`) answering a yes-or-no question as the JSON boolean `field`: the answer, or None if
    the call fails, takes over `timeout` seconds or its reply isn't usable (logged by the error's type alone)."""
    schema = {"type": "object", "properties": {field: {"type": "boolean"}}, "required": [field]}
    try:
        found, _ = await ask_json(prompt, schema, system_prompt, model, timeout=timeout, **options)
    except Exception as e:
        log.warning("a yes-or-no call for %s failed: %s", field, type(e).__name__)
        return None
    return found[field] if found else None


class LimitWatch:
    """Notices the Claude usage limit from the CLI's rate-limit events, or failing that the error text. Every event is
    recorded for the other processes too (jarvis.brain.usage)."""

    def __init__(self, label: str) -> None:
        self.label = label
        self.rejected: RateLimitInfo | None = None

    def saw(self, event: RateLimitEvent) -> None:
        info = event.rate_limit_info
        usage.record(info)
        if info.status != "allowed":
            log.warning("%s rate limit: %s", self.label, info)
        self.rejected = info if info.status == "rejected" else None

    def describe(self, error: str | None) -> str | None:
        """'the Claude usage limit, which resets at 3 PM' if that's why the response failed, else None."""
        if self.rejected and self.rejected.resets_at and self.rejected.resets_at < time.time():
            self.rejected = None  # the CLI only reports changes, so a long-lived watch can outlast the window
        if self.rejected is None and not usage.LIMIT_TEXT.search(error or ""):
            return None
        if not (self.rejected and self.rejected.resets_at):
            return "the Claude usage limit"
        return f"the Claude usage limit, which resets {usage.when(self.rejected.resets_at)}"
