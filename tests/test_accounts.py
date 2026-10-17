"""Tests for jarvis.integrations.accounts: the accounts table, its command, and Google tools' accounts and sign-ins."""

import asyncio
import json
import os
import sqlite3
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from ai_armory.toolsets.google import chat, common

from conftest import use_profile
from jarvis import config, tools
from jarvis.integrations import accounts
from jarvis.integrations.accounts import Account, Accounts
from jarvis.state import settings
from jarvis.storage import profile
from jarvis.tools import google

ROOT = config.ROOT
PANEL = "the Sign in button beside the {} account on the control panel's Google accounts page"


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Isolated database and secrets folder."""
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    use_profile(monkeypatch, tmp_path)  # sign-ins land in tmp_path/secrets
    return SimpleNamespace(dir=tmp_path, secrets=secrets, db=tmp_path / "tasks.db")


def two() -> Accounts:
    return Accounts([Account("work", "me@acme.example", "for work", chat=True),
                     Account("personal", "me@mail.example", "their own")], "work")


def store(found: Accounts) -> None:
    with accounts.change() as kept:
        kept.accounts, kept.default = list(found.accounts), found.default


def run(*argv: str) -> int:
    return accounts.main(list(argv))


def rows(home) -> list[tuple]:
    return sqlite3.connect(home.db).execute("select label, position, is_default from accounts order by position").fetchall()


# ── The table ───────────────────────────────────────────────────

def test_the_table_keeps_everything_in_order_with_one_default(home):
    tricky = Accounts([Account("side-project", "a@b.example", 'Says "hi" \\ née', chat=True), Account("x_2"),
                       Account("a")], "x_2")
    store(tricky)
    assert accounts.load() == tricky
    assert rows(home) == [("side-project", 0, 0), ("x_2", 1, 1), ("a", 2, 0)]
    with pytest.raises(sqlite3.IntegrityError):  # the database itself rejects a second default
        sqlite3.connect(home.db).execute("update accounts set is_default = 1 where label = 'a'")


def test_a_database_that_cant_be_opened_means_no_accounts_rather_than_a_crash(home, monkeypatch, caplog):
    use_profile(monkeypatch, home.dir, db=home.dir)  # a folder, not a database
    assert accounts.load() == Accounts()
    assert "can't read the Google accounts" in caplog.text


def test_changes_from_several_processes_at_once_all_land(home):
    store(Accounts([Account("base")], "base"))

    def add(n: int) -> None:
        with accounts.change() as found:
            found.add(Account(f"a{n}"))

    threads = [threading.Thread(target=add, args=(n,)) for n in range(8)]  # a connection each, like processes
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(accounts.load().labels) == ["a0", "a1", "a2", "a3", "a4", "a5", "a6", "a7", "base"]


def test_a_change_that_fails_changes_nothing(home):
    store(two())
    with pytest.raises(ValueError):
        with accounts.change() as found:
            found.remove("personal")
            found.add(Account("work"))  # taken
    assert accounts.load() == two()


# ── A fresh install ─────────────────────────────────────────────

def test_two_processes_starting_at_once_make_the_table_once(home):
    errors = []

    def start() -> None:
        try:
            accounts.load()
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=start) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == [] and rows(home) == []


def test_a_fresh_install_has_no_accounts_and_stays_that_way(home):
    (home.secrets / "google-work.json").write_text("{}")  # a sign-in alone adds no account; only the command does
    assert accounts.load() == Accounts()
    assert accounts.show(Accounts()).startswith("No Google accounts yet. Add one in the control panel")


def test_a_fresh_install_starts_without_google_tools_or_a_crash(home):
    """Subprocess: tool account lists are fixed at import. Its empty DB keeps out this machine's data/."""
    assert accounts.load() == Accounts()
    settings.about()  # also creates the settings table
    env = os.environ | {"JARVIS_DB_FILE": str(home.db)}
    code = ("from ai_armory.toolsets.google import current\n"
            "from jarvis import tools\n"
            "from jarvis.brain import persona\n"
            "names = {t.name for t in tools.all_tools()}\n"
            "print(sorted(n for n in names if n.split('_')[0] in ('gmail', 'calendar', 'chat', 'docs', 'sheets')))\n"
            "print(repr(current().default_label), 'task_start' in names)\n"
            "print('No Google account is set up yet' in persona.persona())\n")
    done = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines() == ["[]", "'' True", "True"]


def test_one_account_alone_gets_every_tool_and_everything_by_default(home):
    """Subprocess, as above: no other label offered, writes go to it, the prompt names only it."""
    only = Accounts([Account("work", "me@acme.example", "for work", chat=True)], "work")
    store(only)
    settings.about()
    env = os.environ | {"JARVIS_DB_FILE": str(home.db)}
    code = ("from jarvis import tools\n"
            "from jarvis.tools import google\n"
            "found = [t for t in tools.all_tools() if t.armory and 'account' in t.properties]\n"
            "print(len(found) >= 25, sorted({tuple(t.properties['account']['enum']) for t in found}))\n"
            "print(sorted({t.properties['account'].get('default') for t in found}), google.CHAT_LABELS)\n")
    done = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines() == ["True [('work',), ('work', 'all')]", "['all', 'work'] ['work']"]
    assert accounts.describe(only) == (
        'Their Google accounts, by the label the tools take as account: "work" (me@acme.example): for work.')


