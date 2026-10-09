"""Signing JARVIS in to an HTTP MCP server with OAuth, as MCP's authorization spec has it, in JARVIS's own browser.

The server's URL answers 401 until signed in. Where to sign in comes from the server itself: its protected resource
metadata (named by the 401's WWW-Authenticate, or at /.well-known/oauth-protected-resource), which names its
authorization server, whose own metadata gives the authorize, token and registration endpoints (`discover`). JARVIS
registers itself there as a client (dynamic client registration, `register`), then opens the authorize page, with
PKCE, in its own browser (jarvis.integrations.browser), on the profile where the user signs in to sites themselves:
they sign in and agree there, and JARVIS's browser catches the redirect back (REDIRECT, never served) and hands over
the code, which is exchanged for tokens (`sign_in`).

The tokens, with the client and the token endpoint, are kept encrypted in the vault as one secret, `<server>.oauth`
(Server.oauth_secret), never shown or logged. Only the launcher's bridge reads them (jarvis.integrations.mcp_bridge),
through `token`, which refreshes an access token that has run out, or that the server refused, and keeps the new one.
A server that can't register clients this way can't be signed in to; it takes a key instead, entered in the control
panel.
"""

import base64
import hashlib
import json
import logging
import re
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx

from jarvis.core import vault
from jarvis.integrations.mcp_servers import Server

log = logging.getLogger("jarvis.integrations.mcp_oauth")

# Where the authorization server sends the browser back. Nothing listens there: JARVIS's browser catches the
# navigation before it leaves (browser.Browser.sign_in), so the code never reaches the network.
REDIRECT = "http://127.0.0.1:47615/jarvis/signed-in"
SIGN_IN_MINUTES = 10
TIMEOUT = 20
EARLY = 60  # seconds before an access token's end that it's refreshed
CLIENT_NAME = "JARVIS"


class SignInError(Exception):
    """Signing in can't go on; its message says why, fit to say aloud."""


@dataclass
class Meta:
    """Where to sign in to one server."""
    resource: str  # the server's URL, as tokens are asked for
    authorize: str
    token: str
    register: str = ""
    scopes: list[str] = field(default_factory=list)


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def _path(url: str) -> str:
    return urlsplit(url).path.rstrip("/")


async def _json(client: httpx.AsyncClient, url: str) -> dict | None:
    try:
        r = await client.get(url, headers={"Accept": "application/json"})
    except httpx.HTTPError:
        return None
    if r.status_code != 200:
        return None
    try:
        found = r.json()
    except ValueError:
        return None
    return found if isinstance(found, dict) else None


