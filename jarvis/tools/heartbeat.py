"""Voice tools for heartbeat sources: list, toggle, change settings, add and remove (per user, re-read each beat)."""

from jarvis.tasks import heartbeat

from . import tool
from .core import vetted

SETTINGS = {"type": "object", "additionalProperties": {"type": "string"},
            "description": "Settings by key, as heartbeat_sources lists them for its kind; an empty value goes back to "
                           "the default."}


@tool(
    "heartbeat_sources",
    "What the heartbeat checks every few minutes to flag what's new, unasked: each source, on or off, with its "
    "settings, and the kinds more can be added of. Use when the user asks what the heartbeat watches or flags.",
    reads=True,
)
def heartbeat_sources() -> dict:
    found = heartbeat.described()
    return {"sources": found["sources"], "can_add": found["kinds"]}


@tool(
    "heartbeat_source_change",
    "Switch a heartbeat source on or off, or change its settings (calendar's minutes ahead, mail's Gmail search). Use "
    "when the user says to stop or start flagging something, or to flag it differently. Applies from the next beat.",
    {"id": {"type": "string", "description": "The source's id, from heartbeat_sources."},
     "on": {"type": "boolean"},
     "settings": SETTINGS},
    ["id"],
    said="Changed the heartbeat's {id} source",
)
def heartbeat_source_change(id: str, on: bool | None = None, settings: dict | None = None) -> str:
    c = heartbeat.change_source(id, on, settings)
    return f"{c.title} is {'on' if c.on else 'off'}" + "".join(f"; {k}: {v}" for k, v in c.settings.items()) + "."


@tool(
    "heartbeat_source_add",
    "Add another heartbeat source of a kind that allows more, with its own title and settings: another Gmail search "
    "whose new mail is flagged as it arrives, say. Use when the user wants to hear about something new every time it "
    "turns up, rather than once (a watch, schedule_add), or a skill says to add one. Its first check only notes "
    "what's there already.",
    {"kind": {"type": "string", "description": "A kind from heartbeat_sources' can_add."},
     "title": {"type": "string", "description": "A few words: what it flags, as the user would say it."},
     "settings": SETTINGS,
     "for_skill": {"type": "string", "description": "The skill that says to add it, if one does."}},
    ["kind", "title", "settings"],
    said="Added {title} to the heartbeat",
)
def heartbeat_source_add(kind: str, title: str, settings: dict, for_skill: str = "") -> str:
    added = f"by the {vetted(for_skill, 'the skill')} skill" if for_skill.strip() else "by voice"
    c = heartbeat.add_source(kind, vetted(title, "the title"), settings, added)
    return f"Added {c.title} (id {c.id}): checked from the next beat."


@tool(
    "heartbeat_source_remove",
    "Take out a heartbeat source that was added; a built-in one can only be switched off (heartbeat_source_change).",
    {"id": {"type": "string", "description": "The source's id, from heartbeat_sources."}},
    ["id"],
    said="Took {id} out of the heartbeat",
)
def heartbeat_source_remove(id: str) -> str:
    heartbeat.remove_source(id)
    return f"Took {id} out of the heartbeat."
