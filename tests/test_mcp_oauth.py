"""Tests for jarvis.integrations.mcp_oauth: discovery, refusals, denied sign-ins, keeping and refreshing tokens."""

import asyncio
import base64
import hashlib
import json
import time
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from test_connections import WIKI, Auth, serving
from test_mcp_servers import fresh  # noqa: F401  (the fixture)

from jarvis.auth import vault
from jarvis.integrations import mcp_oauth
from jarvis.integrations.mcp_servers import Server

URL = "https://mcp.notes.example/v1/mcp"
AS_META = {"authorization_endpoint": "https://login.notes.example/authorize",
           "token_endpoint": "https://login.notes.example/token",
           "registration_endpoint": "https://login.notes.example/register"}


def answering(pages: dict[str, dict], seen: list[str] | None = None):
    """Handler serving `pages` (URL to JSON), 404 otherwise; records each URL asked for in `seen`."""
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(str(request.url))
        page = pages.get(str(request.url))
        return httpx.Response(200, json=page) if page is not None else httpx.Response(404)
    return handler


def discover(pages: dict[str, dict], first: httpx.Response | None = None, seen: list[str] | None = None):
    return asyncio.run(mcp_oauth.discover(URL, serving(answering(pages, seen)), first))


# ── Where to sign in ────────────────────────────────────────────

def test_with_no_hint_the_well_known_places_are_tried_path_first():
    seen = []
    meta = discover({
        "https://mcp.notes.example/.well-known/oauth-protected-resource": {
            "authorization_servers": ["https://login.notes.example/tenant"], "scopes_supported": ["read", 7]},
        "https://login.notes.example/.well-known/openid-configuration/tenant": AS_META,
    }, seen=seen)
    assert seen == ["https://mcp.notes.example/.well-known/oauth-protected-resource/v1/mcp",
                    "https://mcp.notes.example/.well-known/oauth-protected-resource",
                    "https://login.notes.example/.well-known/oauth-authorization-server/tenant",
                    "https://login.notes.example/.well-known/openid-configuration/tenant"]
    assert (meta.resource, meta.token, meta.register) == \
        (URL, AS_META["token_endpoint"], AS_META["registration_endpoint"])
    assert meta.scopes == ["read"]  # only the names


def test_an_older_server_is_its_own_authorization_server_and_the_401s_scope_wins():
    first = httpx.Response(401, headers={"WWW-Authenticate": 'Bearer realm="x", scope="notes:read notes:write"'})
    meta = discover({"https://mcp.notes.example/.well-known/oauth-authorization-server": AS_META}, first)
    assert meta.authorize == AS_META["authorization_endpoint"] and meta.scopes == ["notes:read", "notes:write"]


@pytest.mark.parametrize("change, said", [
    ({"token_endpoint": "http://login.notes.example/token"}, "isn't over https"),
    ({"registration_endpoint": "http://login.notes.example/register"}, "isn't over https"),
    ({"code_challenge_methods_supported": ["plain"]}, "doesn't support PKCE"),
    ({"authorization_endpoint": ""}, "doesn't say where to sign in"),
])
def test_a_sign_in_jarvis_wont_use_is_refused(change, said):
    with pytest.raises(mcp_oauth.SignInError, match=said):
        discover({"https://mcp.notes.example/.well-known/oauth-authorization-server": AS_META | change})


def test_a_server_that_says_nothing_takes_a_key():
    with pytest.raises(mcp_oauth.SignInError, match="mcp.notes.example doesn't say where to sign in"):
        discover({})


def test_only_a_401_that_says_where_to_sign_in_needs_one():
    def unreachable(request):
        raise httpx.ConnectError("no route")

    assert not asyncio.run(mcp_oauth.needs_sign_in(URL, serving(unreachable)))
    assert not asyncio.run(mcp_oauth.needs_sign_in(URL, serving(lambda r: httpx.Response(401))))  # says nothing more
    assert not asyncio.run(mcp_oauth.needs_sign_in(URL, serving(lambda r: httpx.Response(403))))


# ── Signing in ──────────────────────────────────────────────────

def test_the_authorize_page_asks_with_pkce_for_the_resource_and_its_scopes():
    verifier, challenge = mcp_oauth.pkce()
    assert base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=") == challenge
    assert mcp_oauth.pkce()[0] != verifier
    meta = mcp_oauth.Meta(URL, "https://login.notes.example/authorize?tenant=t1", "https://t", scopes=["a", "b"])
    url = mcp_oauth.authorize_url(meta, "c-1", challenge, "st")
    query = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
    assert url.startswith("https://login.notes.example/authorize?tenant=t1&")
    assert query | {"tenant": "t1"} == query and query["redirect_uri"] == mcp_oauth.REDIRECT
    assert (query["code_challenge"], query["code_challenge_method"], query["resource"], query["scope"]) == \
        (challenge, "S256", URL, "a b")