async def challenge(url: str, client: httpx.AsyncClient) -> httpx.Response | None:
    """The server's answer to an unsigned first request: a 401 asks for a sign-in. None if it can't be reached."""
    hello = {"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {
        "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "jarvis", "version": "1"}}}
    try:
        return await client.post(url, json=hello, headers={"Accept": "application/json, text/event-stream"})
    except httpx.HTTPError as e:
        log.info("%s couldn't be reached: %s", _origin(url), type(e).__name__)
        return None


def _www_authenticate(response: httpx.Response | None) -> dict[str, str]:
    """The 401's WWW-Authenticate parameters: resource_metadata, scope."""
    said = response.headers.get("www-authenticate", "") if response is not None else ""
    return {k.lower(): v for k, v in re.findall(r'(\w+)="([^"]*)"', said)}


async def discover(url: str, client: httpx.AsyncClient, first: httpx.Response | None = None) -> Meta:
    """Where to sign in to the server at `url`. Raises SignInError if it doesn't say."""
    asked = _www_authenticate(first)
    resource_meta = None
    for where in filter(None, (asked.get("resource_metadata"),
                               f"{_origin(url)}/.well-known/oauth-protected-resource{_path(url)}",
                               f"{_origin(url)}/.well-known/oauth-protected-resource")):
        if resource_meta := await _json(client, where):
            break
    resource_meta = resource_meta or {}
    issuers = [s for s in resource_meta.get("authorization_servers") or [] if isinstance(s, str)]
    issuer = (issuers[0] if issuers else _origin(url)).rstrip("/")  # older servers are their own
    meta = None
    for where in (f"{_origin(issuer)}/.well-known/oauth-authorization-server{_path(issuer)}",
                  f"{_origin(issuer)}/.well-known/openid-configuration{_path(issuer)}",
                  f"{issuer}/.well-known/openid-configuration"):
        if (meta := await _json(client, where)) and meta.get("authorization_endpoint") and meta.get("token_endpoint"):
            break
        meta = None
    if not meta:
        raise SignInError(f"{urlsplit(url).hostname} doesn't say where to sign in, so it takes a key instead.")
    for key in ("authorization_endpoint", "token_endpoint", "registration_endpoint"):
        if meta.get(key) and not str(meta[key]).startswith("https://"):
            raise SignInError(f"{urlsplit(url).hostname}'s sign-in isn't over https, so JARVIS won't use it.")
    if "S256" not in (meta.get("code_challenge_methods_supported") or ["S256"]):
        raise SignInError(f"{urlsplit(url).hostname}'s sign-in doesn't support PKCE, which JARVIS needs.")
    scope = asked.get("scope", "")
    scopes = scope.split() if scope else [s for s in resource_meta.get("scopes_supported") or [] if isinstance(s, str)]
    return Meta(resource_meta.get("resource") or url, meta["authorization_endpoint"], meta["token_endpoint"],
                meta.get("registration_endpoint", ""), scopes)


async def needs_sign_in(url: str, client: httpx.AsyncClient | None = None) -> bool:
    """Whether the server at `url` asks for an OAuth sign-in: it refuses an unsigned request, and says where."""
    own = client is None
    client = client or httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True)
    try:
        first = await challenge(url, client)
        if first is None or first.status_code != 401:
            return False
        try:
            await discover(url, client, first)
        except SignInError:
            return False
        return True
    finally:
        if own:
            await client.aclose()


async def register(meta: Meta, client: httpx.AsyncClient) -> dict:
    """JARVIS as a client of the authorization server: its client_id, and client_secret if it was given one."""
    if not meta.register:
        raise SignInError("Its sign-in doesn't let new apps register themselves, so it takes a key instead.")
    r = await client.post(meta.register, json={
        "client_name": CLIENT_NAME, "redirect_uris": [REDIRECT], "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"], "token_endpoint_auth_method": "none",
        **({"scope": " ".join(meta.scopes)} if meta.scopes else {})})
    if r.status_code not in (200, 201):
        raise SignInError(f"Its sign-in wouldn't register JARVIS (it answered {r.status_code}).")
    found = r.json()
    if not found.get("client_id"):
        raise SignInError("Its sign-in registered JARVIS without giving it an id.")
    return {"client_id": found["client_id"], "client_secret": found.get("client_secret", "")}


def pkce() -> tuple[str, str]:
    """A PKCE verifier and its S256 challenge."""
    verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode()).digest()
    return verifier, base64.urlsafe_b64encode(digest).decode().rstrip("=")


def authorize_url(meta: Meta, client_id: str, code_challenge: str, state: str) -> str:
    query = {"response_type": "code", "client_id": client_id, "redirect_uri": REDIRECT, "state": state,
             "code_challenge": code_challenge, "code_challenge_method": "S256", "resource": meta.resource}
    if meta.scopes:
        query["scope"] = " ".join(meta.scopes)
    return meta.authorize + ("&" if "?" in meta.authorize else "?") + urlencode(query)


def _tokens(found: dict, kept: dict) -> dict:
    """The record kept in the vault after a token answer, over what was kept before."""
    if not found.get("access_token"):
        raise SignInError("The sign-in answered without a token.")
    expires = found.get("expires_in")
    return kept | {"access_token": found["access_token"],
                   "refresh_token": found.get("refresh_token") or kept.get("refresh_token", ""),
                   "expires_at": time.time() + float(expires) if isinstance(expires, (int, float)) else 0,
                   "scope": found.get("scope", kept.get("scope", "")), "at": time.time()}


