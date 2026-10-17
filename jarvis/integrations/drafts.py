"""The Google tools (AI Armory) that send or delete, and the drafting tools that prepare each: a send runs only on a
draft, after a spoken yes (jarvis.gates.draft_gate)."""

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
