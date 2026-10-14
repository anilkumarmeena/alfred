"""Background tasks' risky-command requests, put to the user by voice.

- One task at a time, oldest first, all its waiting requests in one question; never while the voice shell has its
  own question open, so one yes can't answer two things.
- The user's very next words answer it, read by consent.words, else a model call that sees only those words and the
  runner's command summary. Unclear hands the turn to the brain. The answer covers every waiting request of that task.
- Asked aloud at most once per call; unanswered requests stay for the app's buttons and the next greeting.
- Yes scope: once (default), session (rest of the task), always (offers a rule via `RuleOffer` first).
- The brain may answer later (task_approve) only on the user's own words this turn (`Heard`), which must plainly say
  so or order just what the commands do. No email, web page or task output can put words there.
"""

import asyncio

from jarvis.core import profile
from jarvis.safety import consent, remembered
from jarvis.safety.commands import ordered, said_breadth, tells_approve, tells_deny
from jarvis.safety.consent import Verdict


class VoiceApprovals:
    def __init__(self) -> None:
        self.waiting: dict[int, dict] = {}  # not yet asked, by id
        self.asked: set[int] = set()  # asked this call; never asked again
        self.open: dict | None = None  # just asked; the user's next words answer it
        # Set when a request may have come due; the voice session waits on it instead of polling
        self.changed = asyncio.Event()

    def add(self, approval: dict) -> None:
        """Queue a pending request."""
        if approval.get("state", "pending") == "pending" and approval["id"] not in self.asked:
            self.waiting.setdefault(approval["id"], approval)
            self.changed.set()

    def drop(self, approval_id: int) -> None:
        """Forget a request answered elsewhere, lapsed or withdrawn."""
        self.waiting.pop(approval_id, None)
        if self.open and self.open["id"] == approval_id:
            self.close()

    def due(self) -> dict | None:
        """Oldest waiting request, if none is open."""
        if self.open:
            return None
        return self.waiting[min(self.waiting)] if self.waiting else None

    def ask(self, approval: dict) -> str:
        """Open a request plus its task's other waiting ones under one question; returns what to say."""
        group = [approval] + [a for i, a in sorted(self.waiting.items())
                              if a["task"] == approval["task"] and i != approval["id"]]
        for a in group:
            self.waiting.pop(a["id"], None)
            self.asked.add(a["id"])
        # `about`: what the consent model is told is waiting
        self.open = approval | {"group": [a["id"] for a in group],
                                "about": " and ".join(a["summary"] for a in group if a.get("summary"))}
        return spoken(group)

    def hear(self, said: str, verdict: Verdict | None = None) -> tuple[dict, str, str] | None:
        """Read the answer: (request, "approve"|"deny", "once"|"session"|"always"), or None if unclear.

        `verdict` is consent.reply's reading; without it the word lists decide. Always closes the open request.
        """
        if not (approval := self.open):
            return None
        self.close()
        verdict = verdict or consent.words(said) or consent.UNCLEAR
        if verdict.yes:
            return approval, "approve", verdict.scope
        if verdict.no:
            return approval, "deny", "once"
        return None

    def close(self) -> None:
        """Drop the open request: something else was asked, so a yes now would be for that."""
        if self.open:
            self.open = None
            self.changed.set()


class Heard:
    """The user's own words this turn: the only thing that lets the brain run task_approve or screen_look."""

    def __init__(self) -> None:
        self.said = ""
        self.spent = True  # nothing heard yet, or already used to answer a request
        self.before = ""  # JARVIS's previous reply, which a bare "yes" answers
        self._reply = ""

    def turn(self, said: str) -> None:
        """Record the words starting a turn, before the brain sees them."""
        self.said, self.spent, self.before = said.strip(), False, self._reply

    def replied(self, reply: str) -> None:
        """Record the brain's reply, which the next turn may answer."""
        self._reply = reply.strip()

    def take(self, decision: str, commands: list[str] = (), verdict: Verdict | None = None) -> str | None:
        """Return the words if they plainly `decision` ("approve"|"deny") a request, else None.

        A yes may also order exactly what every one of `commands` does ("push it"). `verdict` covers words the lists
        can't read. Spends the words, so one utterance answers one task's requests.
        """
        if decision == "approve":
            told = (tells_approve(self.said) or bool(commands) and all(ordered(self.said, c) for c in commands)
                    or bool(verdict and verdict.yes) and not tells_deny(self.said))
        else:
            told = tells_deny(self.said) or bool(verdict and verdict.no)
        if self.spent or not told:
            return None
        self.spent = True
        return self.said



