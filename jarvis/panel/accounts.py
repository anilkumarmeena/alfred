"""Control panel actions: Google accounts."""

import asyncio
from dataclasses import asdict

from jarvis.integrations import accounts, google_signin
from jarvis.panel.base import GIT_TIMEOUT, HIDDEN, LOG_LINES, SERVICE_NAMES, _id, _text, action, log  # noqa: F401

# ── Google accounts ─────────────────────────────────────────────


def _accounts() -> list[dict]:
    found = accounts.load()
    return [{"label": a.label, "email": a.email, "note": a.note, "chat": a.chat, "default": a.label == found.default,
             "signed_in": accounts.token_path(a.label).exists()} for a in found.accounts]


@action
async def accounts_(_: dict) -> list[dict]:
    return _accounts()


@action
async def account_note(body: dict) -> list[dict]:
    with accounts.change() as found:
        found.update(_text(body, "label"), note=_text(body, "note", 300))
    return _accounts()


@action
async def account_default(body: dict) -> list[dict]:
    with accounts.change() as found:
        found.set_default(_text(body, "label"))
    return _accounts()


@action
async def account_add(body: dict) -> list[dict]:
    """Add an account to sign in next: {"label", "note", "chat" (Workspace with Google Chat)}."""
    account = accounts.Account(_text(body, "label", 40), note=_text(body, "note", 300), chat=body.get("chat") is True)

    def add() -> list[dict]:
        with accounts.change() as found:
            found.add(account)
        return _accounts()

    return await asyncio.to_thread(add)


@action
async def account_remove(body: dict) -> list[dict]:
    """Remove an account and delete its sign-in: {"label"}."""
    label = _text(body, "label")

    def remove() -> list[dict]:
        with accounts.change() as found:
            found.remove(label)
            accounts.remove_files(label)  # inside the transaction: a failed delete keeps the account
        return _accounts()

    return await asyncio.to_thread(remove)


@action
async def google_setup(body: dict) -> dict:
    """The OAuth client's state for the Google accounts page: {"origin"} is the panel's address."""
    return await asyncio.to_thread(google_signin.signins.setup, _text(body, "origin", 300))


@action
async def google_sign_in(body: dict) -> dict:
    """Start signing an account in: {"label", "step" (Chat accounts' scope step, 0 first), "origin"}. Returns Google's
    URL to open and the state to follow."""
    return await asyncio.to_thread(google_signin.start, _text(body, "label", 40), _id(body, "step"),
                                   _text(body, "origin", 300))


@action
async def google_sign_in_status(body: dict) -> dict:
    """How a started sign-in went, waiting a while for it to come back: {"state"}; with the accounts once it's over."""
    found = await google_signin.signins.wait(_text(body, "state", 300))
    return found | ({"accounts": await asyncio.to_thread(_accounts)} if found["state"] != "waiting" else {})


@action
async def google_check(body: dict) -> dict:
    """Check a signed-in account's Google APIs again (after turning one on in Google Cloud): {"label"}."""
    label = _text(body, "label", 40)
    return await asyncio.to_thread(lambda: asdict(google_signin.recheck(label)) | {"accounts": _accounts()})


@action
async def google_sign_in_paste(body: dict) -> dict:
    """Finish a sign-in from the address Google's redirect ended on (a phone with a Desktop app client): {"address"}.
    How it went reaches the panel through google_sign_in_status, which is waiting on it."""
    await google_signin.signins.finish_pasted(_text(body, "address"))
    return {}
