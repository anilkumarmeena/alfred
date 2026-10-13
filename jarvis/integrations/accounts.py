"""Google accounts JARVIS uses, stored in the accounts table of data/tasks.db.

Each has a label, email, optional note, a Google Chat flag, and one is the default for anything made without naming an
account. Accounts are signed in from the control panel (jarvis.integrations.google_signin). Running services pick up a
change at their next conversation turn, task or watch check (jarvis.tools.google.fresh), so no restart is needed.

    uv run python -m jarvis.integrations.accounts                  list
    uv run python -m jarvis.integrations.accounts add LABEL [--email E] [--note N] [--chat] [--default]
    uv run python -m jarvis.integrations.accounts rename OLD NEW
    uv run python -m jarvis.integrations.accounts remove LABEL [--yes]
    uv run python -m jarvis.integrations.accounts default LABEL
    uv run python -m jarvis.integrations.accounts set LABEL [--email E] [--note N] [--chat | --no-chat]
    uv run python -m jarvis.integrations.accounts check-chat [LABEL]

Changes run in one write-locked transaction (`change`) so concurrent processes can't undo each other. Sign-ins live in
secrets/google-<label>.json and secrets/google-<label>-chat.json; renaming moves both.
"""

import argparse
import logging
import re
import sqlite3
import sys
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path

from jarvis.core import database, profile

log = logging.getLogger("jarvis.integrations.accounts")

LABEL = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")  # used in file names

_SCHEMA = """
create table accounts (
    label text primary key,
    position integer not null,           -- list order
    email text not null default '',
    note text not null default '',       -- what it's for; passed to the prompt
    chat integer not null default 0,     -- 1: Workspace account; sign-in also requests Google Chat
    is_default integer not null default 0
)"""
_ONE_DEFAULT = "create unique index accounts_one_default on accounts(is_default) where is_default = 1"


def token_path(label: str) -> Path:
    return profile.current().secrets / f"google-{label}.json"


def chat_state_path(label: str) -> Path:
    return profile.current().secrets / f"google-{label}-chat.json"


@dataclass(frozen=True)
class Account:
    label: str
    email: str = ""
    note: str = ""
    chat: bool = False


@dataclass
class Accounts:
    """Ordered accounts and the default's label ("" if none)."""
    accounts: list[Account] = field(default_factory=list)
    default: str = ""

    @property
    def labels(self) -> list[str]:
        return [a.label for a in self.accounts]

    @property
    def chat_labels(self) -> list[str]:
        return [a.label for a in self.accounts if a.chat]

    def get(self, label: str) -> Account | None:
        return next((a for a in self.accounts if a.label == label), None)

    def resolve(self, name: str) -> str:
        """Label for `name` (case-insensitive label or email); unknown names are returned unchanged."""
        key = name.strip().lower()
        return next((a.label for a in self.accounts if key in (a.label, a.email.lower())), name)

    def need(self, label: str) -> Account:
        if not (account := self.get(label)):
            raise ValueError(f"There's no {label} account. The accounts: {', '.join(self.labels) or 'none yet'}.")
        return account

    def add(self, account: Account, default: bool = False) -> None:
        if problem := label_problem(account.label, self.labels):
            raise ValueError(problem)
        self._email_free(account.email, account.label)
        self.accounts.append(account)
        if default or not self.default:
            self.default = account.label

    def remove(self, label: str) -> None:
        self.accounts.remove(self.need(label))
        if self.default == label:
            self.default = self.labels[0] if self.accounts else ""

    def rename(self, old: str, new: str) -> None:
        account = self.need(old)
        if problem := label_problem(new, [l for l in self.labels if l != old]):
            raise ValueError(problem)
        self.accounts[self.accounts.index(account)] = replace(account, label=new)
        if self.default == old:
            self.default = new

    def update(self, label: str, **changes) -> None:
        account = self.need(label)
        if changes.get("email"):
            self._email_free(changes["email"], label)
        self.accounts[self.accounts.index(account)] = replace(account, **changes)

    def set_default(self, label: str) -> None:
        self.default = self.need(label).label

    def _email_free(self, email: str, label: str) -> None:
        if email and (other := next((a for a in self.accounts
                                     if a.label != label and a.email.lower() == email.lower()), None)):
            raise ValueError(f"{email} is already the {other.label} account.")


