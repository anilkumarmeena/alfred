"""Sign-in health checks, so JARVIS warns (jarvis.tasks.notices) before a tool fails on an expired or revoked login.

  google  each account still refreshes. Google gives no expiry, so this only detects failure (within ~1 h)
  github  gh CLI login, plus the GitHub MCP token if enabled (expiry from github-authentication-token-expiration)
  gitlab  the GitLab MCP token if enabled: active, and expires_at from personal_access_tokens/self

Each Problem has a stable key so it is said once. Warns SIGNIN_WARN_DAYS before expiry. Unreachable
services are silent (offline isn't a sign-in problem). Tokens are never shown or logged; the only write is saving a
refreshed Google access token.
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
    key: str  # stable while the problem lasts
    text: str  # one sentence, safe to speak and show on the lock screen: no tokens or service replies


def _ending(key: str, what: str, ends: date, today: date, fix: str) -> list[Problem]:
    """Warning if `ends` is within SIGNIN_WARN_DAYS, else []."""
    days = (ends - today).days
    if days > SIGNIN_WARN_DAYS:
        return []
    when = "today" if days <= 0 else "tomorrow" if days == 1 else f"in {days} days, on {ends:%a} {ends.day} {ends:%b}"
    return [Problem(f"{key}:ends:{ends.isoformat()}", f"{what} expires {when}: {fix}")]


def _stopped(service: str, name: str, fix: str) -> Problem:
    """MCP server token no longer accepted."""
    return Problem(f"signin:{service}:token:out", f"JARVIS's {name} token has stopped working; {fix}")


# ── Google ──────────────────────────────────────────────────────


def _google_account(label: str) -> str:
    """'' if the sign-in works (refreshing if due), 'out' if not, 'offline' if Google is unreachable."""
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

    from jarvis.tools import google  # configures AI Armory with JARVIS's accounts

    google.fresh()  # accounts added or removed since the last check
    out = []
    for label in google.LABELS:
        try:
            state = await asyncio.wait_for(asyncio.to_thread(_google_account, label), TIMEOUT)
        except Exception as e:  # log the type only: the message may contain request data
            log.warning("checking the %s Google account failed: %s", label, type(e).__name__)
            continue
        if state == "out":
            out.append(Problem(f"signin:google:{label}:out",
                               f"The {label} Google account is signed out, so JARVIS can't reach its mail, calendar "
                               f"or files. Sign it in again with {settings().sign_in(label)}."))
    return out


# ── GitHub ──────────────────────────────────────────────────────


def _expiry(value: str | None) -> date | None:
    """Parse GitHub's '2026-11-01 12:00:00 UTC' or GitLab's '2026-11-01'."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.removesuffix(" UTC").strip()).date()
    except ValueError:
        return None


async def _token_check(url: str, headers: dict[str, str]):
    """GET `url`; None if unreachable."""
    import httpx

    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as c:
            return await c.get(url, headers=headers)
    except httpx.HTTPError as e:
        log.info("sign-in check of %s couldn't connect: %s", url.split("/")[2], type(e).__name__)
        return None


def _secret(server_name: str) -> str | None:
    """The MCP server's token if it is enabled and set, else None."""
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
        out.append(Problem("signin:github:gh:out", "The GitHub CLI on the computer is signed out, so JARVIS and "
                                                   "its tasks can't use it. Sign it in again: gh auth login."))
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
    """All sign-in problems. A failing check is logged (type only) and skipped."""
    results = await asyncio.gather(*(fn(now) for fn in CHECKS.values()), return_exceptions=True)
    out = []
    for name, found in zip(CHECKS, results):
        if isinstance(found, BaseException):
            log.warning("sign-in check %s failed: %s", name, type(found).__name__)
        else:
            out += found
    return out


if __name__ == "__main__":
    # Print what JARVIS would say now
    for p in asyncio.run(check(datetime.now(profile.tz()))) or [Problem("", "Every connection works.")]:
        print(p.text)
