"""Google tools (Gmail, Calendar, Chat, Docs, Sheets, Slides, Drive) from AI Armory, across the user's labelled accounts.

Importing configures the tool sets (accounts, sign-ins in secrets/google-<label>.json, time zone, name) and registers
them; nothing is registered without an account. Sends and deletes are drafted, then done by a gated tool taking only
the draft id, approved by spoken yes (jarvis.gates.draft_gate).

Accounts are added and signed in from the control panel (jarvis.integrations.google_signin) or the accounts command.
`fresh` follows those changes, so no restart is needed.
"""

from collections.abc import Callable

from jarvis.integrations import accounts
from jarvis.integrations.accounts import google_settings as settings  # noqa: F401  (tests read it here)
from jarvis.integrations.armory import all_tools, configure, current, load_many

from . import register_toolsets, unregister

# A conversation's tool list is fixed when it starts (prompt cache); fresh() takes changes for the next one, and the
# voice brain restarts its CLI on a change (jarvis.brain.cli.CliBrain.start).
ACCOUNTS = accounts.load()
LABELS = ACCOUNTS.labels
CHAT_LABELS = ACCOUNTS.chat_labels  # accounts signed in with Google Chat scope
_registered: set[str] = set()  # tool names from AI Armory's Google tool sets
_followers: list[Callable[[], None]] = []  # rerun when the accounts change (jarvis.tools.files' Gmail attachments)


def on_change(fn: Callable[[], None]) -> Callable[[], None]:
    """Run `fn` now and again whenever the accounts change: tools whose schemas list the accounts register there."""
    _followers.append(fn)
    fn()
    return fn


def _register() -> None:
    configure(settings(ACCOUNTS))
    unregister(_registered)
    _registered.clear()
    if LABELS:
        toolsets = load_many(["google"])  # built now, so their account fields list the current accounts
        register_toolsets(toolsets, before=fresh)
        _registered.update(t.name for t in all_tools(toolsets))
    for fn in _followers:
        fn()


def fresh() -> None:
    """Follow changes made elsewhere: reread the accounts (registering the tools again when they changed: added,
    removed, renamed, a new note or email), and reconfigure when profile settings changed. Runs as each session, voice
    turn, task and watch check starts (jarvis.brain.sdk.jarvis_server, jarvis.brain.cli, jarvis.tasks.scheduler) and
    before each Google tool call."""
    global ACCOUNTS, LABELS, CHAT_LABELS
    if (found := accounts.load()) != ACCOUNTS:
        ACCOUNTS, LABELS, CHAT_LABELS = found, found.labels, found.chat_labels
        _register()
    elif (now := settings(ACCOUNTS)) != current():
        configure(now)


_register()
