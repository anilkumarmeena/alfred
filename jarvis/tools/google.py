"""Google tools (Gmail, Calendar, Chat, Docs, Sheets, Slides, Drive) from AI Armory, across the user's labelled accounts.

Importing configures the tool sets (accounts, sign-ins in secrets/google-<label>.json, time zone, name) and registers
them; nothing is registered without an account. Sends and deletes are drafted, then done by a gated tool taking only
the draft id, approved by spoken yes (jarvis.safety.draft_gate).

Add an account: uv run python -m jarvis.integrations.accounts add <label>
"""

from ai_armory import load_many
from ai_armory.toolsets.google import Account, GoogleSettings, configure, current

from jarvis.core import profile
from jarvis.core.config import APPROVAL_MINUTES, GOOGLE_CLIENT_SECRET
from jarvis.integrations import accounts
from jarvis.safety.commands import JARVIS_SECRETS

from . import register_toolsets

SIGN_IN = "uv run python -m jarvis.integrations.accounts login {label}"
IMAGE_FOLDER = "JARVIS doc images"  # Drive staging folder for Mac images inserted into docs


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


# Read once: fixed account lists keep the prompt cache warm.
ACCOUNTS = accounts.load()
LABELS = ACCOUNTS.labels
CHAT_LABELS = ACCOUNTS.chat_labels  # accounts signed in with Google Chat scope


def fresh() -> None:
    """Reconfigure if profile settings changed, so long-running processes follow Settings panel edits."""
    if (now := settings(ACCOUNTS)) != current():
        configure(now)


configure(settings(ACCOUNTS))
if LABELS:
    register_toolsets(load_many(["google"]), before=fresh)
