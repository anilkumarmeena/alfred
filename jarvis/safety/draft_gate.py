"""The CLI brain's gate: ShellGate plus a spoken yes for Google sends and deletes.

Gated Google actions are drafted first, then done by a second tool taking only the draft id (DRAFTED_BY). The first
call is refused; at turn end JARVIS reads the drafting tool's own summary (all recipients incl. bcc, exact text) and
asks. Yes/no rules match risky shell commands; the consent model sees only `WHAT` the kind does, never the draft.
A clear yes approves that draft once in AI Armory. Only the latest draft can be approved; a newer one replaces it.
"""

import logging

from ai_armory.toolsets.google import approve, pending
from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

from jarvis import tools
from jarvis.core import profile
from jarvis.safety import consent, shell

log = logging.getLogger("jarvis.safety.draft_gate")

PREFIX = "draft:"  # marks a draft key among the gate's command keys
SEND_QUESTION = "Shall I send it?"
GO_QUESTION = "Shall I go ahead?"
# Sends to other people (the rest are deletes)
SENDS = {"gmail_send", "chat_send", "calendar_send_invite", "calendar_send_update", "calendar_send_response"}
DRAFTED_BY = {"gmail_send": "gmail_draft_send or gmail_draft_reply", "chat_send": "chat_draft",
              "calendar_send_invite": "calendar_draft_invite", "calendar_send_update": "calendar_draft_update",
              "calendar_send_response": "calendar_draft_respond", "calendar_delete_event": "calendar_draft_delete",
              "docs_delete": "docs_draft_delete", "sheets_delete_tab": "sheets_draft_delete_tab"}
# Marked read-only, as nothing leaves before the yes, yet each call replaces the draft awaiting one
DRAFTING = {t for said in DRAFTED_BY.values() for t in said.split(" or ")}
# Description given to the consent model: from the kind, never the draft's text
WHAT = {"gmail_send": "sending an email", "chat_send": "sending a Google Chat message",
        "calendar_send_invite": "sending calendar invites",
        "calendar_send_update": "sending an event's change to its guests",
        "calendar_send_response": "answering a calendar invite", "calendar_delete_event": "deleting a calendar event",
        "docs_delete": "deleting part of a Google Doc", "sheets_delete_tab": "deleting a tab of a Google Sheet"}


def gated() -> set[str]:
    """Full names of JARVIS tools that need a spoken yes."""
    return {f"mcp__jarvis__{t.name}" for t in tools.all_tools() if t.gated}


def _draft_id(key: str) -> str:
    return key.removeprefix(PREFIX)


class DraftGate(shell.ShellGate):
    async def __call__(self, name: str, args: dict, ctx) -> PermissionResultAllow | PermissionResultDeny:
        if name not in gated():
            return await super().__call__(name, args, ctx)
        tool = name.removeprefix("mcp__jarvis__")
        draft_id = str(args.get("draft_id", "")).strip()
        draft = pending(draft_id)
        if draft is None or draft.kind != tool:
            again = DRAFTED_BY.get(tool, "the drafting tool")
            return PermissionResultDeny(message=f"Not done: there's no draft {draft_id} for {tool}, or it has lapsed. "
                                                f"Make it again with {again}.")
        key = PREFIX + draft_id
        if key in self.agreed:
            del self.agreed[key]
            approve(draft_id)
            log.warning("%s for draft %s, as agreed: %s", tool, draft_id, draft.short)
            return PermissionResultAllow()
        if key not in self.asked:
            # A yes covers only the latest draft; older ones lapse
            self.asked = [k for k in self.asked if not k.startswith(PREFIX)] + [key]
            self.pending = {k: t for k, t in self.pending.items() if not k.startswith(PREFIX)}
            self.agreed = {k: t for k, t in self.agreed.items() if not k.startswith(PREFIX)}
            self.what[key] = WHAT.get(tool, "sending or deleting something on Google")
            log.warning("asking about draft %s: %s", draft_id, draft.short)
        return PermissionResultDeny(
            message=f"Not done yet: JARVIS reads the draft aloud to {profile.name()} and asks for their yes itself. Don't "
            f"read it out, describe it or ask anything yourself; stop here. When they say yes, now or a few minutes "
            f"later, call {tool} again with the same draft_id."
        )

    def kind_of(self, key: str) -> str:
        """Consent kind for a key; drafts map to GOOGLE so their text never reaches the model."""
        return consent.GOOGLE if key.startswith(PREFIX) else super().kind_of(key)

    def question(self, spoken: str = "") -> str:
        """Read held drafts out verbatim, then ask; else defer to ShellGate."""
        drafts = [d for k in self.asked if k.startswith(PREFIX) and (d := pending(_draft_id(k)))]
        if not drafts:
            self.asked = [k for k in self.asked if not k.startswith(PREFIX)]  # drop lapsed drafts
            return super().question(spoken)
        self.put = True
        commands = any(not k.startswith(PREFIX) for k in self.asked)
        sends = all(d.kind in SENDS for d in drafts)
        if commands:
            ask = f"{SEND_QUESTION[:-1]}, and go ahead with the command?" if sends else GO_QUESTION
        else:
            ask = SEND_QUESTION if sends else GO_QUESTION
        return shell.following(spoken, " ".join(d.summary for d in drafts) + " " + ask)

    def agreed_hint(self, agreed: list[str]) -> str:
        """Tell the model exactly what the user's yes now allows."""
        commands = [k for k in agreed if not k.startswith(PREFIX)]
        hints = [super().agreed_hint(commands)] if commands else []
        for k in agreed:
            if k.startswith(PREFIX):
                draft = pending(_draft_id(k))
                tool = draft.kind if draft else "the same tool"
                hints.append(f"[{profile.name()} has said yes to the draft read out to them. Go ahead now: call {tool} "
                             f"with draft_id {_draft_id(k)}.]")
        return "\n".join(hints)

    def described(self, agreed: list[str]) -> list[str]:
        """What was approved, for the day's log: commands verbatim, drafts briefly."""
        return [k if not k.startswith(PREFIX) else d.short if (d := pending(_draft_id(k)))
                else f"draft {_draft_id(k)}" for k in agreed]