@pytest.mark.parametrize("status, body, said", [
    (400, {"error": "invalid_client_metadata"}, "wouldn't register JARVIS"),
    (201, {"client_secret": "s"}, "without giving it an id"),
])
def test_a_registration_that_fails_says_why(status, body, said):
    meta = mcp_oauth.Meta(URL, "https://a", "https://t", "https://login.notes.example/register")
    with pytest.raises(mcp_oauth.SignInError, match=said):
        asyncio.run(mcp_oauth.register(meta, serving(lambda r: httpx.Response(status, json=body))))


@pytest.mark.parametrize("back, said", [
    ("?error=access_denied&state={state}", "turned down \\(access_denied\\)"),
    ("?state={state}", "without a code"),
    ("", "without JARVIS's own check"),
])
def test_a_sign_in_that_comes_back_without_a_code_keeps_nothing(fresh, back, said):
    auth = Auth()

    async def browser(url: str, back_to: str) -> str:
        state = parse_qs(urlsplit(url).query)["state"][0]
        return back_to + back.format(state=state)

    with pytest.raises(mcp_oauth.SignInError, match=said):
        asyncio.run(mcp_oauth.sign_in(WIKI, browser, serving(auth)))
    assert not mcp_oauth.signed_in(WIKI)


def test_a_server_on_this_mac_or_out_of_reach_cant_be_signed_in_to(fresh):
    async def never(url, back):
        raise AssertionError("no page should open")

    with pytest.raises(mcp_oauth.SignInError, match="runs on this computer"):
        asyncio.run(mcp_oauth.sign_in(Server("local", "Local", command="local-mcp"), never))

    def unreachable(request):
        raise httpx.ConnectTimeout("slow")

    with pytest.raises(mcp_oauth.SignInError, match="can't be reached right now"):
        asyncio.run(mcp_oauth.sign_in(WIKI, never, serving(unreachable)))


# ── The tokens ──────────────────────────────────────────────────

def keep(**record) -> None:
    vault.ensure()
    vault.put(WIKI.oauth_secret, json.dumps({"client_id": "c-1", "token_endpoint": "https://auth.wiki.example/token",
                                             "resource": WIKI.url, "access_token": "a-1", "refresh_token": "r-1",
                                             "expires_at": time.time() + 3600} | record))


def refusing(request: httpx.Request) -> httpx.Response:
    raise AssertionError(f"no call expected, got {request.url}")


def test_a_token_still_good_is_used_as_kept(fresh):
    keep()
    assert asyncio.run(mcp_oauth.token(WIKI, client=serving(refusing))) == "a-1"
    keep(expires_at=0)  # no end given
    assert asyncio.run(mcp_oauth.token(WIKI, client=serving(refusing))) == "a-1"


def test_a_token_about_to_run_out_is_refreshed_with_the_clients_secret_and_kept(fresh):
    keep(expires_at=time.time() + mcp_oauth.EARLY - 1, client_secret="shh")
    forms = []

    def answer(request: httpx.Request) -> httpx.Response:
        forms.append({k: v[0] for k, v in parse_qs(request.content.decode()).items()})
        return httpx.Response(200, json={"access_token": "a-2", "refresh_token": "r-2", "expires_in": 600,
                                         "scope": "wiki"})

    assert asyncio.run(mcp_oauth.token(WIKI, client=serving(answer))) == "a-2"
    assert forms == [{"grant_type": "refresh_token", "refresh_token": "r-1", "client_id": "c-1",
                      "resource": WIKI.url, "client_secret": "shh"}]
    kept = mcp_oauth.kept(WIKI)
    assert kept is not None and (kept["access_token"], kept["refresh_token"], kept["scope"]) == ("a-2", "r-2", "wiki")
    assert time.time() + 590 < kept["expires_at"] <= time.time() + 600


def test_a_sign_in_that_cant_be_refreshed_says_to_sign_in_again_and_keeps_the_old(fresh):
    keep(expires_at=1, refresh_token="")
    with pytest.raises(mcp_oauth.SignInError, match="has run out; sign in again"):
        asyncio.run(mcp_oauth.token(WIKI, client=serving(refusing)))
    keep()
    for answer in (lambda r: httpx.Response(400, json={"error": "invalid_grant"}),
                   lambda r: httpx.Response(200, json={"token_type": "bearer"})):  # no token in it
        with pytest.raises(mcp_oauth.SignInError, match="stopped working; sign in again"):
            asyncio.run(mcp_oauth.token(WIKI, refresh=True, client=serving(answer)))
    assert (mcp_oauth.kept(WIKI) or {})["access_token"] == "a-1"


def test_not_signed_in_a_garbled_record_and_forgetting(fresh):
    with pytest.raises(mcp_oauth.SignInError, match="isn't signed in to Wiki"):
        asyncio.run(mcp_oauth.token(WIKI))
    vault.ensure()
    vault.put(WIKI.oauth_secret, "not json")
    assert mcp_oauth.kept(WIKI) is None and not mcp_oauth.signed_in(WIKI)
    keep()
    assert mcp_oauth.signed_in(WIKI) and mcp_oauth.forget(WIKI)
    assert not mcp_oauth.signed_in(WIKI) and not mcp_oauth.forget(WIKI)
