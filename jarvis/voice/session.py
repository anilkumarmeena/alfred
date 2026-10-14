"""One voice call's state: brain, task approval requests, floor and announcements, timing, pacing, lookups.

LiveKit-free, so call routing is testable with stand-ins. Each user turn goes through `reply`: a yes/no to an offered
rule or a pending task request is handled before the brain sees it. Task requests arrive on the runner's event stream
(`watch_tasks`) and are asked when due and the floor is free (`ask_approvals`, event-driven, no polling).
"""

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import suppress

from jarvis.brain import VoiceBrain, retry, side
from jarvis.brain.route import Router
from jarvis.core import profile
from jarvis.safety import approvals, consent, remembered
from jarvis.safety.approvals import VoiceApprovals
from jarvis.state import conversation, memory
from jarvis.tasks import announce, client
from jarvis.voice import ram, timing, turn
from jarvis.voice.floor import Announcement, Announcements, Floor, Moment

log = logging.getLogger("jarvis")

# Recheck a request blocked by the brain's own open question this often: that question can lapse on its own
QUESTION_RECHECK_SECONDS = 30
# Follow runner events for the whole call; reconnect 30 s after any stop or failure
RUNNER_EVENTS = retry.Policy(waits=(30,), retries=retry.KINDS, forever=True, passing=(client.RunnerUnavailable,))


def _waiting(n: int, more: str = "") -> str:
    """How many task updates wait, as a sentence with a leading space ("" for none); `more` is " more" after a lead."""
    if not n:
        return ""
    if n == 1:
        return f" There is one{more} task update waiting."
    return f" There are {n}{more} task updates waiting."


