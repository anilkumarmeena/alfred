"""Runner mixin for approval requests: a task asking to run a risky command on the computer.

Requests live in tasks.db and are batched, announced (push, desktop notification, voice), answered (push, panel, voice,
assistant, CLI), turned into remembered rules, auto-approved by the user's own orders, lapsed, or withdrawn.
See jarvis.tasks.runner for the full flow.
"""

import asyncio
import hashlib
import hmac
import json
import logging
import secrets
import time
from collections.abc import Callable
from pathlib import Path

from jarvis.core import profile
from jarvis.core.config import TASK_APPROVAL_HOURS
from jarvis.core.database import APPROVED, CANCELLED, DENIED, EXPIRED, PENDING, SPENT, WAIT_APPROVAL
from jarvis.safety import remembered
from jarvis.safety.commands import ordered, orders
from jarvis.state import activity as bell
from jarvis.tasks.runner_base import RunnerBase

log = logging.getLogger("jarvis.tasks.runner")

APPROVAL_BATCH_SECONDS = 5  # asks within this window share one request
APPROVAL_QUESTION = "may it run "  # opens the question of a task waiting only on approvals
ACCESS_QUESTION = "may it also "  # opens a request_access question
# "panel" needs no nonce: jarvis.server already requires the passcode for it
_VIA = {"push": "with a tap", "web": "on the web page", "panel": "in the control panel", "voice": "by voice",
        "cli": "on the command line", "assistant": "by telling JARVIS", "rule": "with a rule they made",
        "order": "by telling JARVIS to do it"}
_OWN_ANSWERS = ("rule", "order")  # the runner answering on the user's earlier say-so
SAID_MOST = 500  # chars of the user's words kept in the task log
ORDER_HOURS = remembered.SESSION_HOURS  # orders last until the task ends, capped at this
# Withdrawal reason, by the `via` that withdrew the request
WITHDRAWN = {"re-queued": "{name} sent the task new instructions, so it started a new run",
             "stopped": "the task stopped", "failed": "the task failed", "done": "the task finished",
             "discarded": "the task was discarded"}


class NotAllowed(PermissionError):
    """The answer's nonce or assistant details don't match the request."""


def _hash(nonce: str) -> str:
    return hashlib.sha256(nonce.encode()).hexdigest()


def approval_summary(commands: list[dict]) -> str:
    """'a command that ...' or '2 commands: one that ..., one that ...'."""
    if len(commands) == 1:
        return f"a command that {commands[0]['why']}"
    return f"{len(commands)} commands: " + ", ".join(f"one that {c['why']}" for c in commands)


def approval_question(pending: list[dict]) -> str:
    """Question for a task waiting only on approvals: 'may it run a command that ...?'"""
    return APPROVAL_QUESTION + " and ".join(approval_summary(a["commands"]) for a in pending) + "?"


def approval_spoken(approval: dict, task: dict) -> str:
    """What the voice agent asks. Wording comes from the gate's analysis, not the task's own text."""
    return f"Your task, {task['title']}, wants to run {approval_summary(approval['commands'])}. Okay to go ahead?"


def approval_result(approval: dict) -> str:
    """Message to the task once a request is answered, withdrawn or lapsed."""
    n, state, via = approval["id"], approval["state"], _VIA.get(approval["via"] or "", "")
    if state == APPROVED:
        listed = [f"with {c['tool']}, in {c['folder']}: {c['command']}" for c in approval["commands"]]
        exactly = listed[0] if len(listed) == 1 else " ".join(f"({i}) {c}" for i, c in enumerate(listed, 1))
        return f"{profile.name()} approved request {n} {via}. Run exactly this, once, {exactly}"
    if state == DENIED:
        return (f"{profile.name()} declined request {n} {via}. Don't run it or a variant of it. Carry on without it, "
                "or explain in your summary.")
    if state == CANCELLED:
        return (f"Request {n} was withdrawn unanswered: {WITHDRAWN.get(approval['via'] or '', 'the task moved on').format(name=profile.name())}. "
                "Don't run it. If you still need it, run it again and JARVIS will ask afresh.")
    return (f"No answer to request {n} within {TASK_APPROVAL_HOURS:g} hours, so it has lapsed. Don't run it. Finish what "
            f"you can, and end with the exact command for {profile.name()} to run themselves.")


# Settled state -> (word for push and bell, light colour)
SETTLED = {APPROVED: ("approved", "ok"), DENIED: ("declined", "bad"), EXPIRED: ("lapsed", "warn"),
           CANCELLED: ("no longer needed", "warn")}


def _commands(approval: dict) -> str:
    return " and ".join(c["command"] for c in approval["commands"]) or approval_summary(approval["commands"])


