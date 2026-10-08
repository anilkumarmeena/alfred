"""Whether JARVIS's connections still work, so the heartbeat can say when one stops, before a tool fails on it.

  google  each configured Google account: signed in, and its sign-in still refreshes. Google gives no end date for a
          sign-in, so this can only say once it has stopped working (revoked, a password change, or a 7-day token
          from an OAuth app left in testing), within about an hour, as access tokens last that long
  github  the gh CLI's login, which the heartbeat and tasks use, and the GitHub MCP server's token (github.token in
          the vault) when that server is on: each still accepted, and, for a token with an end date (GitHub's
          github-authentication-token-expiration header), how soon it ends
  gitlab  the GitLab MCP server's token (gitlab.token), when that server is on: still accepted and active, and its
          expires_at (GitLab's personal_access_tokens/self)

Each finding is a Problem with a key that stays the same while it lasts, so the heartbeat says it once (and again
only if it clears and comes back). It warns SIGNIN_WARN_DAYS before a token's end. A check that can't reach the
service says nothing: being offline isn't a sign-in problem. Read-only: it reads tokens to test them, never shows,
logs or changes them, except that refreshing a Google sign-in saves the new access token, as every Google tool does.
"""

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import date, datetime

from jarvis.core import profile
from jarvis.core.config import SIGNIN_WARN_DAYS
from jarvis.core.proc import run_try

log = logging.getLogger("jarvis.integrations.signins")
TIMEOUT = 20
GITHUB_API = "https://api.github.com"


@dataclass(frozen=True)
class Problem:
    key: str  # the same while the problem lasts
    text: str  # one sentence, fit to be spoken and shown on the lock screen: never a token or what a service said


def _ending(key: str, what: str, ends: date, today: date, fix: str) -> list[Problem]:
    """A warning if `ends` is within SIGNIN_WARN_DAYS, else nothing."""
    days = (ends - today).days
    if days > SIGNIN_WARN_DAYS:
        return []
    when = "today" if days <= 0 else "tomorrow" if days == 1 else f"in {days} days, on {ends:%a} {ends.day} {ends:%b}"
    return [Problem(f"{key}:ends:{ends.isoformat()}", f"{what} expires {when}: {fix}")]


def _stopped(service: str, name: str, fix: str) -> Problem:
    """An MCP server's token that the service no longer accepts."""
    return Problem(f"signin:{service}:token:out", f"JARVIS's {name} token has stopped working; {fix}")


# ── Google ──────────────────────────────────────────────────────


def _google_account(label: str) -> str:
    """'' if the account's sign-in works (refreshing it if it's due), 'out' if it doesn't, 'offline' if Google can't
    be reached."""
    from ai_armory.toolsets.google import common
    from google.auth.exceptions import RefreshError, TransportError
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    path = common.token_path(label)
    if not path.exists():
        return "out"
    try:
        creds = Credentials.from_authorized_user_file(str(path))
    except (ValueError, KeyError):
        return "out"
    if creds.valid:
        return ""
    if not creds.refresh_token:
        return "out"
    try:
        creds.refresh(Request())
    except RefreshError:
        return "out"
    except TransportError:
        return "offline"
    common.write_private(path, creds.to_json())
    return ""


async def google_accounts(now: datetime) -> list[Problem]:
    from ai_armory.toolsets.google.common import settings

    from jarvis.tools import google  # sets AI Armory's Google tool sets up with JARVIS's accounts

    out = []
    for label in google.LABELS:
        try:
            state = await asyncio.wait_for(asyncio.to_thread(_google_account, label), TIMEOUT)
        except Exception as e:  # the type alone: an error's text may carry what the request did
            log.warning("checking the %s Google account failed: %s", label, type(e).__name__)
            continue
        if state == "out":
            out.append(Problem(f"signin:google:{label}:out",
                               f"The {label} Google account is signed out, so JARVIS can't reach its mail, calendar "
                               f"or files. Sign it in again on the Mac: {settings().sign_in(label)}"))
    return out


# ── GitHub ──────────────────────────────────────────────────────


