"""Control panel actions: Logins, JARVIS's own keys, and Claude logins."""

import asyncio

from jarvis import claude_login, setup_local
from jarvis.auth import credentials, logins
from jarvis.llm import claude_logins, usage
from jarvis.panel.base import GIT_TIMEOUT, HIDDEN, LOG_LINES, SERVICE_NAMES, _id, _text, action, log  # noqa: F401
from jarvis.state import activity

# ── Logins and JARVIS's own keys ────────────────────────────────
# Passcodes (core.logins) and keys (core.credentials) are set only here: never read back, never by voice


def _access(login: str) -> dict:
    """Logins (caller marked) and key status/source; never key values."""
    return {"logins": [found | {"this": found["name"] == login} for found in logins.listed()],
            "keys": [{"name": c.name, "label": c.label, "hint": c.hint, "required": c.required,
                      "source": credentials.source(c)} for c in credentials.ALL.values()]}


def _passcode(body: dict) -> str:
    value = body.get("passcode_new", "")
    if not isinstance(value, str):
        raise ValueError("The passcode must be text.")
    return value


@action
async def access(body: dict) -> dict:
    return await asyncio.to_thread(_access, body.get("login", ""))


@action
async def login_add(body: dict) -> dict:
    """Add a login {"name", "passcode_new"}; passcodes must be unique."""
    name = _text(body, "name", 40)
    await asyncio.to_thread(logins.add, name, _passcode(body))
    await asyncio.to_thread(activity.add, f"Added the login {name} in the control panel.")
    return await access(body)


@action
async def login_passcode(body: dict) -> dict:
    """Change a login's passcode: {"name", "passcode_new"}."""
    await asyncio.to_thread(logins.set_passcode, _text(body, "name", 40), _passcode(body), str(body.get("login", "")))
    return await access(body)


@action
async def login_remove(body: dict) -> dict:
    """Remove a login (never the last): {"name"}. Its devices must log in again."""
    name = _text(body, "name", 40)
    await asyncio.to_thread(logins.remove, name, str(body.get("login", "")))
    await asyncio.to_thread(activity.add, f"Removed the login {name} in the control panel.")
    return await access(body)


def _set_key(body: dict) -> None:
    key = credentials.need(_text(body, "name", 40))
    credentials.put(key, str(body.get("value", "")))
    if key in (credentials.LIVEKIT_KEY, credentials.LIVEKIT_SECRET):
        setup_local.write_livekit_config()  # picked up when LiveKit next starts


@action
async def key_set(body: dict) -> dict:
    """Store a JARVIS key in the vault: {"name", "value"}. Write-only; used after services restart."""
    await asyncio.to_thread(_set_key, body)
    return await access(body)


@action
async def key_clear(body: dict) -> dict:
    """Clear an optional key: {"name"}. Falls back to .env if set there."""
    await asyncio.to_thread(credentials.clear, credentials.need(_text(body, "name", 40)))
    return await access(body)


# ── Claude logins (jarvis.llm.claude_logins, jarvis.claude_login) ─────
# Signed in here through the web service, in the desktop session, so each lands in the keychain the services read.
# JARVIS runs on the first usable one in the priority order set here. "" names this Mac's own Claude Code login,
# always on the list; any other can be removed. Only the owner changes them: a login is a Claude account and its usage.

CLAUDE_LOGINS = "change JARVIS's Claude logins"


def _claude_login(body: dict) -> str:
    return claude_logins.known(_text(body, "name", 40))


def _claude_row(name: str, active: str, checks: dict) -> dict:
    said = claude_login.status(name)
    found = claude_logins.account(name)
    return {"name": name, "active": name == active, "skipped": claude_logins.skipped(name),
            "signed_in": bool(said.get("loggedIn")),
            "email": said.get("email") or found.get("emailAddress"), "org": said.get("orgName"),
            "plan": said.get("subscriptionType"), "check": checks.get(name),
            "usage": usage.said(kept=usage.of(name)) if usage.of(name) else ""}


async def _claude_logins(body: dict) -> dict:
    active, checks = await asyncio.to_thread(claude_logins.active), await asyncio.to_thread(claude_logins.checks)
    names = await asyncio.to_thread(claude_logins.order)
    rows = await asyncio.gather(*(asyncio.to_thread(_claude_row, name, active, checks) for name in names))
    return {"logins": list(rows), "can_change": await asyncio.to_thread(logins.is_owner, str(body.get("login", "")))}


def _claude_owner_only(body: dict) -> None:
    logins.owner_only(str(body.get("login", "")), CLAUDE_LOGINS)


@action
async def claude_logins_(body: dict) -> dict:
    """The Claude logins JARVIS can run on, by priority: each one's account, last check and usage, why it's passed
    over if it is, and which one JARVIS runs on now."""
    return await _claude_logins(body)


@action
async def claude_login_start(body: dict) -> dict:
    """Start signing a login in: {"name" (new = add it; "" = this Mac's own), "email" (optional, prefilled)}. Returns
    Claude's sign-in link and the state to finish with."""
    await asyncio.to_thread(_claude_owner_only, body)
    raw = _text(body, "name", 40)
    name = claude_logins.valid(raw) if raw else claude_logins.MAC
    return await asyncio.to_thread(claude_login.signins.start, name, _text(body, "email", 200))


@action
async def claude_login_finish(body: dict) -> dict:
    """Finish a sign-in with the code Claude's page showed: {"state", "code"}. Checks the login, then lists them."""
    await asyncio.to_thread(_claude_owner_only, body)
    ok, said = await asyncio.to_thread(claude_login.signins.finish, _text(body, "state", 100), _text(body, "code", 2000))
    if ok:
        await asyncio.to_thread(activity.add, "Signed a Claude login in from the control panel.")
    return {"ok": ok, "said": said} | await _claude_logins(body)


@action
async def claude_login_cancel(body: dict) -> dict:
    await asyncio.to_thread(claude_login.signins.cancel, _text(body, "state", 100))
    return {}


@action
async def claude_login_check(body: dict) -> dict:
    """Check JARVIS's services can use a login: {"name"}. One tiny model call."""
    await asyncio.to_thread(_claude_owner_only, body)
    ok, said = await asyncio.to_thread(claude_login.check, _claude_login(body))
    return {"ok": ok, "said": said} | await _claude_logins(body)


@action
async def claude_login_order(body: dict) -> dict:
    """Set the priority order: {"names": every login, first = most wanted}. Applies from JARVIS's next call; tasks
    already started stay on theirs while it's usable."""
    await asyncio.to_thread(_claude_owner_only, body)
    names = body.get("names")
    if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
        raise ValueError("Send the logins' names, in order.")
    await asyncio.to_thread(claude_logins.set_order, names)
    await asyncio.to_thread(activity.add, "Changed the Claude logins' order in the control panel: "
                                          + " > ".join(n or "this Mac" for n in names) + ".")
    return await _claude_logins(body)


@action
async def claude_login_remove(body: dict) -> dict:
    """Sign a login out and forget it: {"name"}. Never this Mac's own, which the terminal uses."""
    await asyncio.to_thread(_claude_owner_only, body)
    name = _claude_login(body)
    await asyncio.to_thread(claude_login.sign_out, name)
    await asyncio.to_thread(activity.add, f"Removed the Claude login {name} in the control panel.")
    return await _claude_logins(body)