async def _token_call(client: httpx.AsyncClient, kept: dict, form: dict) -> dict:
    form = form | {"client_id": kept["client_id"], "resource": kept["resource"]}
    if kept.get("client_secret"):
        form["client_secret"] = kept["client_secret"]
    r = await client.post(kept["token_endpoint"], data=form, headers={"Accept": "application/json"})
    if r.status_code != 200:
        raise SignInError(f"The sign-in's token step failed ({r.status_code}).")
    return _tokens(r.json(), kept)


def kept(server: Server) -> dict | None:
    """What's kept of the server's sign-in, or None if it isn't signed in."""
    if not vault.has(server.oauth_secret):
        return None
    try:
        return json.loads(vault.get(server.oauth_secret))
    except ValueError:
        return None


def signed_in(server: Server) -> bool:
    return kept(server) is not None


def forget(server: Server) -> bool:
    return vault.remove(server.oauth_secret)


# Opens a page in JARVIS's browser and returns the address the browser was sent back to (browser.Browser.sign_in)
Opener = Callable[[str, str], Awaitable[str]]


async def sign_in(server: Server, open_page: Opener, client: httpx.AsyncClient | None = None) -> str:
    """Signs JARVIS in to the server: the user signs in on its page in JARVIS's browser, and the tokens are kept.
    Returns a sentence saying so; raises SignInError with what went wrong."""
    if server.transport != "http":
        raise SignInError(f"{server.label} runs on this Mac, so it takes a key, not a sign-in.")
    own = client is None
    client = client or httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True)
    try:
        first = await challenge(server.url, client)
        if first is None:
            raise SignInError(f"{server.label} can't be reached right now.")
        meta = await discover(server.url, client, first)
        registered = await register(meta, client)
        verifier, code_challenge = pkce()
        state = secrets.token_urlsafe(24)
        back = await open_page(authorize_url(meta, registered["client_id"], code_challenge, state), REDIRECT)
        got = {k: v[0] for k, v in parse_qs(urlsplit(back).query).items()}
        if got.get("state") != state:
            raise SignInError("The sign-in came back without JARVIS's own check, so it wasn't used.")
        if got.get("error"):
            raise SignInError(f"The sign-in was turned down ({got['error'][:60]}).")
        if not got.get("code"):
            raise SignInError("The sign-in came back without a code.")
        record = {"client_id": registered["client_id"], "client_secret": registered["client_secret"],
                  "token_endpoint": meta.token, "resource": meta.resource}
        record = await _token_call(client, record, {"grant_type": "authorization_code", "code": got["code"],
                                                    "redirect_uri": REDIRECT, "code_verifier": verifier})
        vault.ensure()
        vault.put(server.oauth_secret, json.dumps(record))
        log.warning("signed in to %s", server.name)
        return f"Signed in to {server.label}."
    finally:
        if own:
            await client.aclose()


async def token(server: Server, refresh: bool = False, client: httpx.AsyncClient | None = None) -> str:
    """The server's access token, refreshed first if it has run out (or is about to), or if `refresh`, as after the
    server refused it. Raises SignInError if it isn't signed in, or the sign-in can't be refreshed."""
    record = kept(server)
    if record is None:
        raise SignInError(f"JARVIS isn't signed in to {server.label}.")
    due = record.get("expires_at") and record["expires_at"] - EARLY < time.time()
    if not (refresh or due):
        return record["access_token"]
    if not record.get("refresh_token"):
        raise SignInError(f"JARVIS's sign-in to {server.label} has run out; sign in again.")
    own = client is None
    client = client or httpx.AsyncClient(timeout=TIMEOUT)
    try:
        record = await _token_call(client, record, {"grant_type": "refresh_token",
                                                    "refresh_token": record["refresh_token"]})
    except (SignInError, httpx.HTTPError) as e:
        raise SignInError(f"JARVIS's sign-in to {server.label} has stopped working; sign in again.") from e
    finally:
        if own:
            await client.aclose()
    vault.put(server.oauth_secret, json.dumps(record))
    return record["access_token"]
