"""AI Armory (JARVIS's Google and Mac toolsets, a pinned dependency: docs/development.md), from one place. The rest
of JARVIS imports what it uses of AI Armory from here, never ai_armory itself (tests/test_architecture.py holds it to
that), so an AI Armory change, or a private name of it JARVIS leans on, shows up here alone."""

from ai_armory import Tool, ToolSet, all_tools, call, load_many
from ai_armory.toolsets.google import (
    Account,
    GoogleSettings,
    approve,
    auth,
    checks,
    common,
    configure,
    current,
    drafts,
    pending,
)
from ai_armory.toolsets.google.checks import Outcome
from ai_armory.toolsets.google.web import WebSignIns
from ai_armory.toolsets.mac import notify as mac_notify

__all__ = ["Account", "GoogleSettings", "Outcome", "Tool", "ToolSet", "WebSignIns", "all_tools", "approve", "auth",
           "call", "checks", "common", "configure", "current", "drafts", "html_text", "load_many", "mac_notify",
           "pending"]


def html_text(html: str) -> str:
    """An email's HTML as the text gmail_read gives (hidden text dropped). AI Armory keeps it private."""
    from ai_armory.toolsets.google.gmail import _html_text

    return _html_text(html)
