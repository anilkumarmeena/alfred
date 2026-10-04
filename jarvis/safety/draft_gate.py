"""The CLI brain's gate (shell.ShellGate), which also holds the Google tools' sends and deletes for a spoken yes.

Sending an email, a Google Chat message or calendar invites, changing or answering an event that has guests, and
deleting a calendar event, parts of a Google Doc or a Google Sheet's tab are each drafted first and done by a second
tool that takes only the draft's id (DRAFTED_BY pairs them, such as chat_draft then chat_send).
Those are gated (jarvis.tools.google), so sdk.jarvis_server leaves them out of allowed_tools and every call comes
here. The first is refused and the draft is put to the user at the end of the turn: JARVIS reads the draft's own
summary, worked out by the drafting tool rather than taken from the model's words (for an email or a Chat message,
everyone it goes to, cc and bcc included, and the exact text), then asks. The same yes and no rules and time limits
as risky shell commands apply (new_turn), and a reply the word lists can't read goes to the model as a reply to a send
or a delete (`WHAT` the draft's kind does: "sending a Google Chat message"), never with the draft. After a clear yes,
the draft is approved, so AI Armory lets the identical call through once, and only for the draft just read out: a
newer draft replaces an older one still waiting.
"""

import logging

from ai_armory.toolsets.google import approve, pending
from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

from jarvis import tools
from jarvis.core import profile
from jarvis.safety import consent, shell

log = logging.getLogger("jarvis.safety.draft_gate")

PREFIX = "draft:"  # a draft's key among the commands the gate asks about
SEND_QUESTION = "Shall I send it?"
GO_QUESTION = "Shall I go ahead?"
# What goes to other people, rather than deleting something
SENDS = {"gmail_send", "chat_send", "calendar_send_invite", "calendar_send_update", "calendar_send_response"}
DRAFTED_BY = {"gmail_send": "gmail_draft_send or gmail_draft_reply", "chat_send": "chat_draft",
              "calendar_send_invite": "calendar_draft_invite", "calendar_send_update": "calendar_draft_update",
              "calendar_send_response": "calendar_draft_respond", "calendar_delete_event": "calendar_draft_delete",
              "docs_delete": "docs_draft_delete", "sheets_delete_tab": "sheets_draft_delete_tab"}
# What each does, as consent's model is told it: from the draft's kind, never its text
WHAT = {"gmail_send": "sending an email", "chat_send": "sending a Google Chat message",
        "calendar_send_invite": "sending calendar invites",
        "calendar_send_update": "sending an event's change to its guests",
        "calendar_send_response": "answering a calendar invite", "calendar_delete_event": "deleting a calendar event",
        "docs_delete": "deleting part of a Google Doc", "sheets_delete_tab": "deleting a tab of a Google Sheet"}


def gated() -> set[str]:
    """The full names of the JARVIS tools that wait for a yes."""
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
            # A yes only ever covers the draft read out last: any other still waiting lapses.
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
        """A draft's send or delete, as a kind: never the draft itself, which may hold an email's text."""
        return consent.GOOGLE if key.startswith(PREFIX) else super().kind_of(key)

    def question(self, spoken: str = "") -> str:
        """Reads a held draft out in full, word for word, then asks; otherwise as ShellGate does."""
        drafts = [d for k in self.asked if k.startswith(PREFIX) and (d := pending(_draft_id(k)))]
        if not drafts:
            self.asked = [k for k in self.asked if not k.startswith(PREFIX)]  # lapsed ones can't be asked about
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
        """Tells the model exactly what the user's yes now lets it do."""
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
        """What the user said yes to, for the day's log: commands as they are, drafts in a few words."""
        return [k if not k.startswith(PREFIX) else d.short if (d := pending(_draft_id(k)))
                else f"draft {_draft_id(k)}" for k in agreed]
