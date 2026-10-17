"""Tests for jarvis.auth.credentials: vault storage, .env fallback and migration, panel entry that never echoes keys."""

import asyncio
import json
import os
import stat

import pytest

from jarvis import config, panel, setup_local
from jarvis.auth import credentials, vault
from jarvis.notify import apns
from jarvis.state import settings

CLIENT = {"installed": {"client_id": "123.apps.googleusercontent.com", "client_secret": "GOCSPX-real-secret",
                        "redirect_uris": ["http://localhost"]}}
SECRET = "a-secret-long-enough-for-the-jwt-signer-0123456789"


@pytest.fixture
def key(monkeypatch, tmp_path):
    """Master key, empty vault, scratch .env and livekit.yaml, no JARVIS keys in the environment."""
    monkeypatch.setattr(config, "MASTER_KEY", vault.new_key())
    monkeypatch.setattr(vault, "ENV_FILE", tmp_path / "dotenv")
    monkeypatch.setattr(config, "GOOGLE_CLIENT_SECRET", "")
    monkeypatch.setattr(config, "SECRETS_DIR", tmp_path)
    monkeypatch.setattr(setup_local, "LIVEKIT_CONFIG", tmp_path / "livekit.yaml")
    monkeypatch.setattr(setup_local, "tailscale_ip", lambda: "")
    for var in credentials.VARS:
        monkeypatch.delenv(var, raising=False)
    for name in vault.names():
        vault.remove(name)
    settings.put(vault.CHECK, "")
    return tmp_path


def test_env_stands_in_until_the_vault_has_it(key, monkeypatch):
    c = credentials.APNS_KEY_ID
    assert credentials.get(c) == "" and credentials.source(c) == ""
    monkeypatch.setenv(c.var, "ABC123DEFG")
    assert credentials.get(c) == "ABC123DEFG" and credentials.source(c) == ".env"
    credentials.put(c, "ZZZ999YYYX")
    assert credentials.get(c) == "ZZZ999YYYX" and credentials.source(c) == "vault"
    assert credentials.clear(c) and credentials.get(c) == "ABC123DEFG"


def test_a_vault_that_wont_open_falls_back_to_env(key, monkeypatch):
    credentials.put(credentials.LIVEKIT_SECRET, SECRET)
    monkeypatch.setenv(credentials.LIVEKIT_SECRET.var, "the-old-one-in-env-0123456789-abcdef")
    monkeypatch.setattr(config, "MASTER_KEY", vault.new_key())  # the wrong key
    assert credentials.get(credentials.LIVEKIT_SECRET) == "the-old-one-in-env-0123456789-abcdef"


def test_adopting_moves_what_env_holds_into_the_vault_and_out_of_env(key, monkeypatch):
    vault.ENV_FILE.write_text(f"JARVIS_USER_NAME=Alex\nLIVEKIT_API_KEY=jarvisab12\nexport LIVEKIT_API_SECRET={SECRET}\n"
                              f"{vault.VAR}={config.MASTER_KEY}\n")
    monkeypatch.setenv("LIVEKIT_API_KEY", "jarvisab12")
    monkeypatch.setenv("LIVEKIT_API_SECRET", SECRET)
    client = key / "client_secret_123.json"
    client.write_text(json.dumps(CLIENT))
    monkeypatch.setattr(config, "GOOGLE_CLIENT_SECRET", str(client))
    assert credentials.adopt() == ["LiveKit API key", "LiveKit API secret", "Google OAuth client"]
    text = vault.ENV_FILE.read_text()
    assert "LIVEKIT" not in text and "JARVIS_USER_NAME=Alex" in text and config.MASTER_KEY in text
    assert stat.S_IMODE(vault.ENV_FILE.stat().st_mode) == 0o600
    assert "LIVEKIT_API_SECRET" not in os.environ and credentials.get(credentials.LIVEKIT_SECRET) == SECRET
    assert json.loads(credentials.get(credentials.GOOGLE_CLIENT)) == CLIENT and client.exists()  # the user removes it
    assert credentials.adopt() == []


def test_without_a_vault_key_adopting_leaves_env_alone(key, monkeypatch):
    monkeypatch.setattr(config, "MASTER_KEY", "not-a-key")
    vault.ENV_FILE.write_text("LIVEKIT_API_KEY=jarvisab12\n")
    monkeypatch.setenv("LIVEKIT_API_KEY", "jarvisab12")
    assert credentials.adopt() == [] and "jarvisab12" in vault.ENV_FILE.read_text()
    assert credentials.get(credentials.LIVEKIT_KEY) == "jarvisab12"


@pytest.mark.parametrize("c, value", [(credentials.APNS_KEY_ID, "abc"), (credentials.LIVEKIT_SECRET, "short"),
                                      (credentials.LIVEKIT_KEY, "has spaces"), (credentials.GOOGLE_CLIENT, "{nope"),
                                      (credentials.GOOGLE_CLIENT, '{"installed": {"client_id": "x"}}')])
def test_a_wrong_looking_value_is_refused_without_repeating_it(key, c, value):
    with pytest.raises(ValueError) as e:
        credentials.put(c, value)
    assert value not in str(e.value) and not vault.has(c.name)


def test_livekits_keys_cant_be_cleared(key):
    with pytest.raises(ValueError, match="replace it"):
        credentials.clear(credentials.LIVEKIT_KEY)


def test_apns_reads_its_key_id_from_the_vault(key, monkeypatch, tmp_path):
    monkeypatch.setattr(apns, "APNS_TEAM_ID", "TEAM123456")
    monkeypatch.setattr(apns, "APNS_KEY_FILE", tmp_path / "apns_key.p8")
    (tmp_path / "apns_key.p8").write_text("key")
    assert not apns.configured()
    credentials.put(credentials.APNS_KEY_ID, "ABC123DEFG")
    assert apns.configured() and apns.key_id() == "ABC123DEFG"


def test_the_panel_sets_keys_shows_only_where_they_come_from_and_rewrites_livekits_config(key, caplog):
    got = asyncio.run(panel.call("key_set", {"name": "livekit.api_secret", "value": SECRET}))
    shown = {k["name"]: k["source"] for k in got["keys"]}
    assert shown["livekit.api_secret"] == "vault" and shown["apns.key_id"] == "" and SECRET not in json.dumps(got)
    assert f": {SECRET}" in (key / "livekit.yaml").read_text()
    assert stat.S_IMODE((key / "livekit.yaml").stat().st_mode) == 0o600
    assert SECRET not in caplog.text and "LiveKit API secret" in caplog.text
    with pytest.raises(ValueError, match="no key called"):
        asyncio.run(panel.call("key_set", {"name": "github.token", "value": "x"}))
    got = asyncio.run(panel.call("key_set", {"name": "apns.key_id", "value": "ABC123DEFG"}))
    got = asyncio.run(panel.call("key_clear", {"name": "apns.key_id"}))
    assert {k["name"]: k["source"] for k in got["keys"]}["apns.key_id"] == ""


def test_the_old_variables_are_kept_out_of_sessions_and_commands(monkeypatch):
    from jarvis.integrations import mcp_servers
    from jarvis.safety import host

    monkeypatch.setattr(host, "dotenv_values", lambda path: {})  # moved out of .env
    monkeypatch.setenv("LIVEKIT_API_SECRET", SECRET)  # as LiveKit's worker puts it back
    assert "LIVEKIT_API_SECRET" not in host.scrubbed_env()
    blanked = mcp_servers.session_env()
    assert all(blanked[var] == "" for var in [*credentials.VARS, "JARVIS_PASSCODE"])