def _check_via(via: str) -> None:
    """ValueError unless `via` is a user channel (not "rule" or "order")."""
    if via not in _VIA or via in _OWN_ANSWERS:
        raise ValueError(f"via must be one of {', '.join(v for v in _VIA if v not in _OWN_ANSWERS)}.")


def session_key(task_id: int) -> str:
    """Session key for a task's session-scoped remembered rules."""
    return f"task:{task_id}"


def _offered(approval: dict) -> list[dict]:
    """Per-command rememberable offers, as computed by the gate."""
    return [c.get("remember") or {} for c in approval["commands"]]


def _approval_view(approval: dict, task: dict | None = None) -> dict:
    """API view of a request plus its remember offer; never includes the nonce hash."""
    view = {k: v for k, v in approval.items() if k != "nonce_hash"} | {
        "summary": approval_summary(approval["commands"]), "remember": remembered.offer(_offered(approval))}
    if task:
        view |= {"title": task["title"], "spoken": approval_spoken(approval, task)}
    return view


class Approvals(RunnerBase):
    """Approval requests and the user's standing orders on a task."""

    def cleared(self, task_id: int, item: dict) -> bool:
        """Whether this host command may run now, and log why.

        Cleared by an approval for this exact command in the resumed run, a standing order, or a remembered rule
        (always-scope or this task's session)."""
        if (approval := self.store.use_approved(task_id, item)) is not None:
            self.store.log(task_id, "agreed", f"running, as agreed in request {approval['id']}: {item['command']}")
            return True
        if said := self._ordering(task_id, [item]):
            self.store.log(task_id, "agreed", f"running, as {profile.name()} said (\"{said}\"): {item['command']}")
            return True
        if used := remembered.covering(item.get("remember") or {}, [session_key(task_id)]):
            self.store.log(task_id, "agreed", f"running under {remembered.told(used)}: {item['command']}")
            return True
        return False

    def _orders(self, task_id: int) -> list[dict]:
        """Unexpired orders kept on the task, oldest first."""
        kept = json.loads((self.store.get(task_id) or {}).get("orders") or "[]")
        return [o for o in kept if time.time() - o["at"] < ORDER_HOURS * 3600]

    def _ordering(self, task_id: int, items: list[dict]) -> str:
        """The first order covering every one of `items`, or ''."""
        return next((o["said"] for o in self._orders(task_id)
                     if all(ordered(o["said"], i["command"]) for i in items)), "")

    def _order(self, task_id: int, said: str | None) -> None:
        """Keep the user's words if they order a risky action ("merge and push"); such commands then skip asking."""
        said = " ".join((said or "").split())[:SAID_MOST]
        if not orders(said):
            return
        kept = [o for o in self._orders(task_id) if o["said"] != said] + [{"said": said, "at": time.time()}]
        self.store.set(task_id, orders=json.dumps(kept, ensure_ascii=False))
        self.store.log(task_id, "order", f"{profile.name()}'s own words, which go ahead for what they order: \"{said}\"")

    def _apply_orders(self, task_id: int) -> list[int]:
        """Approve the task's pending requests fully covered by its orders."""
        def said(approval: dict) -> str:
            return (words := self._ordering(task_id, approval["commands"])) and f"as {profile.name()} said (\"{words}\")"

        return self._answer_each(task_id, APPROVED, "order", said)

    def request_approval(self, task_id: int, item: dict) -> dict:
        """Ask the user about a risky host command; asks within APPROVAL_BATCH_SECONDS share one announcement."""
        expires = time.time() + TASK_APPROVAL_HOURS * 3600
        approval, new = self.store.ask_approval(task_id, item, expires, APPROVAL_BATCH_SECONDS)
        if new:
            self.store.log(task_id, "approval", f"request {approval['id']} asks to run a command that {item['why']}, "
                                                f"in {item['folder']}: {item['command']}")
            if len(approval["commands"]) == 1:  # first of the batch: announce after the window
                self._background(self._announce_approval(approval["id"], APPROVAL_BATCH_SECONDS))
        return approval

    async def _announce_approval(self, approval_id: int, delay: float = 0) -> None:
        """Announce a request: voice event, desktop notification, and a push carrying a one-time nonce."""
        await asyncio.sleep(delay)
        approval = self.store.approval(approval_id)
        if not approval or approval["state"] != PENDING or approval["announced"]:
            return
        nonce = secrets.token_urlsafe(32)
        approval = self.store.set_approval(approval_id, announced=1, nonce_hash=_hash(nonce))
        task = self.store.get(approval["task"])
        spoken = approval_spoken(approval, task)
        self.broadcast({"type": "approval", "text": spoken, "approval": _approval_view(approval, task)})
        first = approval["commands"][0]
        more = f" (and {len(approval['commands']) - 1} more)" if len(approval["commands"]) > 1 else ""
        self._background(self._notify(f"JARVIS · task {task['id']}", f"Needs your go-ahead: {approval_summary(approval['commands'])}. "
                                                                    f"Answer on the phone, or: runner approve {approval_id}"))
        url, tag = f"/?task={task['id']}&approval={approval_id}", f"approval-{approval_id}"
        self._background(self._push(
            f"Task {task['id']} needs your go-ahead", f"{first['command'][:300]}{more}\nin {Path(first['folder']).name}",
            url, tag, subtitle=first["why"], category="JARVIS_APPROVAL",
            data={"approval": {"id": approval_id, "task": task["id"], "nonce": nonce, "expires": int(approval["expires"])}}))

    def approvals(self) -> list[dict]:
        """Pending requests, oldest first."""
        return [_approval_view(a, self.store.get(a["task"])) for a in self.store.approvals()]

    def answer(self, approval_id: int, decision: str, via: str, nonce: str | None = None, task: int | None = None,
               commands: list[dict] | None = None, said: str | None = None, remember: str = "once",
               breadth: str = "exact", whole_task: bool = False) -> dict:
        """Record the user's answer. First answer wins; later ones get the current state with answered=False.

        Phone answers need the nonce; assistant answers must match task, exact commands and carry the user's words.
        A yes may `remember` (session/always, at `breadth`), result in `remembered`. `whole_task` applies the answer
        to the task's other announced pending requests, listed in `also`. Raises NotAllowed or ValueError."""
        if decision not in ("approve", "deny"):
            raise ValueError("decision must be approve or deny.")
        _check_via(via)
        if remember not in ("once", *remembered.SCOPES) or breadth not in remembered.BREADTHS:
            raise ValueError("remember must be once, session or always, and breadth exact or kind.")
        if not (approval := self.store.approval(approval_id)):
            raise ValueError(f"There's no request {approval_id}.")
        if via in ("push", "web") and nonce is None:
            raise NotAllowed("An answer from the phone needs the request's nonce.")
        if nonce is not None and not (approval["nonce_hash"] and hmac.compare_digest(_hash(nonce), approval["nonce_hash"])):
            raise NotAllowed("That answer doesn't match the request.")
        if via == "assistant":
            self._check_told(approval, task, commands, said)
        state = APPROVED if decision == "approve" else DENIED
        if approval["state"] != PENDING or not self.store.answer_approval(approval_id, state, via):
            approval = self.store.approval(approval_id)
            known = "already answered" if approval["state"] in (APPROVED, DENIED, SPENT) else "no longer needed"
            return _approval_view(approval) | {"answered": False, "outcome": f"Request {approval_id} was {known}."}
        if via == "assistant":
            said = " ".join(said.split())[:SAID_MOST]
            log.warning("request %s %s through the assistant, on hearing: %s", approval_id, state, said)
            self.store.log(approval["task"], "assistant", f"request {approval_id} {state} through the assistant, on "
                                                          f"{profile.name()}'s words: \"{said}\"")
        view = _approval_view(self._settled(approval_id)) | {"answered": True, "outcome": f"Request {approval_id} {state}."}
        also = self._answer_rest(approval, state, via) if whole_task else []
        if also:
            view["also"] = also
            listed = ", ".join(f"request {n}" for n in also)
            view["outcome"] += f" So {'was' if len(also) == 1 else 'were'} {listed}."
        if state == APPROVED and remember != "once":
            kept, never = {}, ""
            for n in [approval_id, *also]:
                try:
                    kept |= {r["id"]: r for r in self.remember(n, breadth, via, remember)}
                except remembered.NotRememberable as e:
                    never = never or str(e)
            view["remembered"] = {"rules": list(kept.values())} | ({"never": never} if never and not kept else {})
        return view

    def _answer_rest(self, approval: dict, state: str, via: str) -> list[int]:
        """Apply the same answer to the task's other announced pending requests; returns their ids."""
        with_it = f"{_VIA[via]}, with request {approval['id']}"
        return self._answer_each(approval["task"], state, via, lambda other: with_it if other["announced"] else "")

    def remember(self, approval_id: int, breadth: str, via: str, scope: str = "always") -> list[dict]:
        """Save rules from an approved request (session or always), then approve other requests they cover.

        The caller must have shown the user what the rules cover. Raises NotRememberable for always-ask commands."""
        if not (approval := self.store.approval(approval_id)):
            raise ValueError(f"There's no request {approval_id}.")
        if approval["state"] not in (APPROVED, SPENT):
            raise ValueError(f"Request {approval_id} wasn't approved, so there's nothing to remember.")
        _check_via(via)
        rules = remembered.candidates(_offered(approval), breadth)
        session = session_key(approval["task"]) if scope == "session" else None
        kept = remembered.save(rules, scope, _commands(approval), via, session)
        what = f"{'for its session' if scope == 'session' else 'always'}: {remembered.covers(kept)}"
        self.store.log(approval["task"], "remembered", f"{profile.name()} said {_VIA[via]} to allow, {what}")
        bell.add(f"Remembered approval, {what}", "ok", approval["task"])
        self.apply_rules()
        return kept

    def apply_rules(self) -> list[int]:
        """Approve pending requests fully covered by current rules; returns their ids."""
        def ruled(approval: dict) -> str:
            keys = [session_key(approval["task"])]
            covered = all(remembered.covering(o, keys) for o in _offered(approval))
            return f"by a rule {profile.name()} made" if covered else ""

        return self._answer_each(None, APPROVED, "rule", ruled)

    def _answer_each(self, task_id: int | None, state: str, via: str, why: Callable[[dict], str]) -> list[int]:
        """Answer each pending request (one task's, or all) for which `why` returns a reason ('' skips); returns ids."""
        done = []
        for approval in self.store.approvals(task_id):
            if (reason := why(approval)) and self.store.answer_approval(approval["id"], state, via):
                self.store.log(approval["task"], "agreed" if state == APPROVED else "declined",
                               f"request {approval['id']} {state} {reason}")
                self._settled(approval["id"])
                done.append(approval["id"])
        return done

    @staticmethod
    def _check_told(approval: dict, task: int | None, commands: list[dict] | None, said: str | None) -> None:
        """NotAllowed unless an assistant answer matches the request exactly as announced."""
        if task != approval["task"]:
            raise NotAllowed(f"Request {approval['id']} isn't task {task}'s.")
        if not approval["announced"]:
            raise NotAllowed(f"Request {approval['id']} hasn't been put to {profile.name()} yet.")
        if commands != approval["commands"]:
            raise NotAllowed(f"Request {approval['id']} isn't for those commands.")
        if not (isinstance(said, str) and said.strip()):
            raise NotAllowed(f"An answer through the assistant carries {profile.name()}'s words.")

    def _settled(self, approval_id: int) -> dict:
        """Deliver a settled request's outcome to the task (nudge or relaunch), the voice agent and the phone."""
        approval = self.store.approval(approval_id)
        task_id, state = approval["task"], approval["state"]
        result = approval_result(approval)
        self.store.log(task_id, f"request {state}", result)
        task = self.store.get(task_id)
        if task["status"] in ("queued", "running"):
            self.store.add_nudge(task_id, result)  # delivered after its next tool call
        elif task["status"] == "waiting" and task["wait_kind"] != WAIT_APPROVAL:
            self.store.add_nudge(task_id, result)  # waiting on its own question: deliver with that answer
        elif task["status"] in ("waiting", "stopped") and state != CANCELLED:
            if not task["worktree"] or task["node"] or Path(task["worktree"]).exists():  # a machine's folder is there
                self._launch(task_id, result)
        word = SETTLED[state][0]
        self.broadcast({"type": "approval_answered", "text": f"Task {task_id}'s request {approval_id} {word}.",
                        "approval": _approval_view(approval)})
        carries_on = " It carries on." if state in (APPROVED, DENIED) else ""
        if approval["announced"]:  # same tag: replaces the push with buttons
            self._background(self._push(f"Task {task_id}: request {word}", f"{approval_summary(approval['commands'])}."
                                        f"{carries_on}", f"/?task={task_id}", f"approval-{approval_id}"))
        return approval

    def _close_approvals(self, task_id: int, why: str = "stopped") -> None:
        """Withdraw the task's pending requests (it stopped, ended or was re-queued)."""
        for approval in self.store.approvals(task_id):
            if self.store.answer_approval(approval["id"], CANCELLED, why):
                self._settled(approval["id"])

    def _spend_approvals(self, task_id: int, before: float) -> None:
        """A yes is valid only for the run it resumed: mark unused approvals from before `before` spent."""
        for approval in self.store.approvals(task_id, (APPROVED,)):
            if (approval["answered_at"] or 0) < before:
                self.store.set_approval(approval["id"], state=SPENT)

    async def _expire_approvals(self, every: float = 60) -> None:
        """Loop: lapse expired requests, and approve any newly covered by rules."""
        while True:
            now = time.time()
            for approval in self.store.approvals():
                if approval["expires"] <= now and self.store.answer_approval(approval["id"], EXPIRED, "timeout"):
                    self._settled(approval["id"])
            try:
                self.apply_rules()
            except Exception:
                log.exception("checking requests against remembered approvals failed")
            await asyncio.sleep(every)
