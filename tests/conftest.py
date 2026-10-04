"""Set up before any test module imports jarvis: an install of the tests' own (JARVIS_DATA_DIR, its data folder and
database), and a profile in it every test runs as (PROFILE), holding the Google accounts and "about you" below, so the
tools' account lists and the prompt are the same everywhere and this machine's data/ is never touched. The vault has no
key and a scratch .env, and no token comes from this machine.

A test that wants a profile of its own, its data, database and sign-ins apart from every other test's, takes the
`own_profile` fixture, or calls use_profile() for one in a folder of its choosing."""

import atexit
import dataclasses
import os
import shutil
import tempfile
from pathlib import Path

import pytest

SCRATCH = Path(tempfile.mkdtemp(prefix="jarvis-tests-"))
atexit.register(shutil.rmtree, SCRATCH, True)
os.environ["JARVIS_DATA_DIR"] = str(SCRATCH / "data")
os.environ["JARVIS_DB_FILE"] = str(SCRATCH / "data" / "tasks.db")
os.environ["JARVIS_GOAL_LOOP"] = "0"  # no model calls when a task finishes; test_goal turns it on, with a fake check
os.environ["JARVIS_CONSENT"] = "0"  # no model call for a yes the word lists can't read; test_consent fakes one
os.environ["JARVIS_ANNOUNCE"] = "0"  # a task's end in fixed words, without a model call; test_announce fakes one
os.environ["JARVIS_INJECTION_CHECK"] = "0"  # sessions' tool results unscreened; test_injection fakes the check

# After JARVIS_DATA_DIR, which jarvis.core.config reads
from jarvis.core import config, credentials, logins, profile, transcribe, vault  # noqa: E402
from jarvis.integrations import accounts  # noqa: E402
from jarvis.state import conversation, settings  # noqa: E402

PROFILE = profile.Profile("test", "Sam", config.mac_timezone() or "UTC", config.DATA_DIR, config.DB_FILE,
                          SCRATCH / "secrets")
profile.activate(PROFILE)
transcribe.LOG_FILE = SCRATCH / "transcribe.log"  # the transcriber's errors: none in this checkout's logs/


def use_profile(monkeypatch: pytest.MonkeyPatch, data: Path | None = None, **changes) -> profile.Profile:
    """Runs the rest of the test as a profile of its own: its data in `data` (its database data/tasks.db and its
    sign-ins data/secrets, unless `changes` say otherwise), or with no `data`, the current one's with `changes`, such
    as another name."""
    folders = {"data": data, "db": data / "tasks.db", "secrets": data / "secrets"} if data else {}
    mine = dataclasses.replace(profile.current() if data is None else PROFILE, **folders | changes)
    monkeypatch.setattr(profile, "_process", mine)
    return mine


def use_task_cache(monkeypatch: pytest.MonkeyPatch, folder: Path) -> Path:
    """Background tasks' cache folder (TASK_CACHE_DIR) is `folder` for the rest of the test, in every part of the
    worker that reads it."""
    from jarvis.tasks import briefs, host_tools, worker

    for module in (worker, host_tools, briefs):
        monkeypatch.setattr(module, "TASK_CACHE_DIR", folder)
    return folder


@pytest.fixture(autouse=True)
def fresh_conversation(monkeypatch) -> conversation.Conversation:
    """Each test talks in a conversation of its own: what one heard or offered never reaches the next."""
    monkeypatch.setattr(conversation, "_process", fresh := conversation.Conversation())
    return fresh


@pytest.fixture
def own_profile(monkeypatch, tmp_path) -> profile.Profile:
    """A profile of the test's own, in tmp_path."""
    return use_profile(monkeypatch, tmp_path)


# No master key, .env or tokens of this machine's: the vault's key goes in a scratch .env, and GitHub and GitLab start
# disabled, as on a Mac with no tokens yet
config.MASTER_KEY = ""
vault.ENV_FILE = SCRATCH / "dotenv"  # not named .env, which sandboxes hide
logins.FILE = SCRATCH / "logins.json"  # none: the first-setup passcode, JARVIS_PASSCODE, is what's checked
for _var in ("GITHUB_PERSONAL_ACCESS_TOKEN", "GITLAB_PERSONAL_ACCESS_TOKEN", "JARVIS_GITHUB_MCP", logins.VAR,
             *credentials.VARS):
    os.environ.pop(_var, None)
config.GOOGLE_CLIENT_SECRET = ""  # no client_secret*.json of this machine's
ACCOUNTS = accounts.Accounts([accounts.Account("work", "me@acme.example", "for work", chat=True),
                              accounts.Account("personal", "me@mail.example", "their own")], "work")
ABOUT = "Works at Acme on its video platform.\n- Also runs their own ventures, kept apart from the day job."

with accounts.change() as found:
    found.accounts, found.default = list(ACCOUNTS.accounts), ACCOUNTS.default
settings.set_about(ABOUT)


@pytest.fixture
def voice(monkeypatch):
    """The voice brain on fake CLIs (tests/fake_cli.py): voice(*turns, **settings) gives a fake_cli.Voice whose
    CLIs play `turns`, each a script of what the CLI streams for one query."""
    from fake_cli import Voice

    return lambda *turns, **cli: Voice(monkeypatch, list(turns), **cli)
