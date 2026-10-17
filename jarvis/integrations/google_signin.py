"""Google account sign-in from the control panel, run by the web service.

The sign-in itself is AI Armory's (ai_armory.toolsets.google.web.WebSignIns): keeping each begun sign-in by its
one-time state, where Google sends it back (a Desktop app client to http://127.0.0.1:WEB_PORT/api/google/callback, a
phone pasting that address back; a Web app client to the panel's own address), saving the token, checking each API
and the callback page. Tokens are written by AI Armory, never shown or logged. What's JARVIS's here: the client JSON
(jarvis.auth.credentials), the accounts (jarvis.integrations.accounts) and their emails, and the activity line.
"""

import logging

from jarvis import config
from jarvis.auth import credentials
from jarvis.integrations import accounts
from jarvis.integrations.armory import Outcome, WebSignIns, checks
from jarvis.state import activity

log = logging.getLogger("jarvis.integrations.google_signin")

CALLBACK = "/api/google/callback"
NO_CLIENT = ("JARVIS has no Google OAuth client yet: create one in Google Cloud and paste its JSON on this page "
             "first.")


def _signed_in(o: Outcome) -> None:
    """Keep the email a sign-in chose and say so in the activity feed."""
    log.info("sign-in for the %s account: %s", o.label, o.state)
    if o.state == "done":
        _keep_email(o)
        activity.add(o.said)


signins = WebSignIns(credentials.google_client, CALLBACK, config.WEB_PORT, on_done=_signed_in, no_client=NO_CLIENT,
                     app="JARVIS", brand="J.A.R.V.I.S.", where="JARVIS's Google accounts page")


def start(label: str, step: int, origin: str) -> dict:
    """Begin a sign-in for a known account (WebSignIns.start), with its email as Google's hint."""
    found = accounts.load()
    account = found.need(label)
    accounts.configure_google(found)
    started = signins.start(label, step, origin, account.email)
    log.info("sign-in started for the %s account (step %d)", label, step)
    return started


def recheck(label: str) -> Outcome:
    """Check the signed-in account's Google APIs again (after turning one on), filling in its email once Gmail works."""
    accounts.configure_google(found := accounts.load())
    found.need(label)
    return _keep_email(checks.recheck(label, signins.client()))


def _keep_email(o: Outcome) -> Outcome:
    """Store the email the sign-in chose, noting what it replaced or a clash."""
    if o.email and (said := accounts.take_email(o.label, o.email)):
        o.notes.append(said)
    return o