def spoken(group: list[dict]) -> str:
    """Question for one task's waiting requests: the runner's own for one, a combined one for several."""
    if len(group) == 1:
        return group[0]["spoken"]
    first = group[0]
    title = f", {first['title']}," if first.get("title") else ""
    return (f"Your task{title} wants to run {' and '.join(a['summary'] for a in group)}. One yes covers "
            "them all. Okay to go ahead?")


def answered(approval: dict, decision: str, outcome: dict) -> str:
    """Spoken confirmation of an answer, incl. other requests it covered and any session rule kept."""
    if not outcome.get("answered"):
        return "That request was already dealt with."
    if decision != "approve":
        return f"Okay, the task won't run {'them' if outcome.get('also') else 'it'}."
    said = "Okay, the task carries on."
    if n := len(outcome.get("also") or []):
        said += f" That yes covers its {'other request' if n == 1 else f'{n} other requests'} waiting too."
    if kept := outcome.get("remembered"):
        if kept.get("rules"):
            said += f" For the rest of it, {remembered.covers(kept['rules'])} won't need asking."
        else:
            said += f" That yes is just for this once: {kept.get('never', 'it can’t be remembered')}."
    return said


class RuleOffer:
    """A rule offered after an "always" yes; only the user's very next words can save it.

    One at a time; anything else said or asked drops it. `open`: rules, source approval id (None for the voice
    shell's own command), breadth, example.
    """

    def __init__(self) -> None:
        self.open: dict | None = None

    def put(self, rules: list[dict], approval: int | None = None, breadth: str = "exact", example: str = "") -> str:
        """Offer rules; returns the question to ask."""
        self.open = {"rules": rules, "approval": approval, "breadth": breadth, "example": example}
        return f"{remembered.preview(rules)}. Save that rule?"

    def hear(self, said: str, verdict: Verdict | None = None) -> tuple[dict, bool] | None:
        """Read the answer: (offer, save?) for a clear yes/no; else drop it unsaved and return None."""
        offer, self.open = self.open, None
        if offer is None:
            return None
        verdict = verdict or consent.words(said, consent.RULE) or consent.UNCLEAR
        if verdict.yes:
            return offer, True
        if verdict.no:
            return offer, False
        return None

    def close(self) -> None:
        self.open = None



def offer_always(offer: RuleOffer, approval_id: int, commands: list[dict], said: str) -> tuple[str, bool]:
    """After an "always" yes: (save-rule question, True), or (why the yes stays one-off, False)."""
    found = remembered.fitting([c.get("remember") or {} for c in commands], said_breadth(said))
    if isinstance(found, str):
        return f"That yes is just for this once: {found}.", False
    rules, breadth = found
    return offer.put(rules, approval_id, breadth), True


def saved(kept: dict) -> str:
    """Spoken result of saving an offered rule."""
    if not kept.get("rules"):
        return f"I couldn't keep that one: {kept.get('never', 'it can’t be remembered')}."
    return "Saved. You can withdraw it any time, in the control panel's settings or by asking me."


def asked(approval: dict) -> str:
    """Note for the brain alone: which task and request a spoken question (which names neither) is about."""
    return f"That question was task {approval['task']}'s request {approval['id']}."


def note(approval: dict, decision: str) -> str:
    """Context note for the brain: what the user just approved or declined."""
    word = "approved" if decision == "approve" else "declined"
    return f"{profile.name()} {word} task {approval['task']}'s request {approval['id']} to run {approval['summary']}."
