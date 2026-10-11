"""Tests for jarvis.integrations.signins and the notices it and usage limits give: once per problem or window."""

import asyncio
import json
from datetime import datetime, timedelta

import pytest
from test_notices import AFTERNOON, make

from conftest import use_profile
from jarvis.brain import usage
from jarvis.integrations import signins
from jarvis.tasks import notices


@pytest.fixture
def db(tmp_path, monkeypatch):
    use_profile(monkeypatch, tmp_path)


# ── As notices ──────────────────────────────────────────────────


def test_usage_warnings_are_said_once_per_window(tmp_path, monkeypatch, db):
    from claude_agent_sdk import RateLimitInfo

    monkeypatch.setattr(usage, "USAGE_FILE", tmp_path / "usage.json")
    stamp = AFTERNOON.timestamp()
    usage.record(RateLimitInfo(status="allowed_warning", resets_at=int(stamp + 3600), rate_limit_type="five_hour",
                               utilization=0.9), now=stamp)
    told, notify = make(tmp_path, {"usage": notices.usage_notices})
    asyncio.run(told.check())
    asyncio.run(told.check())
    assert notify.sent == [("JARVIS · usage", "JARVIS has used 90% of Claude's five-hour limit; it resets at 4 PM.")]


def test_sign_in_problems_are_ongoing_notices(monkeypatch):
    async def check(now):
        return [signins.Problem("signin:gitlab:token:out", "JARVIS's GitLab token has stopped working.")]

    monkeypatch.setattr(signins, "check", check)
    found = asyncio.run(notices.signin_notices(AFTERNOON))
    assert [(n.key, n.source, n.ongoing) for n in found] == [("signin:gitlab:token:out", "signins", True)]


# ── The sign-in checks ──────────────────────────────────────────


class Reply:
    def __init__(self, status: int, headers: dict | None = None, body: dict | None = None) -> None:
        self.status_code, self.headers, self.body = status, headers or {}, body

    def json(self):
        return self.body


@pytest.fixture
def services(monkeypatch):
    """Fake gh CLI, vault tokens and service answers, set per test."""
    state = {"gh": (0, "Logged in to github.com account me"), "tokens": {}, "replies": {}, "asked": []}

    async def run_try(*argv, timeout=30):
        return state["gh"]

    async def token_check(url, headers):
        state["asked"].append((url, headers))
        return state["replies"].get(url.split("/")[2])

    monkeypatch.setattr(signins, "run_try", run_try)
    monkeypatch.setattr(signins, "_secret", lambda name: state["tokens"].get(name))
    monkeypatch.setattr(signins, "_token_check", token_check)
    monkeypatch.setattr(signins, "CHECKS", {"github": signins.github, "gitlab": signins.gitlab})
    return state


def problems(now=AFTERNOON) -> dict[str, str]:
    return {p.key: p.text for p in asyncio.run(signins.check(now))}


def test_everything_working_is_nothing_to_say(services):
    services["tokens"] = {"github": "t1"}
    services["replies"] = {"api.github.com": Reply(200)}
    assert problems() == {}


def test_the_gh_cli_signed_out_is_said_but_not_being_offline(services):
    services["gh"] = (1, "github.com\n  X Failed to log in to github.com account me (keyring)\n"
                         "  - The token in keyring is invalid.")
    assert list(problems()) == ["signin:github:gh:out"]
    services["gh"] = (1, "error connecting to api.github.com: check your internet connection")
    assert problems() == {}


def test_a_github_token_that_expires_soon_is_warned_about(services):
    services["tokens"] = {"github": "t1"}
    ends = (AFTERNOON + timedelta(days=3)).strftime("%Y-%m-%d 10:00:00 UTC")
    services["replies"] = {"api.github.com": Reply(200, {"github-authentication-token-expiration": ends})}
    found = problems()
    key = f"signin:github:token:ends:{(AFTERNOON + timedelta(days=3)).date().isoformat()}"
    assert found == {key: "JARVIS's GitHub token expires in 3 days, on Tue 29 Sep: make a new one on GitHub and enter "
                          "it in the control panel's Connections page."}
    assert services["asked"][0][1]["Authorization"] == "Bearer t1"
    far = (AFTERNOON + timedelta(days=40)).strftime("%Y-%m-%d 10:00:00 UTC")
    services["replies"] = {"api.github.com": Reply(200, {"github-authentication-token-expiration": far})}
    assert problems() == {}


def test_tokens_that_stopped_working(services):
    services["tokens"] = {"github": "t1", "gitlab": "t2"}
    services["replies"] = {"api.github.com": Reply(401), "gitlab.com": Reply(200, body={"active": False})}
    assert set(problems()) == {"signin:github:token:out", "signin:gitlab:token:out"}


def test_a_gitlab_token_ending_tomorrow(services, monkeypatch):
    from jarvis.integrations import mcp_servers

    class Server:
        env = {"GITLAB_API_URL": "https://gitlab.acme.example/api/v4"}

    monkeypatch.setattr(mcp_servers, "get", lambda name: Server())
    services["tokens"] = {"gitlab": "t2"}
    services["replies"] = {"gitlab.acme.example": Reply(200, body={"active": True, "expires_at": "2026-09-27"})}
    assert list(problems().values()) == ["JARVIS's GitLab token expires tomorrow: make a new one in GitLab and enter it "
                                         "in the control panel's Connections page."]
    assert services["asked"][0] == ("https://gitlab.acme.example/api/v4/personal_access_tokens/self",
                                    {"PRIVATE-TOKEN": "t2"})


def test_no_token_set_is_nothing_checked(services):
    assert problems() == {} and services["asked"] == []


def test_one_check_failing_leaves_the_others(services, monkeypatch):
    async def broken(now):
        raise OSError("gh isn't installed")

    monkeypatch.setattr(signins, "CHECKS", {"github": broken, "gitlab": signins.gitlab})
    services["tokens"] = {"gitlab": "t2"}
    services["replies"] = {"gitlab.com": Reply(401)}
    assert list(problems()) == ["signin:gitlab:token:out"]


def test_a_google_sign_in_that_no_longer_refreshes(tmp_path, monkeypatch):
    from ai_armory.toolsets.google import common
    from google.auth.exceptions import RefreshError
    from google.oauth2.credentials import Credentials

    tokens = {"work": tmp_path / "work.json", "personal": tmp_path / "personal.json"}
    tokens["work"].write_text(json.dumps({"token": "a", "refresh_token": "r", "client_id": "c", "client_secret": "s",
                                          "expiry": "2020-01-01T00:00:00Z"}))
    monkeypatch.setattr(common, "token_path", lambda label: tokens[label])

    def refused(self, request):
        raise RefreshError("invalid_grant: Token has been expired or revoked.")

    monkeypatch.setattr(Credentials, "refresh", refused)
    found = asyncio.run(signins.google_accounts(AFTERNOON))
    assert {p.key for p in found} == {"signin:google:work:out", "signin:google:personal:out"}  # personal: never in
    assert all("invalid_grant" not in p.text for p in found)
    assert "Sign it in again with the Sign in button beside the work account" in found[0].text