def _expiry(value: str | None) -> date | None:
    """GitHub's '2026-11-01 12:00:00 UTC', or GitLab's '2026-11-01', as a date."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.removesuffix(" UTC").strip()).date()
    except ValueError:
        return None


async def _token_check(url: str, headers: dict[str, str]):
    """GET `url` with a token: the response, or None if the service couldn't be reached."""
    import httpx

    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as c:
            return await c.get(url, headers=headers)
    except httpx.HTTPError as e:
        log.info("sign-in check of %s couldn't connect: %s", url.split("/")[2], type(e).__name__)
        return None


def _secret(server_name: str) -> str | None:
    """The MCP server's token, if the server is on and its token is set; None otherwise."""
    from jarvis.core import vault
    from jarvis.integrations import mcp_servers

    server = mcp_servers.get(server_name)
    if not server or not server.enabled or not server.secret_env:
        return None
    name = next(iter(server.secret_env.values()))
    return vault.get(name) if vault.has(name) else None


async def github(now: datetime) -> list[Problem]:
    out = []
    code, said = await run_try("gh", "auth", "status", "--hostname", "github.com", timeout=TIMEOUT)
    said = said.lower()
    offline = any(w in said for w in ("connect", "timeout", "timed out", "network", "no such host"))
    if code and not offline and any(w in said for w in ("not logged", "invalid", "expired", "re-authenticate")):
        out.append(Problem("signin:github:gh:out", "The GitHub CLI on the Mac is signed out, so the heartbeat can't "
                                                   "see reviews or mentions. Sign it in again: gh auth login."))
    token = await asyncio.to_thread(_secret, "github")
    if token:
        r = await _token_check(f"{GITHUB_API}/user", {"Authorization": f"Bearer {token}",
                                                      "Accept": "application/vnd.github+json"})
        fix = "make a new one on GitHub and enter it in the control panel's Connections page."
        if r is not None and r.status_code == 401:
            out.append(_stopped("github", "GitHub", fix))
        elif r is not None and (ends := _expiry(r.headers.get("github-authentication-token-expiration"))):
            out += _ending("signin:github:token", "JARVIS's GitHub token", ends, now.astimezone(profile.tz()).date(), fix)
    return out


# ── GitLab ──────────────────────────────────────────────────────


async def gitlab(now: datetime) -> list[Problem]:
    from jarvis.integrations import mcp_servers

    token = await asyncio.to_thread(_secret, "gitlab")
    if not token:
        return []
    server = await asyncio.to_thread(mcp_servers.get, "gitlab")
    api = ((server.env if server else {}).get("GITLAB_API_URL") or "https://gitlab.com/api/v4").rstrip("/")
    r = await _token_check(f"{api}/personal_access_tokens/self", {"PRIVATE-TOKEN": token})
    fix = "make a new one in GitLab and enter it in the control panel's Connections page."
    if r is None:
        return []
    if r.status_code == 401:
        return [_stopped("gitlab", "GitLab", fix)]
    if r.status_code != 200:
        return []
    try:
        found = r.json()
    except json.JSONDecodeError:
        return []
    if found.get("revoked") or found.get("active") is False:
        return [_stopped("gitlab", "GitLab", fix)]
    if ends := _expiry(found.get("expires_at")):
        return _ending("signin:gitlab:token", "JARVIS's GitLab token", ends, now.astimezone(profile.tz()).date(), fix)
    return []


CHECKS = {"google": google_accounts, "github": github, "gitlab": gitlab}


async def check(now: datetime) -> list[Problem]:
    """Every connection's problems. One check failing is logged (its type only) and the others still count."""
    results = await asyncio.gather(*(fn(now) for fn in CHECKS.values()), return_exceptions=True)
    out = []
    for name, found in zip(CHECKS, results):
        if isinstance(found, BaseException):
            log.warning("sign-in check %s failed: %s", name, type(found).__name__)
        else:
            out += found
    return out


if __name__ == "__main__":
    # What the heartbeat would say about the sign-ins now
    for p in asyncio.run(check(datetime.now(profile.tz()))) or [Problem("", "Every connection works.")]:
        print(p.text)