def test_the_command_runs_as_a_module(home):
    store(two())
    done = subprocess.run([sys.executable, "-m", "jarvis.integrations.accounts"], cwd=ROOT, capture_output=True, text=True,
                          timeout=60, env=os.environ | {"JARVIS_DB_FILE": str(home.db)})
    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines()[0].split() == ["work", "me@acme.example", "[default,", "Google", "Chat,", "not",
                                                    "signed", "in]", "for", "work"]


# ── Names, and what the prompt says ─────────────────────────────

def test_an_account_can_be_named_by_its_email_or_in_other_capitals():
    found = two()
    assert found.resolve("Personal") == "personal" and found.resolve(" ME@ACME.example ") == "work"
    assert found.resolve("nobody") == "nobody"  # left for the tool to refuse


def test_the_prompt_describes_the_accounts():
    assert accounts.describe(two()) == (
        'Their Google accounts, by the label the tools take as account: "work" (me@acme.example): for work; '
        '"personal" (me@mail.example): their own. Whatever is made or changed without naming an account goes to '
        '"work"; searches cover every account unless the user names one.')
    assert accounts.describe(Accounts([Account("home")], "home")) == (
        'Their Google accounts, by the label the tools take as account: "home".')
    assert accounts.describe(Accounts()).startswith("No Google account is set up yet")


# ── Adding, removing, renaming ──────────────────────────────────

def test_adding_checks_the_label_and_email_and_the_first_is_the_default():
    found = Accounts()
    found.add(Account("home", "a@b.example"))
    assert found.default == "home"
    found.add(Account("job"), default=True)
    assert found.default == "job"
    for bad, why in ((Account("home"), "already a home account"), (Account("Job 2"), "can't be a label"),
                     (Account("all"), "'all' can't be a label"), (Account("other", "A@B.example"), "already the home")):
        with pytest.raises(ValueError, match=why):
            found.add(bad)
    assert found.labels == ["home", "job"]


def test_the_command_adds_lists_changes_and_removes(home, capsys, monkeypatch):
    assert run("add", "home", "--email", "me@mail.example", "--note", "their own") == 0
    assert run("add", "job", "--chat", "--default") == 0
    (home.secrets / "google-job.json").write_text("{}")
    capsys.readouterr()
    run("list")
    assert capsys.readouterr().out.splitlines() == [
        "  home  me@mail.example  [not signed in]  their own",
        "  job   (email unknown)  [default, Google Chat, signed in]"]

    assert run("set", "job", "--email", "me@acme.example", "--note", "for work", "--no-chat") == 0
    assert run("default", "home") == 0
    assert accounts.load() == Accounts([Account("home", "me@mail.example", "their own"),
                                        Account("job", "me@acme.example", "for work")], "home")
    assert "JARVIS uses the change from its next conversation." in capsys.readouterr().out

    monkeypatch.setattr("builtins.input", lambda question: "n")
    assert run("remove", "job") == 1 and (home.secrets / "google-job.json").exists()
    monkeypatch.setattr("builtins.input", lambda question: "yes")
    assert run("remove", "home") == 0
    assert run("remove", "job", "--yes") == 0
    assert not (home.secrets / "google-job.json").exists() and accounts.load() == Accounts()
    for argv in (("remove", "nobody", "--yes"), ("set", "nobody", "--note", "x"), ("set", "nobody"),
                 ("add", "Bad Label")):
        with pytest.raises(SystemExit):
            run(*argv)
    assert rows(home) == []


def test_removing_the_default_makes_the_next_one_the_default():
    found = two()
    found.remove("work")
    assert found.default == "personal"
    found.remove("personal")
    assert found.default == ""


def test_rename_moves_the_sign_in_and_the_chat_state(home, capsys):
    store(two())
    (home.secrets / "google-work.json").write_text('{"token": "w"}')
    (home.secrets / "google-work-chat.json").write_text('{"status": "ok"}')

    assert run("rename", "work", "job") == 0
    assert (home.secrets / "google-job.json").read_text() == '{"token": "w"}'
    assert (home.secrets / "google-job-chat.json").exists() and not (home.secrets / "google-work.json").exists()
    found = accounts.load()
    assert found.labels == ["job", "personal"] and found.default == "job" and found.get("job").chat
    assert "moving google-job.json, google-job-chat.json." in capsys.readouterr().out


