"""Background tasks' requests to run risky commands, put to the user by voice (jarvis.voice.session).

The runner announces each request as an "approval" event, with the question to ask; the voice
agent asks it at the next pause, and the greeting asks about the oldest one still pending. One
task is asked about at a time, oldest first, with all its requests waiting in one question, and never while the voice
shell has a question of its own open, so one yes can't answer two things. The user's very next words answer it,
checked with the word lists (consent.words) before the brain sees them, and that answer goes for every request of that
task still waiting (the runner's whole_task): the user isn't asked about the same task twice. A reply the word lists
can't read goes to a short model call that sees only those words and the runner's summary of the commands waiting
(jarvis.safety.consent); unclear, or anything else,
hands the turn to the brain. Each request is asked aloud at most once per call: unanswered, it stays for the app's
Approve and Deny buttons and in task_list, and the next call's greeting may ask it once more.
One the runner withdraws (the task moved on) is dropped, never asked. A yes counts only for the
requests of the task put to the user in this session.

A yes can say how far it goes (commands.yes_scope): "just this once" (the default), "for this session" (the rest of
that task), or "always" (jarvis.safety.remembered). A session yes is remembered with the answer. An "always" first
says what the rule would cover and asks to save it (`RuleOffer`): only the user's very next words, a clear yes, keep it.
The voice shell offers its own commands' rules the same way. Commands of a kind that always asks are approved once,
and JARVIS says why that yes went no further.

Later in the conversation the user can also tell the brain to answer one ("approve it", "let it
close the hidden Chrome"), which it passes on with task_approve (jarvis.tools.tasks). That works
only on the user's own words in the current turn (`Heard`), which the brain records before it
starts, and which must plainly say so themselves (commands.tells_approve, tells_deny), or order just what the
request's commands do ("push it", "restart now": commands.ordered); words the lists can't read go to the same model
call, which sees only them. No email, web page or task output can put words there, so none can talk the brain into
answering. Each utterance answers one task's requests at most.
"""

import asyncio

from jarvis.core import profile
from jarvis.safety import consent, remembered
from jarvis.safety.commands import ordered, said_breadth, tells_approve, tells_deny
from jarvis.safety.consent import Verdict


class VoiceApprovals:
    def __init__(self) -> None:
        self.waiting: dict[int, dict] = {}  # requests not yet put to the user, by id, each with its `spoken` question
        self.asked: set[int] = set()  # those put to the user in this call, which aren't asked again
        self.open: dict | None = None  # the request just put to the user, which their next words answer
        # Set when one may have come due: a request added, or the one open answered, closed or dropped. The voice
        # session waits on it to ask, rather than looking every few seconds (jarvis.voice.session)
        self.changed = asyncio.Event()

    def add(self, approval: dict) -> None:
        """A pending request, from an event or the runner's list."""
        if approval.get("state", "pending") == "pending" and approval["id"] not in self.asked:
            self.waiting.setdefault(approval["id"], approval)
            self.changed.set()

    def drop(self, approval_id: int) -> None:
        """A request answered some other way (a tap, the command line), lapsed or withdrawn: it isn't asked, or
        answered, here."""
        self.waiting.pop(approval_id, None)
        if self.open and self.open["id"] == approval_id:
            self.close()

    def due(self) -> dict | None:
        """The oldest request to ask about now, if none is open."""
        if self.open:
            return None
        return self.waiting[min(self.waiting)] if self.waiting else None

    def ask(self, approval: dict) -> str:
        """Put a request to the user, once in this call, with the others its task has waiting to be asked: one answer
        goes for them all. Returns what to say."""
        group = [approval] + [a for i, a in sorted(self.waiting.items())
                              if a["task"] == approval["task"] and i != approval["id"]]
        for a in group:
            self.waiting.pop(a["id"], None)
            self.asked.add(a["id"])
        # `about`: what the yes-or-no check's model is told is waiting, made from the commands by the runner
        self.open = approval | {"group": [a["id"] for a in group],
                                "about": " and ".join(a["summary"] for a in group if a.get("summary"))}
        return spoken(group)

    def hear(self, said: str, verdict: Verdict | None = None) -> tuple[dict, str, str] | None:
        """The user's next words after a question: (the request, "approve" or "deny", how far a yes goes: "once",
        "session" or "always") for a clear yes or no. Anything else returns None, and leaves it to the app's buttons
        or a later "approve it". `verdict` is what consent.reply made of the words; without it, the word lists
        decide."""
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
        """Something else was asked after this one (the brain's own question, an announcement): a yes now would
        be for that, so the request is left to the app's buttons or a later "approve it"."""
        if self.open:
            self.open = None
            self.changed.set()


