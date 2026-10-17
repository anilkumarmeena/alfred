"""Tests for signing Google accounts in from the control panel (jarvis.integrations.google_signin), its callback page,
the panel's account actions, and the tools following account changes without a restart. Where sign-ins come back,
replays, expiry, pasted addresses and waiting are AI Armory's (its tests/test_google_web.py)."""

import asyncio
import json
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest
from ai_armory.toolsets.google import checks
from ai_armory.toolsets.google.checks import Check
from ai_armory.toolsets.google.common import SCOPES
from ai_armory.toolsets.google.web import OLD
from google_auth_oauthlib.flow import Flow

from conftest import use_profile
from jarvis import config, panel, server, tools
from jarvis.auth import credentials
from jarvis.integrations import accounts, google_signin
from jarvis.integrations.accounts import Account
from jarvis.state import activity
from jarvis.tools import google

TAILNET = "https://mac.tailnet.ts.net"
LOCAL = f"http://127.0.0.1:{config.WEB_PORT}"
DESKTOP = {"installed": {"client_id": "123456-abc.apps.googleusercontent.com", "client_secret": "s",
                         "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                         "token_uri": "https://oauth2.googleapis.com/token", "redirect_uris": ["http://localhost"]}}
EMAILS = {"work": "me@acme.example", "personal": "me@mail.example", "side": "side@mail.example"}
WEB = {"web": DESKTOP["installed"] | {"redirect_uris": [TAILNET + google_signin.CALLBACK]}}


@pytest.fixture
def home(tmp_path, monkeypatch):
    """An isolated profile with a work (Chat) and a personal account, a Desktop app client, and no sign-ins yet."""
    (tmp_path / "secrets").mkdir()
    use_profile(monkeypatch, tmp_path)
    with accounts.change() as found:
        found.add(Account("work", "me@acme.example", chat=True))
        found.add(Account("personal"))
    use_client(monkeypatch, DESKTOP)
    monkeypatch.setattr(checks, "check", lambda label, given, client: ([Check("Gmail", "ok")], EMAILS[label]))
    monkeypatch.setattr(google_signin.signins, "_pending", {})
    monkeypatch.setattr(google_signin.signins, "wait_seconds", 0.05)
    return SimpleNamespace(secrets=tmp_path / "secrets")


def use_client(monkeypatch, client: dict | None) -> None:
    value = json.dumps(client) if client else ""
    monkeypatch.setattr(credentials, "get", lambda c: value if c is credentials.GOOGLE_CLIENT else "")


def query(url: str) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}


def signs_in(monkeypatch, scopes: list[str] | None = None) -> list[str]:
    """Google exchanges any code for a token with `scopes` (all asked for by default); returns the codes it saw."""
    codes = []

    def fetch_token(flow, code):
        codes.append(code)
        flow.oauth2session.token = {"access_token": "access", "refresh_token": "refresh", "expires_at": 4102444800,
                                    "scope": flow.oauth2session.scope if scopes is None else scopes}

    monkeypatch.setattr(Flow, "fetch_token", fetch_token)
    return codes


# ── Starting ────────────────────────────────────────────────────

def test_a_sign_in_comes_back_to_the_web_service_and_hints_the_accounts_email(home):
    started = google_signin.start("work", 0, TAILNET)
    q = query(started["url"])
    assert q["redirect_uri"] == LOCAL + google_signin.CALLBACK and started["paste"] is True  # Desktop app client
    assert q["login_hint"] == "me@acme.example"  # Google picks the account it already knows


def test_signing_in_needs_a_client_a_known_account_and_the_panels_own_address(home, monkeypatch):
    with pytest.raises(ValueError, match="There's no nobody account"):
        google_signin.start("nobody", 0, LOCAL)
    with pytest.raises(ValueError, match="no sign-in step 1"):
        google_signin.start("personal", 1, LOCAL)
    with pytest.raises(ValueError, match="usual address"):
        google_signin.start("personal", 0, "http://evil.example")  # plain http only on this computer
    use_client(monkeypatch, {"installed": {"client_id": "id"}})
    with pytest.raises(ValueError, match="needs a client_id and a client_secret"):
        google_signin.start("personal", 0, LOCAL)
    use_client(monkeypatch, None)
    with pytest.raises(ValueError, match="no Google OAuth client yet"):
        google_signin.start("personal", 0, LOCAL)
    assert google_signin.signins._pending == {}


def test_the_page_says_what_the_client_needs(home, monkeypatch):
    found = act("google_setup", origin=TAILNET)
    assert (found["kind"], found["missing"], found["paste"]) == ("installed", [], True)
    use_client(monkeypatch, WEB)
    found = act("google_setup", origin=TAILNET)
    assert found["kind"] == "web" and found["missing"] == [f"http://localhost:{config.WEB_PORT}/api/google/callback"]
    use_client(monkeypatch, None)
    assert act("google_setup", origin=TAILNET)["kind"] == ""


# ── Finishing ───────────────────────────────────────────────────

def test_coming_back_saves_the_sign_in_and_the_email_once(home, monkeypatch):
    codes = signs_in(monkeypatch)
    state = google_signin.start("personal", 0, LOCAL)["state"]
    assert google_signin.signins.status(state) == {"state": "waiting"}
    result = asyncio.run(google_signin.signins.finish(state, "the-code"))
    assert (result.state, codes) == ("done", ["the-code"])
    assert result.said == "Signed the personal Google account in as me@mail.example."
    assert (result.email, result.notes) == ("me@mail.example", [])
    token = json.loads((home.secrets / "google-personal.json").read_text())
    assert token["refresh_token"] == "refresh" and set(token["scopes"]) == set(SCOPES)
    assert accounts.load().need("personal").email == "me@mail.example"
    assert activity.recent()[0]["text"].startswith("Signed the personal Google account in as me@mail.example.")
    assert google_signin.signins.status(state)["state"] == "done"
    again = asyncio.run(google_signin.signins.finish(state, "the-code"))  # a replayed callback does nothing
    assert (again.state, again.said, codes) == ("failed", OLD, ["the-code"])
















# ── The callback page and the panel ─────────────────────────────

def callback(**params) -> tuple[int, str]:
    request = SimpleNamespace(query=params)
    r = asyncio.run(server.google_callback(request))
    return r.status, r.text


def test_the_callback_page_says_how_it_went(home, monkeypatch):
    signs_in(monkeypatch)
    state = google_signin.start("personal", 0, LOCAL)["state"]
    status, page = callback(state=state, code="code")
    assert status == 200 and "<h1>Signed in</h1>" in page and "<code>me@mail.example</code>" in page
    assert "close this tab" in page and 'class="c-ok"' in page
    status, page = callback(state=state, code="code")
    assert "<h1>Not signed in</h1>" in page and "old or was already used" in page


def act(name: str, **body):
    return asyncio.run(panel.call(name, body))


def test_the_panel_adds_signs_in_and_removes_accounts(home, monkeypatch):
    signs_in(monkeypatch)
    listed = act("account_add", label="side", note="side project")
    assert [(a["label"], a["default"], a["signed_in"]) for a in listed] == [
        ("work", True, False), ("personal", False, False), ("side", False, False)]
    with pytest.raises(ValueError, match="There's already a side account"):
        act("account_add", label="side")
    started = act("google_sign_in", label="side", step=0, origin=TAILNET)
    assert started["paste"] and act("google_sign_in_status", state=started["state"]) == {"state": "waiting"}
    assert act("google_sign_in_paste", address=f"{LOCAL}{google_signin.CALLBACK}?state={started['state']}&code=c") == {}
    done = act("google_sign_in_status", state=started["state"])  # what the panel's waiting status request hears
    assert done["state"] == "done" and next(a for a in done["accounts"] if a["label"] == "side")["signed_in"]
    left = act("account_remove", label="side")
    assert [a["label"] for a in left] == ["work", "personal"] and left[0]["default"]
    assert not (home.secrets / "google-side.json").exists()


# ── Following account changes without a restart ─────────────────

def test_the_tools_follow_accounts_added_and_removed_elsewhere(home):
    google.fresh()
    assert google.LABELS == ["work", "personal"]
    assert tools.get("gmail_search").armory.input_schema["properties"]["account"]["enum"] == ["work", "personal", "all"]
    assert tools.get("gmail_attachments").properties["account"]["enum"] == ["work", "personal"]
    before = tools.get("gmail_search")
    google.fresh()
    assert tools.get("gmail_search") is before  # unchanged: nothing registered again
    with accounts.change() as found:
        found.add(Account("side"))
    google.fresh()
    assert "side" in tools.get("gmail_attachments").properties["account"]["enum"]
    assert "side" in tools.get("gmail_search").armory.input_schema["properties"]["account"]["enum"]
    assert "side" in google.ACCOUNTS.labels
    with accounts.change() as found:
        for label in list(found.labels):
            found.remove(label)
    google.fresh()
    assert google.LABELS == []
    assert tools.get("gmail_search") is None and tools.get("gmail_attachments") is None  # no account, no tools


def test_a_session_starts_with_the_accounts_as_they_are(home):
    from jarvis.brain import persona, sdk, side

    with accounts.change() as found:
        found.add(Account("side", note="side project"))
    _, allowed = sdk.jarvis_server(side.reads)  # picked after the accounts are reread
    assert "mcp__jarvis__gmail_search" in allowed and '"side": side project' in persona.persona()






# ── Checking each Google API ────────────────────────────────────

def test_a_sign_in_with_an_api_turned_off_says_so_and_checking_again_clears_it(home, monkeypatch):
    signs_in(monkeypatch)
    off = Check("Calendar", "off", "https://console.cloud.google.com/apis/library/calendar-json.googleapis.com")
    monkeypatch.setattr(checks, "check", lambda label, given, client: ([Check("Gmail", "ok"), off], ""))
    state = google_signin.start("personal", 0, LOCAL)["state"]
    result = asyncio.run(google_signin.signins.finish(state, "code"))
    assert result.said == ("Signed the personal Google account in. Calendar is turned off in its Google Cloud project, "
                           "so the tools can't use it yet.")
    page = google_signin.signins.page(result)
    assert "Signed in, almost ready" in page and "Turn on</a>" in page and "check again in JARVIS" in page
    monkeypatch.setattr(checks, "check", lambda label, given, client: ([Check("Gmail", "ok"), Check("Calendar", "ok")],
                                                                          EMAILS[label]))
    again = act("google_check", label="personal")
    assert again["said"] == "Checked the personal Google account as me@mail.example."
    assert next(a for a in again["accounts"] if a["label"] == "personal")["email"] == "me@mail.example"
    with pytest.raises(ValueError, match="is not signed in yet. Sign it in with: the Sign in button"):
        google_signin.recheck("work")


def test_signing_in_as_another_address_takes_it_over_and_says_so(home, monkeypatch):
    signs_in(monkeypatch)
    monkeypatch.setattr(checks, "check", lambda label, given, client: ([Check("Gmail", "ok")], "new@acme.example"))
    state = google_signin.start("work", 2, LOCAL)["state"]  # the last step: no Chat to check
    result = asyncio.run(google_signin.signins.finish(state, "code"))
    assert "The work account is new@acme.example now, not me@acme.example." in result.notes
    assert accounts.load().need("work").email == "new@acme.example"
