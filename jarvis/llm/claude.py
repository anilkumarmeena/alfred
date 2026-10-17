"""Talking to Claude through the Agent SDK: the options every session starts from, one-shot calls (ask, ask_json,
ask_flag), PreToolUse hook helpers, and reading rate-limit and sign-in trouble out of a session. The only module
besides jarvis.llm's others that knows the SDK's message types; a call names a Role (jarvis.llm.roles), or a model id.
"""

import asyncio
import json
import logging
import re
import time
from collections.abc import Callable
from contextlib import aclosing

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    HookMatcher,
    RateLimitEvent,
    RateLimitInfo,
    ToolAnnotations,
)

from jarvis.config import ASSISTANT_NAME, CLAUDE_CLI, DATA_DIR, TOOL_SEARCH
from jarvis.core import gpu
from jarvis.llm import claude_logins, retry, usage
from jarvis.llm.roles import Role, model as model_for

log = logging.getLogger("jarvis.llm.claude")
TOOL_SEARCH_TOOL = "ToolSearch"  # CLI tool that loads deferred tools

# Built-in subagents inherit every session tool, so deny them; only JARVIS's narrower subagents run.
# The CLI may report the tool as "Task", so rules name it both ways.
BUILTIN_AGENTS_DENIED = [f"{tool}({a})" for tool in ("Agent", "Task")
                         for a in ("general-purpose", "claude", "Explore", "Plan", "statusline-setup", "claude-code-guide")]

# Max single CLI message; the SDK's 1 MB default fails large tool results with CLIJSONDecodeError
MAX_BUFFER_SIZE = 20 * 1024 * 1024
ASK_SECONDS = 120  # default `ask` timeout
# Commits and PRs credit the assistant; without this the CLI tells the model to credit itself
ATTRIBUTION = {"commit": f"Co-Authored-By: {ASSISTANT_NAME}", "pr": f"Generated with {ASSISTANT_NAME}"}
# Read-only tools: the CLI may run them in parallel within one step
READS = ToolAnnotations(readOnlyHint=True)
# A call that failed because its login isn't signed in (any more): JARVIS passes that login over until signed in again
SIGNED_OUT = re.compile(r"failed to authenticate|oauth session expired|authentication_failed|please run /login|"
                        r"not logged in|invalid api key", re.IGNORECASE)


def deny(reason: str) -> dict:
    """A PreToolUse hook's answer refusing the call, `reason` going to the model."""
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                   "permissionDecisionReason": reason}}


def refusing(refused: Callable[[str, dict], str]) -> list[HookMatcher]:
    """A PreToolUse hook refusing each call for which `refused(tool name, input)` gives a reason."""

    async def hook(data: dict, _tool_use_id, _ctx) -> dict:
        return deny(why) if (why := refused(data.get("tool_name", ""), data.get("tool_input") or {})) else {}

    return [HookMatcher(hooks=[hook])]


def on_demand(options: dict) -> dict:
    """`options` with deferred connections (alwaysLoad false) loaded on demand via ToolSearch.

    In-process (sdk) servers always load. ToolSearch is allowed freely: it only loads tools, and calls still meet the
    same rules and gate. With no deferred connection, everything loads up front.
    """
    if not (servers := options.get("mcp_servers")):
        return options
    servers = servers | {name: c | {"alwaysLoad": True} for name, c in servers.items() if c.get("type") == "sdk"}
    if not any(c.get("alwaysLoad") is False for c in servers.values()):
        return options | {"mcp_servers": servers}
    return options | {"mcp_servers": servers, "tools": [*options["tools"], TOOL_SEARCH_TOOL],
                      "allowed_tools": [*options.get("allowed_tools", []), TOOL_SEARCH_TOOL],
                      "env": options["env"] | {"ENABLE_TOOL_SEARCH": TOOL_SEARCH}}


def sdk_options(env: dict[str, str] | None = None, login: str | None = None, **options) -> ClaudeAgentOptions:
    """CLI options on the Claude login in use (claude_logins), or `login`: no user/project settings, no other MCP
    servers, no skills unless named.

    Settings are excluded because their allow rules would bypass permission_mode and can_use_tool. `settings` (JSON)
    gains ATTRIBUTION, which the CLI reads from flag settings. Sessions with JARVIS's tools add their screening
    (jarvis.brain.sdk).
    """
    signed_in = claude_logins.env(claude_logins.active() if login is None else login)
    env = signed_in | gpu.env(options.get("sandbox")) | (env or {})
    settings = {"attribution": ATTRIBUTION} | json.loads(options.get("settings") or "{}")
    options = on_demand(options | {"env": env, "settings": json.dumps(settings)})
    return ClaudeAgentOptions(
        cli_path=CLAUDE_CLI or None,
        strict_mcp_config=True,
        setting_sources=[],
        **{"skills": [], "max_buffer_size": MAX_BUFFER_SIZE} | options,
    )


