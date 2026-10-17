"""The Claude Agent SDK's names JARVIS uses, from one place. The rest of JARVIS imports them from here, never from
claude_agent_sdk itself (tests/test_architecture.py holds it to that), so an SDK upgrade, or another provider behind
the same names, is a change inside jarvis.llm alone."""

from claude_agent_sdk import (
    TERMINAL_TASK_STATUSES,
    AgentDefinition,
    AssistantMessage,
    CanUseToolShadowedWarning,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ClaudeSDKError,
    HookMatcher,
    PermissionResultAllow,
    PermissionResultDeny,
    RateLimitEvent,
    RateLimitInfo,
    ResultMessage,
    StreamEvent,
    SystemMessage,
    TaskNotificationMessage,
    TaskStartedMessage,
    TaskUpdatedMessage,
    TextBlock,
    ToolAnnotations,
    ToolPermissionContext,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
    create_sdk_mcp_server,
    tool,
)

__all__ = [
    "TERMINAL_TASK_STATUSES", "AgentDefinition", "AssistantMessage", "CanUseToolShadowedWarning", "ClaudeAgentOptions",
    "ClaudeSDKClient", "ClaudeSDKError", "HookMatcher", "PermissionResultAllow", "PermissionResultDeny",
    "RateLimitEvent", "RateLimitInfo", "ResultMessage", "StreamEvent", "SystemMessage", "TaskNotificationMessage",
    "TaskStartedMessage", "TaskUpdatedMessage", "TextBlock", "ToolAnnotations", "ToolPermissionContext",
    "ToolResultBlock", "ToolUseBlock", "UserMessage", "create_sdk_mcp_server", "query", "tool",
]


def query(*, prompt, options):
    """A one-off session's messages (claude_agent_sdk.query, looked up per call so tests can fake it)."""
    import claude_agent_sdk

    return claude_agent_sdk.query(prompt=prompt, options=options)
