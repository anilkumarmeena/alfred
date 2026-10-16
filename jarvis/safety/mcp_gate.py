"""The CLI brain's gate: DraftGate plus a spoken yes for MCP servers' write tools.

Read and own-inbox tools are allowed upstream and never reach here. A write is refused first; at turn end JARVIS reads
out `describe()` (built from the tool's `says` phrase and the call's own args, not the model's words) and asks. Yes/no
rules match risky shell commands. A clear yes lets the identical call through once. Unlisted or blocked tools
are refused.
"""

import json
import logging
import re

from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

from jarvis.core import profile
from jarvis.integrations.mcp_servers import Server
from jarvis.safety import consent, draft_gate, shell

log = logging.getLogger("jarvis.safety.mcp_gate")

PREFIX = "mcp:"  # marks an MCP action key among the gate's keys
HINT_ARGS = 2000  # max chars of args echoed back to the model after a yes
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


def key(name: str, args: dict) -> str:
    return PREFIX + name + " " + json.dumps(args, sort_keys=True, ensure_ascii=False)


class Gate(draft_gate.DraftGate):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.servers: dict[str, Server] = {}
        self.actions: dict[str, str] = {}  # key -> spoken description

    def use(self, servers: list[Server]) -> None:
        """Set this session's servers."""
        self.servers = {s.name: s for s in servers}

    def _target(self, name: str) -> tuple[Server | None, str]:
        """(session server or None, bare tool name) for a full tool name."""
        server_name, _, tool = name.removeprefix("mcp__").partition("__")
        return (self.servers.get(server_name) if name.startswith("mcp__") else None), tool

    def change(self, name: str, args: dict) -> str | None:
        """describe() for a write/own tool call on a session server; None otherwise."""
        server, tool = self._target(name)
        return describe(server, tool, args) if server and tool in server.named("write", "own") else None

    async def __call__(self, name: str, args: dict, ctx) -> PermissionResultAllow | PermissionResultDeny:
        if name.startswith(("mcp__jarvis__", "mcp__shell__")) or not name.startswith("mcp__"):
            return await super().__call__(name, args, ctx)
        server, tool = self._target(name)
        if server is None or tool not in server.named("write"):
            return PermissionResultDeny(message=f"{tool or name} isn't available.")
        k = key(name, args)
        if k in self.agreed:
            del self.agreed[k]
            log.warning("as agreed: %s", self.actions.get(k, k))
            return PermissionResultAllow()
        if k not in self.asked:
            self.asked.append(k)
            self.actions[k] = describe(server, tool, args)
            self.what[k] = f"a change on {server.label}"  # for the consent model: never the args
            log.warning("asking: %s", self.actions[k])
        return PermissionResultDeny(
            message=f"Not done yet: it reaches other people or changes something there, so it needs {profile.name()}'s "
            "go-ahead, and JARVIS reads out what it would do and asks them for you. Don't describe it or ask anything "
            "yourself; stop here. When they say yes, now or a few minutes later, make the identical call again, with "
            "exactly the same arguments."
        )

    def kind_of(self, key: str) -> str:
        return consent.SERVICE if key.startswith(PREFIX) else super().kind_of(key)

    def new_turn(self, text: str, verdict: consent.Verdict | None = None) -> list[str]:
        agreed = super().new_turn(text, verdict)
        self.actions = {k: d for k, d in self.actions.items() if k in self.pending or k in self.agreed}
        return agreed

    def question(self, spoken: str = "") -> str:
        """Read out each held write, then ask; other held items go through DraftGate."""
        mine = [k for k in self.asked if k.startswith(PREFIX)]
        if not mine:
            return super().question(spoken)
        told = " ".join(self.actions[k] for k in mine)
        if len(mine) < len(self.asked):
            text = f"{told} {super().question()}"
        else:
            self.put = True
            text = f"{told} {shell.QUESTION}"
        return shell.following(spoken, text)

    def agreed_hint(self, agreed: list[str]) -> str:
        """Tell the model exactly which call the user's yes now allows."""
        rest = [k for k in agreed if not k.startswith(PREFIX)]
        hints = [super().agreed_hint(rest)] if rest else []
        for k in agreed:
            if k.startswith(PREFIX):
                name, _, args = k.removeprefix(PREFIX).partition(" ")
                same = f"these arguments: {args}" if len(args) <= HINT_ARGS else "the same arguments as before"
                hints.append(f"[{profile.name()} has said yes to this: {self.actions.get(k, name)} Do it now: call {name} "
                             f"again with exactly {same}]")
        return "\n".join(hints)

    def described(self, agreed: list[str]) -> list[str]:
        """What was approved, for the day's log."""
        return [self.actions.get(k, k) if k.startswith(PREFIX) else super().described([k])[0] for k in agreed]