def test_rename_changes_nothing_if_the_new_label_has_files_already(home):
    store(two())
    (home.secrets / "google-work.json").write_text("w")
    (home.secrets / "google-job.json").write_text("an old one")
    with pytest.raises(SystemExit, match="google-job.json is already there"):
        run("rename", "work", "job")
    assert (home.secrets / "google-work.json").read_text() == "w" and accounts.load() == two()
    for new in ("personal", "Job"):
        with pytest.raises(SystemExit):
            run("rename", "work", new)


def test_a_sign_in_keeps_the_email_it_chose_and_says_what_it_replaced(home):
    store(Accounts([Account("home"), Account("job", "me@acme.example"), Account("other", "you@mail.example")], "home"))
    assert accounts.take_email("home", "me@mail.example") == ""  # filled in: nothing to say
    assert accounts.take_email("job", "ME@acme.example") == ""  # the same address
    assert accounts.take_email("job", "new@acme.example") == "The job account is new@acme.example now, not me@acme.example."
    assert "you@mail.example is already the other account" in accounts.take_email("home", "you@mail.example")
    assert [a.email for a in accounts.load().accounts] == ["me@mail.example", "new@acme.example", "you@mail.example"]


# ── The tools ───────────────────────────────────────────────────

def test_every_google_tool_takes_any_account_and_none_needs_one():
    found = [t for t in tools.all_tools() if t.armory and "account" in t.properties]
    assert len(found) >= 25
    for t in found:
        spec = t.properties["account"]
        assert spec["enum"] in (google.LABELS, [*google.LABELS, "all"]) and google.LABELS == ["work", "personal"]
        assert spec["default"] == ("all" if "all" in spec["enum"] else "work") and "account" not in t.required, t.name
    image = next(t for t in found if t.name == "docs_insert_image").properties["upload_account"]
    assert image["enum"] == google.LABELS


class Chain:
    """Fake Google API client: returns itself for every call and records its account."""

    def __init__(self, label: str, built: list) -> None:
        built.append(label)

    def __getattr__(self, name):
        return lambda *args, **kwargs: self


def test_a_write_without_an_account_goes_to_the_default_and_any_named_one_is_used(monkeypatch):
    built = []
    monkeypatch.setattr(common, "service", lambda label, api, version, needs=None: Chain(label, built))
    draft ={"to": "a@b.example", "subject": "Hi", "body": "Hello"}
    for account, label in ((None, "work"), ("personal", "personal"), ("Personal", "personal"),
                           ("me@mail.example", "personal"), ("ME@ACME.EXAMPLE", "work")):
        content, is_error = asyncio.run(tools.run("gmail_create_draft", draft | ({"account": account} if account else {})))
        assert not is_error and content == f"Draft to a@b.example saved in the {label} account's Drafts.", account
    assert built == ["work", "personal", "personal", "personal", "work"]
    content, is_error = asyncio.run(tools.run("gmail_create_draft", draft | {"account": "nobody"}))
    assert is_error and "field 'account' must be one of ['work', 'personal']" in content and len(built) == 5


class Calendar:
    def __init__(self, items: list[dict]) -> None:
        self.items = items

    def events(self):
        return self

    def list(self, **kwargs):
        return self

    def execute(self):
        return {"items": self.items}


def event(title: str, start: str) -> dict:
    return {"id": "e1", "summary": title, "start": {"dateTime": start}, "end": {"dateTime": start}}


def test_all_covers_every_account_and_a_broken_one_is_noted(monkeypatch):
    def service(label, api, version, needs=None):
        if label == "personal":
            raise RuntimeError(common.not_connected(label))
        return Calendar([event(f"{label} sync", "2026-10-04T14:00:00+05:30")])

    monkeypatch.setattr(common, "service", service)
    assert asyncio.run(tools.run("calendar_events", {})) == (json.dumps({
        "events": [{"account": "work", "id": "e1", "title": "work sync", "start": "2026-10-04T14:00:00+05:30",
                    "end": "2026-10-04T14:00:00+05:30", "location": None, "meet": None, "attendees": 0}],
        "notes": ["Left out the personal account: The personal Google account is not signed in yet. Sign it in "
                  "with: " + PANEL.format("personal")]}), False)
    content, is_error = asyncio.run(tools.run("calendar_events", {"account": "me@mail.example"}))
    assert is_error and content.endswith("Sign it in with: " + PANEL.format("personal"))


def test_the_tools_use_jarvis_sign_ins_and_send_you_to_the_panel_to_sign_in_again():
    secrets = profile.current().secrets
    assert common.token_path("work") == secrets / "google-work.json"
    assert chat.state_path("work") == secrets / "google-work-chat.json"  # where the accounts command keeps it
    assert common.sign_in_again("work", common.GMAIL_MODIFY) == (
        "The work Google account's sign-in doesn't let these tools change mail. Sign it in again with: "
        + PANEL.format("work"))


def test_watches_read_only_signed_in_accounts(monkeypatch):
    monkeypatch.setattr(common, "token_path", lambda label: Path("/") if label == "personal" else Path("/nowhere"))
    assert common.connected() == ["personal"] and common.accounts_for("all") == ["work", "personal"]
