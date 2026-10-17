"""Test setup, run before any test imports jarvis.

Points JARVIS_DATA_DIR at a scratch install and activates PROFILE with fixed Google accounts and "about you", so
results never depend on (or touch) this machine's data/, vault, .env or tokens. Model-backed checks are off.
For an isolated profile, use the `own_profile` fixture or call use_profile().
"""

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
(SCRATCH / "config.toml").write_text("")  # only the shipped defaults, never this machine's config.toml
os.environ["JARVIS_CONFIG"] = str(SCRATCH / "config.toml")
os.environ["JARVIS_GOAL_LOOP"] = "0"  # test_goal enables it with a fake check
os.environ["JARVIS_CONSENT"] = "0"  # test_consent fakes the model call
os.environ["JARVIS_ANNOUNCE"] = "0"  # test_announce fakes the model call
os.environ["JARVIS_INJECTION_CHECK"] = "0"  # test_injection fakes the check
os.environ["JARVIS_TODO_MATCH"] = "0"  # test_todo fakes the model call

# Must follow the env setup above: jarvis.config reads JARVIS_DATA_DIR on import
from jarvis import bootstrap, config  # noqa: E402
from jarvis.auth import credentials, logins, vault
from jarvis.integrations import accounts  # noqa: E402
from jarvis.media import transcribe
from jarvis.safety import host  # noqa: E402
from jarvis.state import conversation, settings  # noqa: E402
from jarvis.storage import profile
from jarvis.tasks import merge_check  # noqa: E402

PROFILE = profile.Profile("test", "Sam", config.mac_timezone() or "UTC", config.DATA_DIR, config.DB_FILE,
                          SCRATCH / "secrets")
profile.activate(PROFILE)
transcribe.LOG_FILE = SCRATCH / "transcribe.log"  # keep errors out of the checkout's logs/
config.CLAUDE_LOGINS_DIR = SCRATCH / "claude-logins"  # never this computer's Claude logins
host.JOB_POLL = 0.05  # jobs' ends are seen at once, not up to 2 s later
bootstrap.wire()  # as every process does at start: task sessions and watches get JARVIS's tools


def use_profile(monkeypatch: pytest.MonkeyPatch, data: Path | None = None, **changes) -> profile.Profile:
    """Switch to a profile rooted at `data` for the rest of the test, or, with no `data`, the current one plus
    `changes`."""
    folders = {"data": data, "db": data / "tasks.db", "secrets": data / "secrets"} if data else {}
    mine = dataclasses.replace(profile.current() if data is None else PROFILE, **folders | changes)
    monkeypatch.setattr(profile, "_process", mine)
    return mine


def use_task_cache(monkeypatch: pytest.MonkeyPatch, folder: Path) -> Path:
    """Point TASK_CACHE_DIR at `folder` in every module that reads it."""
    from jarvis.tasks import briefs, host_tools, worker

    for module in (worker, host_tools, briefs):
        monkeypatch.setattr(module, "TASK_CACHE_DIR", folder)
    return folder


@pytest.fixture(autouse=True)
def google_tools_follow_the_shared_accounts(monkeypatch):
    """A test that switches profile can make the Google tools follow its accounts (google.fresh): put the profile
    back first, then follow the shared accounts again."""
    yield
    from jarvis.tools import google

    monkeypatch.undo()
    google.fresh()


@pytest.fixture(autouse=True)
def fresh_conversation(monkeypatch) -> conversation.Conversation:
    """Fresh conversation per test, so no state leaks between tests."""
    monkeypatch.setattr(conversation, "_process", fresh := conversation.Conversation())
    return fresh


@pytest.fixture
def own_profile(monkeypatch, tmp_path) -> profile.Profile:
    """Isolated profile in tmp_path."""
    return use_profile(monkeypatch, tmp_path)


# No master key, .env or tokens from this machine; GitHub and GitLab start disabled
config.MASTER_KEY = ""
vault.ENV_FILE = SCRATCH / "dotenv"  # not ".env": sandboxes hide it
logins.FILE = SCRATCH / "logins.json"  # absent, so JARVIS_PASSCODE is checked
for _var in ("GITHUB_PERSONAL_ACCESS_TOKEN", "GITLAB_PERSONAL_ACCESS_TOKEN", "JARVIS_GITHUB_MCP", logins.VAR,
             *credentials.VARS):
    os.environ.pop(_var, None)
config.GOOGLE_CLIENT_SECRET = ""
ACCOUNTS = accounts.Accounts([accounts.Account("work", "me@acme.example", "for work", chat=True),
                              accounts.Account("personal", "me@mail.example", "their own")], "work")
ABOUT = "Works at Acme on its video platform.\n- Also runs their own ventures, kept apart from the day job."

with accounts.change() as found:
    found.accounts, found.default = list(ACCOUNTS.accounts), ACCOUNTS.default
settings.set_about(ABOUT)


# What this run did, noted against the code it ran on: merging exactly that code needn't run the suite again
_RUN: dict = {"tree": None, "ran": set(), "failed": set()}


def pytest_collection_finish(session):
    _RUN["tree"] = merge_check.code_tree(session.config.rootpath)


def pytest_runtest_logreport(report):
    if report.failed:
        _RUN["failed"].add(report.nodeid)
    elif report.when == "call" or report.skipped:
        _RUN["ran"].add(report.nodeid)


def pytest_sessionfinish(session):
    root = session.config.rootpath
    try:
        if _RUN["tree"] and merge_check.code_tree(root) == _RUN["tree"]:  # not edited while it ran
            merge_check.record(root, _RUN["tree"], _RUN["ran"] - _RUN["failed"], _RUN["failed"])
    except OSError:
        pass


@pytest.fixture
def voice(monkeypatch):
    """voice(*turns, **cli) -> fake_cli.Voice; each turn is the script the fake CLI streams for one query."""
    from fake_cli import Voice

    return lambda *turns, **cli: Voice(monkeypatch, list(turns), **cli)