class Heard:
    """The user's own words this turn, which alone let the brain answer a request (task_approve) or look at the
    screen (screen_look)."""

    def __init__(self) -> None:
        self.said = ""
        self.spent = True  # nothing heard yet, or it has answered a request already
        self.before = ""  # JARVIS's reply before these words, which a bare "yes" answers (jarvis.tools.screen)
        self._reply = ""

    def turn(self, said: str) -> None:
        """The user's words that start a turn, before the brain sees them."""
        self.said, self.spent, self.before = said.strip(), False, self._reply

    def replied(self, reply: str) -> None:
        """What the brain said back, which the next turn's words may answer."""
        self._reply = reply.strip()

    def take(self, decision: str, commands: list[str] = (), verdict: Verdict | None = None) -> str | None:
        """The words, if they plainly tell JARVIS to `decision` ("approve" or "deny") a waiting command, or for a yes
        order just what every one of `commands` does ("push it"), and haven't answered a request already; else None.
        `verdict` is what consent.reply made of them, for words the lists can't read. Taking them spends them, so one
        utterance answers one task's requests."""
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
    """The question for one task's requests waiting: the runner's own for one, all of them in one for several."""
    if len(group) == 1:
        return group[0]["spoken"]
    first = group[0]
    title = f", {first['title']}," if first.get("title") else ""
    return (f"Task {first['task']}{title} wants to run {' and '.join(a['summary'] for a in group)}. One yes covers "
            "them all. Okay to go ahead?")


def answered(approval: dict, decision: str, outcome: dict) -> str:
    """What JARVIS says once a spoken answer has reached the runner, with what a "for this session" remembered, and
    the task's other requests the same answer went for."""
    task = f"task {approval['task']}"
    if not outcome.get("answered"):
        return f"That request from {task} was already dealt with."
    if decision != "approve":
        return f"Okay, {task} won't run {'them' if outcome.get('also') else 'it'}."
    said = f"Okay, {task} carries on."
    if n := len(outcome.get("also") or []):
        said += f" That yes covers its {'other request' if n == 1 else f'{n} other requests'} waiting too."
    if kept := outcome.get("remembered"):
        if kept.get("rules"):
            said += f" For the rest of it, {remembered.covers(kept['rules'])} won't need asking."
        else:
            said += f" That yes is just for this once: {kept.get('never', 'it can’t be remembered')}."
    return said


class RuleOffer:
    """A rule put to the user after they answered "always": what it would cover has just been said aloud, and only
    their very next words save it. One at a time; anything else said or asked meanwhile drops it. `open` holds the
    rules (remembered.candidates), the request they came from (or None, for the voice shell's own command) and its
    breadth."""

    def __init__(self) -> None:
        self.open: dict | None = None

    def put(self, rules: list[dict], approval: int | None = None, breadth: str = "exact", example: str = "") -> str:
        """Offer these rules; returns the question to ask."""
        self.open = {"rules": rules, "approval": approval, "breadth": breadth, "example": example}
        return f"{remembered.preview(rules)}. Save that rule?"

    def hear(self, said: str, verdict: Verdict | None = None) -> tuple[dict, bool] | None:
        """The user's next words after the question: (the offer, whether to save it) for a clear yes or no. Anything
        else drops it, unsaved, and returns None. `verdict` is what consent.reply made of the words; without it, the
        word lists decide."""
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
    """What JARVIS adds after a yes that said "always" to a request: the question whether to save the rule it would
    make (put to the conversation's `offer`), whose answer JARVIS hears itself, and True; or why that yes went no
    further, and False."""
    found = remembered.fitting([c.get("remember") or {} for c in commands], said_breadth(said))
    if isinstance(found, str):
        return f"That yes is just for this once: {found}.", False
    rules, breadth = found
    return offer.put(rules, approval_id, breadth), True


def saved(kept: dict) -> str:
    """What JARVIS says once a rule offered has been kept, or couldn't be."""
    if not kept.get("rules"):
        return f"I couldn't keep that one: {kept.get('never', 'it can’t be remembered')}."
    return "Saved. You can withdraw it any time, in the control panel's settings or by asking me."


def note(approval: dict, decision: str) -> str:
    """For the brain's context: what the user just said yes or no to."""
    word = "approved" if decision == "approve" else "declined"
    return f"{profile.name()} {word} task {approval['task']}'s request {approval['id']} to run {approval['summary']}."