def label_problem(label: str, taken: list[str]) -> str:
    """Reason `label` is invalid or taken, or ""."""
    if not LABEL.fullmatch(label):
        return (f"'{label}' can't be a label: use up to 32 lowercase letters, digits, dashes and underscores, "
                "starting with a letter or digit.")
    if label == "all":
        return "'all' can't be a label: searches use it for every account."
    if label in taken:
        return f"There's already a {label} account."
    return ""


# ── Storage ─────────────────────────────────────────────────────

def _db() -> AbstractContextManager[sqlite3.Connection]:
    return database.table("accounts", _SCHEMA, _ONE_DEFAULT)


def _read(db: sqlite3.Connection) -> Accounts:
    rows = db.execute("select * from accounts order by position").fetchall()
    found = Accounts([Account(r["label"], r["email"], r["note"], bool(r["chat"])) for r in rows])
    found.default = next((r["label"] for r in rows if r["is_default"]), found.labels[0] if rows else "")
    return found


def _write(db: sqlite3.Connection, accounts: Accounts) -> None:
    db.execute("delete from accounts")
    db.executemany("insert into accounts (label, position, email, note, chat, is_default) values (?, ?, ?, ?, ?, ?)",
                   [(a.label, n, a.email, a.note, int(a.chat), int(a.label == accounts.default))
                    for n, a in enumerate(accounts.accounts)])


def load() -> Accounts:
    """All accounts. An unreadable database is logged and yields none so JARVIS still starts."""
    try:
        with _db() as db:
            return _read(db)
    except sqlite3.Error as e:
        log.error("can't read the Google accounts from %s, so none is set up: %s", profile.current().db.name, e)
        return Accounts()


@contextmanager
def change() -> Iterator[Accounts]:
    """Yield the accounts to edit in place; written back if the block succeeds. Holds the DB write lock, so keep the
    block short (no sign-ins or network calls)."""
    with _db() as db:
        db.execute("begin immediate")
        found = _read(db)
        yield found
        _write(db, found)
        db.execute("commit")


def describe(accounts: Accounts) -> str:
    """Prompt sentence describing the Google accounts."""
    if not accounts.accounts:
        return "No Google account is set up yet, so there's no Gmail, Calendar, Chat, Docs or Sheets."
    each = [f'"{a.label}"' + (f" ({a.email})" if a.email else "") + (f": {a.note.rstrip('.')}" if a.note else "")
            for a in accounts.accounts]
    text = f"Their Google accounts, by the label the tools take as account: {'; '.join(each)}."
    if len(accounts.accounts) > 1:
        text += (f' Whatever is made or changed without naming an account goes to "{accounts.default}"; '
                 "searches cover every account unless the user names one.")
    return text


# ── Account files ───────────────────────────────────────────────

def _files(label: str) -> list[Path]:
    return [token_path(label), chat_state_path(label)]


def move_files(old: str, new: str) -> list[str]:
    """Move sign-in and Chat state from `old` to `new`; returns what moved. Raises ValueError (and changes nothing) if
    `new` files already exist."""
    if clash := [n.name for n in _files(new) if n.exists()]:
        raise ValueError(f"{', '.join(clash)} is already there, from an earlier {new} account. Remove it first.")
    moved = []
    for src, dst in zip(_files(old), _files(new)):
        if src.exists():
            src.rename(dst)
            moved.append(dst.name)
    return moved


def remove_files(label: str) -> list[str]:
    removed = []
    for path in _files(label):
        if path.exists():
            path.unlink()
            removed.append(path.name)
    return removed


# ── CLI ─────────────────────────────────────────────────────────

def show(accounts: Accounts) -> str:
    if not accounts.accounts:
        return ("No Google accounts yet. Add one in the control panel (JARVIS → Google accounts), or with: "
                "uv run python -m jarvis.integrations.accounts add LABEL")
    width = max(len(l) for l in accounts.labels)
    lines = []
    for a in accounts.accounts:
        flags = ["default"] if a.label == accounts.default else []
        flags += ["Google Chat"] if a.chat else []
        flags.append("signed in" if token_path(a.label).exists() else "not signed in")
        note = f"  {a.note}" if a.note else ""
        lines.append(f"  {a.label:<{width}}  {a.email or '(email unknown)'}  [{', '.join(flags)}]{note}")
    return "\n".join(lines)


