"""The CLI brain's gate (draft_gate.DraftGate), which also holds MCP servers' write tools for a spoken yes.

The servers come from the registry (jarvis.integrations.mcp_servers), handed over as each session starts (`use`). Their read and
own-inbox tools are allowed outright, so they never come here. A write tool is refused the first time, and at the end
of the turn JARVIS reads out what it would do, built from the tool's `says` phrase and the call's own arguments rather
than the model's words (`describe`), then asks "Okay to go ahead?". The same yes and no rules and time limits as risky
shell commands apply (ShellGate.new_turn). After a clear yes, the identical call, with the same arguments, goes through
once. Any other tool of theirs (blocked, or not listed) is refused.
"""

import json
import logging
import re

from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

from jarvis.core import profile
from jarvis.integrations.mcp_servers import Server
from jarvis.safety import consent, draft_gate, shell

log = logging.getLogger("jarvis.safety.mcp_gate")

PREFIX = "mcp:"  # an action's key among what the gate asks about
HINT_ARGS = 2000  # characters of a call's arguments repeated to the model with the user's yes (files can be long)
TEXT_LIMIT = 300  # characters of a title, comment or message read out; the rest is counted in words
QUIET = {"jmespath", "encoding"}  # arguments that shape the reply or the transfer, not what's done
_FIELD = re.compile(r"\{(\w+)\}")
_OPTIONAL = re.compile(r"\[([^\[\]]*)\]")


def _text(value: str) -> str:
    """Text as read out: whole if short, else its start and how much more there is; quoted when it's words."""
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
    """What a write would do, from its `says` phrase and its own arguments, as JARVIS reads it out: every argument the
    phrase doesn't name is listed after it, so nothing the call carries goes unsaid."""
    says = server.tools[tool].says if tool in server.tools else ""
    says = _OPTIONAL.sub(lambda m: m.group(1) if all(_filled(args.get(f)) for f in _FIELD.findall(m.group(1)))
                         else "", says or tool.replace("_", " "))
    used = set(_FIELD.findall(says))
    said = _FIELD.sub(lambda m: _value(m.group(1), args[m.group(1)]) if _filled(args.get(m.group(1))) else "", says)
    said = re.sub(r"\s+([,.])", r"\1", re.sub(r"\s{2,}", " ", said)).strip()
    rest = [f"{k.replace('_', ' ')} {_value(k, v)}" if v is not True else k.replace("_", " ")
            for k, v in args.items() if k not in used and k not in QUIET and _filled(v)]
    return f"On {server.label}, {said}" + (", " + ", ".join(rest) if rest else "") + "."


def key(name: str, args: dict) -> str:
    return PREFIX + name + " " + json.dumps(args, sort_keys=True, ensure_ascii=False)


class Gate(draft_gate.DraftGate):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.servers: dict[str, Server] = {}
        self.actions: dict[str, str] = {}  # key -> what it does, as read out

    def use(self, servers: list[Server]) -> None:
        """The servers this session has (jarvis.integrations.mcp_servers.session)."""
        self.servers = {s.name: s for s in servers}

    def _target(self, name: str) -> tuple[Server | None, str]:
        """The server a tool name's call goes to (None if it isn't one of this session's), and the tool's own name."""
        server_name, _, tool = name.removeprefix("mcp__").partition("__")
        return (self.servers.get(server_name) if name.startswith("mcp__") else None), tool

    def change(self, name: str, args: dict) -> str | None:
        """What a call to an MCP server's write (or own) tool does, said as describe() says it; None for a read, or
        anything not on a server of this session's."""
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
            self.what[k] = f"a change on {server.label}"  # for consent's model: never the call's own arguments
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
        """Reads out each write held this turn, then asks; anything else held is asked about as DraftGate does."""
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
        """Tells the model exactly which call the user's yes now lets through."""
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
        """What the user said yes to, for the day's log."""
        return [self.actions.get(k, k) if k.startswith(PREFIX) else super().described([k])[0] for k in agreed]
