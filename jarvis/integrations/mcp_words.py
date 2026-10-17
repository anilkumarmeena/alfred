"""What a connection's write does, in words to read out before the user says yes: built from the tool's `says` phrase
(jarvis.integrations.mcp_servers) and the call's own arguments, never the model's description of it."""

import json
import re

from jarvis.integrations.mcp_servers import Server

TEXT_LIMIT = 300  # chars of free text read out; the rest is counted in words
QUIET = {"jmespath", "encoding"}  # shape the reply, not the action: not read out
_FIELD = re.compile(r"\{(\w+)\}")
_OPTIONAL = re.compile(r"\[([^\[\]]*)\]")


def _text(value: str) -> str:
    """Text for reading aloud: truncated with a word count past TEXT_LIMIT, quoted if multi-word."""
    words = " ".join(value.split())
    if len(words) > TEXT_LIMIT:
        head = words[:TEXT_LIMIT].rsplit(" ", 1)[0]
        return f"'{head}…', and {len(words.split()) - len(head.split())} more words"
    if re.fullmatch(r"[A-Z]+(_[A-Z]+)+", words):  # REQUEST_CHANGES
        return words.replace("_", " ").lower()
    return f"'{words}'" if " " in words else words


def _value(key: str, value) -> str:
    if key == "content" and isinstance(value, str):
        return f"{len(value)} characters"
    if key == "position" and isinstance(value, dict) and (path := value.get("new_path") or value.get("old_path")):
        line = value.get("new_line") or value.get("old_line")  # a diff comment's place, not its commit hashes
        return f"at {path}" + (f" line {line}" if line else "")
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, list) and value and all(isinstance(v, dict) for v in value):
        paths = [str(v.get("path") or v.get("file_path") or v.get("name") or "?") for v in value]
        named = ", ".join(paths[:5]) + (f" and {len(paths) - 5} more" if len(paths) > 5 else "")
        return f"{len(paths)} file{'s' * (len(paths) != 1)} ({named})"
    if isinstance(value, list):
        return ", ".join(_text(str(v)) for v in value)
    if isinstance(value, dict):
        return _text(json.dumps(value, ensure_ascii=False))
    return _text(str(value))


def _filled(value) -> bool:
    return value not in (None, "", [], {}, False)


def describe(server: Server, tool: str, args: dict) -> str:
    """Spoken description of a write: 'On GitLab, ' plus its `action`."""
    return f"On {server.label}, {action(server, tool, args)}."


def action(server: Server, tool: str, args: dict) -> str:
    """What a write does, from its `says` phrase; args the phrase omits are appended so none go unsaid."""
    says = server.tools[tool].says if tool in server.tools else ""
    says = _OPTIONAL.sub(lambda m: m.group(1) if all(_filled(args.get(f)) for f in _FIELD.findall(m.group(1)))
                         else "", says or tool.replace("_", " "))
    used = set(_FIELD.findall(says))
    said = _FIELD.sub(lambda m: _value(m.group(1), args[m.group(1)]) if _filled(args.get(m.group(1))) else "", says)
    said = re.sub(r"\s+([,.])", r"\1", re.sub(r"\s{2,}", " ", said)).strip()
    rest = [f"{k.replace('_', ' ')} {_value(k, v)}" if v is not True else k.replace("_", " ")
            for k, v in args.items() if k not in used and k not in QUIET and _filled(v)]
    return said + (", " + ", ".join(rest) if rest else "")
