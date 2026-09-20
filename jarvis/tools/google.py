"""Gmail, Google Calendar, Google Chat, Google Docs, Google Sheets, Google Slides and Google Drive search across the
user's labelled accounts, kept in the database (jarvis.integrations.accounts), from AI Armory's Google tool sets
(ai_armory.toolsets.google).

Importing this sets those tool sets up with JARVIS's accounts, their sign-ins in secrets/google-<label>.json, the
user's time zone and name, and JARVIS's own sign-in command, then registers their tools under their own names
(gmail_search, chat_draft, ...). With no account, none are registered.

Sends and deletes are drafted first and done by a second tool that takes only the draft's id, such as gmail_send or
chat_send (jarvis.safety.draft_gate.DRAFTED_BY lists them). AI Armory marks those as needing the user's go-ahead, so
they're gated: the brain's gate (jarvis.safety.draft_gate) reads the draft aloud and approves it only after the user's
spoken yes. Email is sent only that way; gmail_create_draft saves a draft in Gmail for the user to send themselves.

Add and sign in an account once with: uv run python -m jarvis.integrations.accounts add <label>
"""

from ai_armory import load_many
from ai_armory.toolsets.google import Account, GoogleSettings, configure, current

from jarvis.core import profile
from jarvis.core.config import APPROVAL_MINUTES, GOOGLE_CLIENT_SECRET
from jarvis.integrations import accounts
from jarvis.safety.commands import JARVIS_SECRETS

from . import register_toolsets

SIGN_IN = "uv run python -m jarvis.integrations.accounts login {label}"
IMAGE_FOLDER = "JARVIS doc images"  # the Drive folder holding images from the Mac while a doc fetches them


def settings(found: accounts.Accounts) -> GoogleSettings:
    """The tool sets' settings for these accounts, and the current profile's sign-ins, time zone and name: an
    account's note is what the model is told it's for."""
    mine = profile.current()
    return GoogleSettings(
        accounts=[Account(a.label, a.email, a.note, a.chat) for a in found.accounts],
        default=found.default,
        token_dir=mine.secrets,
        timezone=mine.timezone,
        user_name=mine.name,
        sign_in_command=SIGN_IN,
        client_secret=GOOGLE_CLIENT_SECRET,
        draft_minutes=2 * APPROVAL_MINUTES,  # time to answer the question, then to go ahead once the user says yes
        image_folder=IMAGE_FOLDER,
        blocked_paths=[str(p) for p in JARVIS_SECRETS],  # never uploaded into a doc, like any secret
    )


# Read once: the tools' lists of accounts are fixed for the session, which keeps the prompt cache warm.
ACCOUNTS = accounts.load()
LABELS = ACCOUNTS.labels
CHAT_LABELS = ACCOUNTS.chat_labels  # the accounts whose sign-in also asks for Google Chat


def fresh() -> None:
    """The tool sets' settings again when the profile's name, time zone or sign-ins have changed since, so a
    long-running process (the task runner) follows a change made in the Settings panel."""
    if (now := settings(ACCOUNTS)) != current():
        configure(now)


configure(settings(ACCOUNTS))
if LABELS:
    register_toolsets(load_many(["google"]), before=fresh)
