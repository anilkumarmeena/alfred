"""Tests for core.vault: master key, AES-256-GCM with name and key id bound, wrong-key check, rotation, .env."""

import base64
import stat
from pathlib import Path

import pytest

from jarvis import config
from jarvis.auth import vault
from jarvis.state import settings


@pytest.fixture
def key(monkeypatch, tmp_path):
    """A fresh master key as config found it, an empty vault, and a scratch .env."""
    value = vault.new_key()
    monkeypatch.setattr(config, "MASTER_KEY", value)
    monkeypatch.setattr(vault, "ENV_FILE", tmp_path / "dotenv")  # not named .env: sandboxes hide those
    for name in vault.names():
        vault.remove(name)
    settings.put(vault.CHECK, "")
    return value


def test_a_secret_goes_in_encrypted_and_comes_out_whole(key):
    vault.put("github.token", "ghp_real_value")
    assert vault.get("github.token") == "ghp_real_value" and vault.has("github.token")
    with vault._db() as db:
        blob, kid = db.execute("select blob, key_id from secrets where name = 'github.token'").fetchone()
    assert b"ghp_real_value" not in blob and kid == vault.keys()[0].id and len(blob) == 12 + len("ghp_real_value") + 16
    assert vault.names() == {"github.token": kid}


def test_each_encryption_has_its_own_nonce(key):
    k = vault.keys()[0]
    assert vault.encrypt("a", b"same", k) != vault.encrypt("a", b"same", k)


def test_a_blob_copied_under_another_name_wont_open(key):
    vault.put("github.token", "ghp_real_value")
    with vault._db() as db:
        db.execute("insert into secrets select 'gitlab.token', blob, key_id, created, updated from secrets "
                   "where name = 'github.token'")
    with pytest.raises(vault.VaultError, match="changed or copied"):
        vault.get("gitlab.token")


def test_a_wrong_key_is_caught_before_anything_is_read_or_written(key, monkeypatch):
    vault.put("github.token", "ghp_real_value")
    monkeypatch.setattr(config, "MASTER_KEY", vault.new_key())
    for attempt in (lambda: vault.get("github.token"), lambda: vault.put("other", "x")):
        with pytest.raises(vault.VaultError, match="isn't the key"):
            attempt()


def test_no_key_and_a_malformed_key_say_what_to_do(key, monkeypatch):
    monkeypatch.setattr(config, "MASTER_KEY", "")
    with pytest.raises(vault.VaultError, match="make vault ARGS=init"):
        vault.put("x", "y")
    monkeypatch.setattr(config, "MASTER_KEY", "not-a-key")
    with pytest.raises(vault.VaultError, match="32 bytes"):
        vault.keys()


def test_the_key_comes_from_env_file_when_the_environment_has_none(key, monkeypatch):
    monkeypatch.setattr(config, "MASTER_KEY", "")  # as in the launcher, under a CLI that blanked it
    vault.ENV_FILE.write_text(f"OTHER=1\n{vault.VAR}={key}\n")
    assert [k.id for k in vault.keys()] == [vault.key_id(base64.urlsafe_b64decode(key + "="))]


def test_the_key_and_its_id_never_show_in_a_repr(key):
    k = vault.keys()[0]
    assert key not in repr(k) and repr(k) == f"Key({k.id})" and len(k.id) == 16


def test_rotating_reencrypts_everything_and_keeps_the_old_key_after_the_new(key):
    vault.put("github.token", "one")
    vault.put("gitlab.token", "two")
    old = vault.keys()[0].id
    vault.rotate()
    text = vault.ENV_FILE.read_text()
    assert text.count(vault.VAR) == 1 and key in text  # the old key kept, second
    found = vault.keys()
    assert found[1].id == old and found[0].id != old
    assert set(vault.names().values()) == {found[0].id}
    assert vault.get("github.token") == "one" and vault.get("gitlab.token") == "two"


def test_a_secret_under_a_key_thats_gone_says_so(key, monkeypatch):
    vault.put("github.token", "one")
    vault.rotate()
    newest = base64.urlsafe_b64encode(vault.keys()[0].secret).decode().rstrip("=")
    with vault._db() as db:  # as if one had been missed under the old key
        db.execute("update secrets set key_id = 'deadbeefdeadbeef'")
    monkeypatch.setattr(config, "MASTER_KEY", newest)
    with pytest.raises(vault.VaultError, match="isn't in JARVIS_MASTER_KEY"):
        vault.get("github.token")


def test_reencrypt_moves_secrets_under_the_first_key(key, monkeypatch):
    vault.put("github.token", "one")
    monkeypatch.setattr(config, "MASTER_KEY", f"{vault.new_key()},{key}")  # a new key put first by hand
    assert vault.reencrypt() == 1
    assert set(vault.names().values()) == {vault.keys()[0].id} and vault.get("github.token") == "one"


def test_init_writes_the_key_into_env_file_mode_600_keeping_the_rest(key, monkeypatch):
    monkeypatch.setattr(config, "MASTER_KEY", "")
    vault.ENV_FILE.write_text(f"JARVIS_PASSCODE=123456\n{vault.VAR}=\n")
    said = vault.init()
    text = vault.ENV_FILE.read_text()
    assert "JARVIS_PASSCODE=123456" in text and text.count(vault.VAR) == 1 and config.MASTER_KEY in text
    assert stat.S_IMODE(vault.ENV_FILE.stat().st_mode) == 0o600 and config.MASTER_KEY not in said
    assert "already" in vault.init()


def test_init_print_shows_the_key_once_and_writes_no_file(key, monkeypatch):
    monkeypatch.setattr(config, "MASTER_KEY", "")
    said = vault.init(print_only=True)
    assert config.MASTER_KEY in said and not vault.ENV_FILE.exists()


def test_a_key_made_on_the_side_goes_into_env(key, monkeypatch):
    monkeypatch.setattr(config, "MASTER_KEY", "")
    vault.ensure()  # as set-secret does
    assert f"{vault.VAR}={config.MASTER_KEY}" in vault.ENV_FILE.read_text() and vault.keys()


def test_rotating_writes_the_new_key_first_so_a_failed_write_moves_nothing(key, monkeypatch):
    vault.put("github.token", "one")

    def fail(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(vault, "write_atomic", fail)
    with pytest.raises(OSError):
        vault.rotate()
    assert set(vault.names().values()) == {vault.keys()[0].id} and vault.get("github.token") == "one"


def test_reset_forgets_every_secret_and_starts_a_new_key(key, monkeypatch, capsys):
    vault.put("github.token", "one")
    monkeypatch.setattr(config, "MASTER_KEY", vault.new_key())  # the old key lost
    assert vault.main(["reset", "--yes"]) == 0
    assert vault.names() == {} and "github.token" in capsys.readouterr().out
    vault.put("github.token", "again")  # the new key works, checked afresh
    assert vault.get("github.token") == "again"


def test_status_lists_names_never_values(key, capsys):
    vault.put("github.token", "ghp_real_value")
    vault.main([])
    out = capsys.readouterr().out
    assert "github.token" in out and "ghp_real_value" not in out and key not in out


def test_config_takes_the_key_out_of_the_environment():
    text = Path(config.__file__).read_text()
    assert 'MASTER_KEY = os.environ.pop("JARVIS_MASTER_KEY"' in text
