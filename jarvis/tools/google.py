"""Google tools (Gmail, Calendar, Chat, Docs, Sheets, Slides, Drive) from AI Armory, across the user's labelled accounts.

Importing configures the tool sets (accounts, sign-ins in secrets/google-<label>.json, time zone, name) and registers
them; nothing is registered without an account. Sends and deletes are drafted, then done by a gated tool taking only
the draft id, approved by spoken yes (jarvis.safety.draft_gate).

Accounts are added and signed in from the control panel (jarvis.integrations.google_signin) or the accounts command.
`fresh` follows those changes, so no restart is needed.
"""

from collections.abc import Callable

from ai_armory import all_tools, load_many
from ai_armory.toolsets.google import Account, GoogleSettings, configure, current

from jarvis.core import profile
from jarvis.core.config import APPROVAL_MINUTES, GOOGLE_CLIENT_SECRET
from jarvis.integrations import accounts
from jarvis.safety.commands import JARVIS_SECRETS

from . import register_toolsets, unregister

SIGN_IN = "the Sign in button beside the {label} account on the control panel's Google accounts page"
IMAGE_FOLDER = "JARVIS doc images"  # Drive staging folder for local images inserted into docs


def settings(found: accounts.Accounts) -> GoogleSettings:
    """Tool-set settings for these accounts and the current profile; an account's note tells the model its purpose."""
    mine = profile.current()
    return GoogleSettings(
        accounts=[Account(a.label, a.email, a.note, a.chat) for a in found.accounts],
        default=found.default,
        token_dir=mine.secrets,
        timezone=mine.timezone,
        user_name=mine.name,
        sign_in_command=SIGN_IN,
        client_secret=GOOGLE_CLIENT_SECRET,
        draft_minutes=2 * APPROVAL_MINUTES,  # time to answer, then to go ahead after the yes
        image_folder=IMAGE_FOLDER,
        blocked_paths=[str(p) for p in JARVIS_SECRETS],  # never uploaded into a doc
    )


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