class VoiceSession:
    """One call.

    `talk` defaults to the current conversation. Optional parts: `stt` (WhisperSTT), `turns` (end-of-turn decisions),
    `pacing` (per-user pace), `router` (lighter model for simple turns), `moment` (when to raise things).
    `runner` is the task runner client."""

    def __init__(self, brain: VoiceBrain, talk: conversation.Conversation | None = None, *, stt=None,
                 turns: turn.JudgedTurns | None = None, pacing: turn.Pacing | None = None, router: Router | None = None,
                 moment: Moment | None = None, timings: timing.Timings | None = None, floor: Floor | None = None,
                 push: Callable[[str], Awaitable[object]] | None = None, runner=client) -> None:
        self.brain = brain
        self.talk = talk or conversation.current()
        self.stt = stt
        self.turns = turns
        self.pacing = pacing
        self.router = router
        self.moment = moment
        self.timings = timings or timing.Timings(path=None)
        self.runner = runner
        self.asks = VoiceApprovals()  # task requests asked in this call
        self.floor = floor or Floor()  # announcements wait for a free floor
        self.announcements = Announcements(self.floor, self.floor.clock, moment=moment, push=push)
        # background_lookup jobs, announced when done
        self.lookups = side.Jobs(lambda text, note: self.announcements.add(Announcement(text, note)))
        self._hints: list[str] = []  # how the next turn was heard, for the brain
        self._last_words = ""
        self._loops: list[asyncio.Task] = []
        self._greeted: int | None = None  # the task whose update the greeting led with

    @property
    def _quick_models(self) -> list:
        """Fast-model sessions, opened and closed with the call."""
        judge = self.turns.judge if self.turns is not None else None
        return [checker.quick for checker in (judge, self.router, self.moment) if checker is not None]

    def open(self) -> None:
        """Before the room connects: expose speaker check and lookups to tools; warm the fast-model sessions."""
        if self.stt is not None:
            # announce enrollment completion when the floor is free
            self.stt.speaker.announce = lambda text: self.announcements.add(Announcement(text))
            self.talk.speaker = self.stt.speaker
        self.talk.lookups = self.lookups
        # slow tool calls that left their turn come back as follow-ups the brain speaks itself
        self.brain.on_background(self._background)
        for quick in self._quick_models:
            quick.warm()

    def start(self, session) -> None:
        """After the greeting: mark the update it led with as heard; start the task-update, announcement and approval
        loops on LiveKit's `session`."""
        self._loops = [asyncio.create_task(self.watch_tasks()),
                       asyncio.create_task(self.announcements.run(session, self.brain, self.stt, self.asks,
                                                                  self.runner.mark_seen)),
                       asyncio.create_task(self.ask_approvals(session))]
        if self._greeted is not None:
            self._loops.append(asyncio.create_task(self._heard_greeting(self._greeted)))

    async def _heard_greeting(self, task_id: int) -> None:
        """Heard, the greeting's lead isn't led with again next time."""
        try:
            await self.runner.mark_seen([task_id])
        except Exception as e:
            log.warning("marking task %s as heard failed: %s", task_id, e)

    async def close(self) -> None:
        """Stop all loops, push pending announcements to the phone, and tell the runner (for the learning review)."""
        for loop in self._loops:
            loop.cancel()
        if self.stt is not None:
            self.stt.speaker.cancel_enrollment()
        self.talk.speaker = self.talk.lookups = None
        self.timings.close()
        steps = [self.lookups.close(), self.announcements.leave(), self.brain.end(), self._ended(),
                 *(quick.close() for quick in self._quick_models)]
        for failed in await asyncio.gather(*steps, return_exceptions=True):
            if isinstance(failed, BaseException):
                log.error("closing the voice session", exc_info=failed)
        conversation.end(self.talk)

    async def _ended(self) -> None:
        """Notify the runner. Best effort: it also detects the end by inactivity."""
        try:
            await self.runner.session_ended()
        except Exception as e:
            log.info("couldn't tell the task runner the session ended: %s", type(e).__name__)

    # ── Each turn ──────────────────────────────────────────────

    def transcribed(self, transcript: str) -> None:
        """A final transcript; the turn may not have ended yet."""
        self.floor.heard(transcript)
        if self.turns is not None:
            self.turns.heard(transcript)
            if self.router is not None:
                self.router.guess(" ".join(self.turns.words))  # pick the model while waiting for the turn end

    def completed(self, said: str, metrics) -> None:
        """User turn ended: log how and by whom, and queue hints on how it was heard for the brain."""
        how = self.turns.committed() if self.turns is not None else {}
        who, logprob = turn.clips_heard(self.stt.take_clips() if self.stt is not None else [])
        how |= {"speaker": who} | ({"logprob": round(logprob, 3)} if logprob is not None else {})
        cut = self.timings.heard(said, metrics, how)
        if self.pacing is not None:
            self.pacing.use(who)
            self._hints = self.pacing.hints(logprob, self._last_words if cut else None)
        self._last_words = said

    def spoke(self, text: str, metrics, interrupted: bool) -> None:
        """JARVIS finished speaking `text`; `metrics` are LiveKit's."""
        self.timings.spoke(metrics, interrupted)
        if self.turns is not None:
            self.turns.said(text)

    async def respond(self, text: str | None) -> AsyncIterator[str]:
        """Timed reply to a user turn."""
        self.floor.turn()  # user's words answer any open question
        if not text:
            return
        self.timings.replying()
        async for chunk in self.reply(text):
            timing.mark("first_text")
            yield chunk

    async def reply(self, text: str) -> AsyncIterator[str]:
        """Route a turn: yes/no to an offered rule or pending task request is handled here (jarvis.safety.consent);
        anything else goes to the brain."""
        verdict = await consent.reply(text, consent.RULE) if self.talk.offer.open else None
        if offered := self.talk.offer.hear(text, verdict):
            yield self._said(await self.keep(text, *offered))
            return
        verdict = await consent.reply(text, consent.TASK, self.asks.open.get("about", "")) if self.asks.open else None
        if heard := self.asks.hear(text, verdict):
            yield self._said(await self.answer(text, *heard))
            return
        said: list[str] = []
        if self._hints:
            self.brain.frame(self._hints)
            self._hints = []
        if self.router is not None:
            self.brain.next_model = await self.router.pick(text)
        async for chunk in self.brain.respond(text):
            said.append(chunk)
            yield chunk
        self._said("".join(said))  # a trailing question holds announcements
        if self.brain.question_open():
            self.asks.close()
        self.asks.changed.set()  # the brain's question may be over
        ram.log_job()  # debug-level memory line

    def _said(self, reply: str) -> str:
        self.floor.said(reply)
        return reply

    async def answer(self, said: str, approval: dict, decision: str, scope: str = "once") -> str:
        """Send a spoken decision for all of that task's pending requests; return what to say.

        scope "session" is remembered for the session; "always" offers to save a rule (via `talk.offer`)."""
        try:
            remember = "session" if scope == "session" else "once"
            outcome = await self.runner.answer(approval["id"], decision, "voice", remember=remember, whole_task=True)
            reply = approvals.answered(approval, decision, outcome)
            self.brain.note("", approvals.note(approval, decision))  # the reply says it; this is the detail
            if decision == "approve" and scope == "always" and outcome.get("answered"):
                added, asked = approvals.offer_always(self.talk.offer, approval["id"], outcome["commands"], said)
                reply += " " + added
                if asked:
                    self.brain.note(added)
        except Exception as e:
            log.warning("answering request %s failed: %s", approval["id"], e)
            reply = "Sorry, I couldn't pass that on to the task runner."
        memory.log_exchange(said, reply)
        return reply

    async def keep(self, said: str, offer: dict, yes: bool) -> str:
        """Save (or decline) the rule offered after an "always"; return what to say."""
        if not yes:
            reply = "Okay, I won't remember it."
        else:
            try:
                if offer["approval"] is not None:
                    kept = await self.runner.remember(offer["approval"], offer["breadth"], "voice")
                else:  # voice shell's own command
                    kept = {"rules": await asyncio.to_thread(remembered.save, offer["rules"], "always",
                                                             offer["example"], "voice")}
                reply = approvals.saved(kept)
            except Exception as e:
                log.warning("saving a remembered approval failed: %s", e)
                reply = "Sorry, I couldn't save that rule."
        self.brain.note("", f"{profile.name()} {'agreed to' if yes else 'turned down'} remembering: "
                            f"{remembered.covers(offer['rules'])}.")
        memory.log_exchange(said, reply)
        return reply

    # ── Unprompted ─────────────────────────────────────────────

    async def greeting(self) -> str:
        """Hello, led by the weightiest unseen task update in brief, then how many more wait, then the oldest pending
        request. Fixed wording, no model call, so speech starts at once."""
        greeting = f"Hello {profile.name()}."
        try:
            async with asyncio.timeout(2):
                unseen, pending = await asyncio.gather(self.runner.tasks(unseen=True), self.runner.approvals())
        except Exception:  # a down runner mustn't delay the greeting
            return greeting + " JARVIS online."
        asked = {approval["task"] for approval in pending}  # their requests are asked instead
        unseen = [t for t in unseen if t["id"] not in asked]
        lead = announce.first(unseen)
        self._greeted = lead["id"] if lead else None
        rest = len(unseen) - 1
        if not lead:
            greeting += " JARVIS online." + _waiting(len(unseen))
        elif lead["status"] == "waiting":  # its question goes last, so the floor holds for the answer
            greeting += (_waiting(len(unseen)) if rest else "") + " " + announce.brief(lead)
        else:
            greeting += " " + announce.brief(lead) + _waiting(rest, " more")
        if unseen:  # so "what are they?" leads to task_list, and a reply to the lead reaches its task
            self.brain.note(greeting, announce.context(lead) if lead else "")
        for approval in pending:
            self.asks.add(approval)
        if approval := self.asks.due():  # greeting ends with the oldest pending request
            spoken = self.asks.ask(approval)
            self.brain.note(spoken, approvals.asked(approval))
            greeting += " " + spoken
        return greeting

    def _background(self, summary: str, prompt: str, words: str) -> None:
        """A slow tool call's result: the brain follows up on it when the floor allows."""
        self.announcements.add(Announcement(summary, speak=lambda brain: brain.follow_up(prompt, words)))

    async def ask_approvals(self, session) -> None:
        """Ask pending task requests one at a time when the floor is free; never while the brain's question is open."""
        while True:
            self.asks.changed.clear()
            if not (approval := self.asks.due()) or self.brain.question_open():
                with suppress(TimeoutError):
                    await asyncio.wait_for(self.asks.changed.wait(), QUESTION_RECHECK_SECONDS if approval else None)
                continue
            async with self.floor.lock:
                await self.floor.wait(session, self.stt)
                if self.asks.due() is not approval or self.brain.question_open():  # answered, or the shell asked
                    continue
                self.talk.offer.close()  # a yes now is for this request, not an earlier offer
                spoken = self.asks.ask(approval)
                self.brain.note(spoken, approvals.asked(approval))
                await self.floor.say(session, spoken)

    async def watch_tasks(self) -> None:
        """Queue task updates and alerts as announcements; approval requests go to `asks`.

        Reconnects per RUNNER_EVENTS when the stream ends or fails."""

        async def follow() -> None:
            async for event in self.runner.events():
                self._task_event(event)
            raise client.RunnerUnavailable(client.DOWN)  # stream ended: runner stopped

        await retry.retry(RUNNER_EVENTS, follow, "task announcements")

    def _task_event(self, event: dict) -> None:
        if event["type"] == "approval":
            self.asks.add(event["approval"])
        elif event["type"] == "approval_answered":
            self.asks.drop(event["approval"]["id"])
        elif event["type"] == "alert":  # notices, reminders and watch findings
            self.announcements.add(Announcement(event["text"], seen=list(event["tasks"] or []),
                                                urgency=event.get("urgency", "soon"), at=event.get("at"),
                                                pushed=event.get("pushed", True)))
        elif event["type"] == "status" and not event["task"]["seen"]:  # runner marks what's worth saying
            # The brain gets task context and numbered edits silently. Already pushed; if unheard it stays unseen
            # for the next greeting
            self.announcements.add(Announcement(event["text"], announce.context(event["task"]), [event["task"]["id"]],
                                                pushed=True))