def short_options(system_prompt: str, model: Role | str, **options) -> ClaudeAgentOptions:
    """Options for one short tool-less call; falls back to a lighter model while the limit is used up."""
    return sdk_options(model=usage.available(model_for(model)), system_prompt=system_prompt, tools=[], allowed_tools=[],
                       max_turns=1, cwd=str(DATA_DIR), **options)


async def ask(prompt: str, system_prompt: str, model: Role | str, timeout: float = ASK_SECONDS,
              **options) -> tuple[str, float]:
    """One short tool-less call. Returns (reply, cost_usd).

    Raises retry.CallFailed (a RuntimeError, with kind) on an error result, TimeoutError past `timeout`.
    """
    import claude_agent_sdk  # resolved at call time so tests can fake query

    options = short_options(system_prompt, model, **options)
    reply, cost, rejected, error = "", 0.0, False, None
    async with asyncio.timeout(timeout), aclosing(claude_agent_sdk.query(prompt=prompt, options=options)) as messages:
        async for m in messages:
            if isinstance(m, RateLimitEvent):
                record(m.rate_limit_info, options)
                rejected = m.rate_limit_info.status == "rejected"
            elif isinstance(m, AssistantMessage):
                error = m.error or error
            elif isinstance(m, claude_agent_sdk.ResultMessage):
                cost = m.total_cost_usd or 0.0
                if m.is_error:
                    signed_out(claude_logins.of_env(options.env), f"{error} {m.result}")
                    kind = retry.result_kind(m.api_error_status, m.terminal_reason, rejected, error)
                    raise retry.CallFailed(f"the model call failed: {m.subtype}", kind)
                reply = m.result or ""
    return reply, cost


def json_reply(reply: str, schema: dict) -> dict | None:
    """The reply (or its first-{ to last-} span) parsed as a JSON object matching `schema`; else None."""
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


async def ask_json(prompt: str, schema: dict, system_prompt: str, model: Role | str,
                   **options) -> tuple[dict | None, float]:
    """`ask` for a JSON object matching `schema`. Returns (object or None, cost); RuntimeError on failure.

    A non-matching reply is logged by length only, since it may quote user data.
    """
    system_prompt += ("\n\nReply with only a JSON object, with nothing before or after it, that matches this JSON "
                      f"schema:\n{json.dumps(schema)}")
    reply, cost = await ask(prompt, system_prompt, model, **options)
    if (value := json_reply(reply, schema)) is None:
        log.warning("a %s reply wasn't JSON matching its schema (%d characters)", model, len(reply))
    return value, cost


async def ask_flag(prompt: str, field: str, system_prompt: str, model: Role | str, timeout: float = 60,
                   **options) -> bool | None:
    """Yes/no via `ask_json` as boolean `field`; None on failure, timeout or unusable reply."""
    schema = {"type": "object", "properties": {field: {"type": "boolean"}}, "required": [field]}
    try:
        found, _ = await ask_json(prompt, schema, system_prompt, model, timeout=timeout, **options)
    except Exception as e:
        log.warning("a yes-or-no call for %s failed: %s", field, type(e).__name__)
        return None
    return found[field] if found else None


def record(info: RateLimitInfo, options: ClaudeAgentOptions) -> None:
    """Record a rate-limit event for the login the call ran on."""
    usage.record(info, login=claude_logins.of_env(options.env))


def signed_out(login: str | None, error: str | None) -> None:
    """Pass a login over until it's signed in again, if `error` says the call failed to authenticate."""
    if login is not None and SIGNED_OUT.search(error or ""):
        log.warning("the %s Claude login isn't signed in: %s", login or "default", (error or "").strip()[:200])
        claude_logins.note(login, False, "Claude says it isn't signed in any more: sign it in again.")


class LimitWatch:
    """Detect the usage limit from rate-limit events, else error text. Events are recorded for other processes, under
    `login`, the login the session runs on (set once its options are made); a failure to authenticate passes that
    login over."""

    def __init__(self, label: str, login: str | None = None) -> None:
        self.label = label
        self.login = login
        self.rejected: RateLimitInfo | None = None

    def runs_on(self, options: ClaudeAgentOptions) -> ClaudeAgentOptions:
        self.login = claude_logins.of_env(getattr(options, "env", None))
        return options

    def saw(self, event: RateLimitEvent) -> None:
        info = event.rate_limit_info
        usage.record(info, login=self.login)
        if info.status != "allowed":
            log.warning("%s rate limit: %s", self.label, info)
        # Extra usage carries a run on past a rejected window, so that window didn't stop it
        self.rejected = info if info.status == "rejected" and info.overage_status != "allowed" else None

    def describe(self, error: str | None) -> str | None:
        """E.g. 'the Claude usage limit, which resets at 3 PM' if that caused the failure, else None."""
        signed_out(self.login, error)
        if self.rejected and self.rejected.resets_at and self.rejected.resets_at < time.time():
            self.rejected = None  # CLI reports only changes; the window may have passed
        if self.rejected is None and not usage.LIMIT_TEXT.search(error or ""):
            return None
        if not (self.rejected and self.rejected.resets_at):
            return "the Claude usage limit"
        return f"the Claude usage limit, which resets {usage.when(self.rejected.resets_at)}"