def configure_google(found: Accounts) -> None:
    """Configure AI Armory's Google toolset with these accounts, as jarvis.tools.google does."""
    from ai_armory.toolsets.google import configure

    from jarvis.tools import google

    configure(google.settings(found))


def _check_chat(label: str) -> int:
    from ai_armory.toolsets.google import auth

    found = load()
    if label not in found.chat_labels:
        sys.exit(f"Google Chat isn't set up for the {label} account. Turn it on with: "
                 f"uv run python -m jarvis.integrations.accounts set {label} --chat")
    configure_google(found)
    return auth.check_chat(label)


def take_email(label: str, email: str) -> str:
    """Store the email a sign-in chose; it replaces a different stored one. Returns a note when there's something to
    say (what it replaced, or that another account has it), else "". Writes only when it changes."""
    if load().need(label).email.lower() == email.lower():
        return ""
    with change() as accounts:
        known = accounts.need(label).email
        try:
            accounts.update(label, email=email)
        except ValueError as e:
            return f"Signed in as {email}, but {e} Remove one of them."
    return f"The {label} account is {email} now, not {known}." if known else ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m jarvis.integrations.accounts", description="JARVIS's Google accounts.")
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("list", help="list the accounts (the default)")
    add = commands.add_parser("add", help="add an account (sign it in from the control panel)")
    add.add_argument("label")
    add.add_argument("--email", default="")
    add.add_argument("--note", default="", help="what it's for, e.g. 'for work'")
    add.add_argument("--chat", action="store_true", help="a Workspace account with Google Chat")
    add.add_argument("--default", action="store_true", help="make it the default")
    rename = commands.add_parser("rename", help="rename an account, with its sign-in and history")
    rename.add_argument("old")
    rename.add_argument("new")
    remove = commands.add_parser("remove", help="remove an account and its sign-in")
    remove.add_argument("label")
    remove.add_argument("--yes", action="store_true", help="don't ask first")
    commands.add_parser("default", help="make an account the default").add_argument("label")
    change_ = commands.add_parser("set", help="change an account's email, note or Chat")
    change_.add_argument("label")
    change_.add_argument("--email")
    change_.add_argument("--note")
    change_.add_argument("--chat", action=argparse.BooleanOptionalAction)
    commands.add_parser("check-chat", help="check Google Chat works").add_argument("label", nargs="?")
    args = parser.parse_args(argv)

    command = args.command or "list"
    if command == "list":
        print(show(load()))
        return 0
    if command == "check-chat":
        if not (label := args.label or next(iter(load().chat_labels), "")):
            sys.exit("No account has Google Chat. Mark one with: uv run python -m jarvis.integrations.accounts set LABEL --chat")
        return _check_chat(label)
    try:
        if command == "add":
            with change() as accounts:
                accounts.add(Account(args.label, args.email.strip(), args.note.strip(), args.chat),
                             default=args.default)
            print(f"Added the {args.label} account. Sign it in with the Sign in button beside it on the control "
                  "panel's Google accounts page.")
        elif command == "rename":
            with change() as accounts:
                accounts.rename(args.old, args.new)
                moved = move_files(args.old, args.new)  # inside the transaction: a failed move rolls back the rename
            print(f"Renamed the {args.old} account to {args.new}" + (f", moving {', '.join(moved)}." if moved else "."))
        elif command == "remove":
            load().need(args.label)
            if not args.yes and not input(f"Remove the {args.label} account and delete its sign-in? [y/N] "
                                          ).strip().lower().startswith("y"):
                print("Nothing changed.")
                return 1
            with change() as accounts:
                accounts.remove(args.label)
                removed = remove_files(args.label)
            print(f"Removed the {args.label} account" + (f" and {', '.join(removed)}." if removed else ".")
                  + (f" The default is now {accounts.default}." if accounts.default else ""))
        elif command == "default":
            with change() as accounts:
                accounts.set_default(args.label)
            print(f"The {args.label} account is the default now.")
        elif command == "set":
            changes = {k: v.strip() if isinstance(v, str) else v
                       for k, v in (("email", args.email), ("note", args.note), ("chat", args.chat)) if v is not None}
            if not changes:
                sys.exit("Say what to change: --email, --note, --chat or --no-chat.")
            with change() as accounts:
                accounts.update(args.label, **changes)
            print(f"Changed the {args.label} account.")
    except ValueError as e:
        sys.exit(str(e))
    print("JARVIS uses the change from its next conversation.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
