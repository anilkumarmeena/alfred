"""One voice call's parts and state, on one object: the brain, tasks' requests put to the user, the floor and its
announcements, the turn's timing and pacing, and the background lookups. jarvis.voice.agent makes one per call, from
the LiveKit side of it; nothing here needs LiveKit, so a call's routing is tested with stand-ins.

Each user turn goes through `reply`: a yes or no to a rule just offered, or to a task's request just asked, is heard
before the brain sees it; anything else is the brain's. Tasks' requests come in on the runner's event stream
(`watch_tasks`) and are put to the user once one comes due and the floor is free (`ask_approvals`), woken by the
requests themselves (VoiceApprovals.changed) rather than looking every few seconds.
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

# A request held back by the brain's own question about a command is looked at again this often, as well as after
# each turn: the question can lapse on its own (jarvis.safety.shell's APPROVAL_MINUTES)
QUESTION_RECHECK_SECONDS = 30
# The runner's events, followed for as long as the call lasts: again 30 s after they stop or fail, whatever the error
RUNNER_EVENTS = retry.Policy(waits=(30,), retries=retry.KINDS, forever=True, passing=(client.RunnerUnavailable,))


class VoiceSession:
    """One call. `talk` is its conversation (jarvis.state.conversation: the profile, the rule offered, and what tools
    reach), the current one by default. `stt` is the session's WhisperSTT (who spoke each turn, how sure Whisper was,
    the speaker check), `turns` how each turn's end was decided, `pacing` each user's own pace, `router` a lighter
    model for simple turns and `moment` when to bring things up, each when set. `runner` is the task runner's client."""

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
        self.asks = VoiceApprovals()  # tasks' requests put to the user in this call
        self.floor = floor or Floor()  # whether an exchange is under way, which announcements wait out
        self.announcements = Announcements(self.floor, self.floor.clock, moment=moment, push=push)
        # Slow reading the brain hands off with background_lookup, announced when it's done
        self.lookups = side.Jobs(lambda text, note: self.announcements.add(Announcement(text, note)))
        self._hints: list[str] = []  # what to tell the brain about how the next turn was heard
        self._last_words = ""
        self._loops: list[asyncio.Task] = []

    @property
    def _quick_models(self) -> list:
        """The fast model's warm sessions, which start with the call and close with it."""
        judge = self.turns.judge if self.turns is not None else None
        return [checker.quick for checker in (judge, self.router, self.moment) if checker is not None]

    def open(self) -> None:
        """Before the room connects: the parts tools reach from outside the call, the speaker check (voice_enroll) and
        the lookups (background_lookup), are this call's, and the fast model's sessions start."""
        if self.stt is not None:
            # A spoken enrollment's end is said when the floor is free
            self.stt.speaker.announce = lambda text: self.announcements.add(Announcement(text))
            self.talk.speaker = self.stt.speaker
        self.talk.lookups = self.lookups
        for quick in self._quick_models:
            quick.warm()

    def start(self, session) -> None:
        """Once the greeting's said: task updates, alerts and requests are put to the user in LiveKit's `session` for
        as long as the call lasts. Long shell jobs are command tasks, so watch_tasks hears them end too."""
        self._loops = [asyncio.create_task(self.watch_tasks()),
                       asyncio.create_task(self.announcements.run(session, self.brain, self.stt, self.asks,
                                                                  self.runner.mark_seen)),
                       asyncio.create_task(self.ask_approvals(session))]

    async def close(self) -> None:
        """The call is over: everything it started stops, what's still waiting goes to the phone, and the runner is
        told, for the learning review."""
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
        """Tell the runner the conversation is over. It finds it by the quiet anyway, later."""
        try:
            await self.runner.session_ended()
        except Exception as e:
            log.info("couldn't tell the task runner the session ended: %s", type(e).__name__)

    # ── Each turn ──────────────────────────────────────────────

    def transcribed(self, transcript: str) -> None:
        """The user's words, transcribed; the turn detector may still be waiting for more."""
        self.floor.heard(transcript)
        if self.turns is not None:
            self.turns.heard(transcript)
            if self.router is not None:
                self.router.guess(" ".join(self.turns.words))  # which model answers, decided while the end is awaited

    def completed(self, said: str, metrics) -> None:
        """The user's turn ended with `said`: how it ended and who spoke it are logged, and the brain will be told how
        it was heard."""
        how = self.turns.committed() if self.turns is not None else {}
        who, logprob = turn.clips_heard(self.stt.take_clips() if self.stt is not None else [])
        how |= {"speaker": who} | ({"logprob": round(logprob, 3)} if logprob is not None else {})
        cut = self.timings.heard(said, metrics, how)
        if self.pacing is not None:
            self.pacing.use(who)
            self._hints = self.pacing.hints(logprob, self._last_words if cut else None)
        self._last_words = said

    def spoke(self, text: str, metrics, interrupted: bool) -> None:
        """JARVIS's words were spoken, with LiveKit's metrics: when their audio started, and Kokoro's."""
        self.timings.spoke(metrics, interrupted)
        if self.turns is not None:
            self.turns.said(text)

    async def respond(self, text: str | None) -> AsyncIterator[str]:
        """The reply to a user turn that reached JARVIS, timed."""
        self.floor.turn()  # the user's words answer any question open
        if not text:
            return
        self.timings.replying()
        async for chunk in self.reply(text):
            timing.mark("first_text")
            yield chunk

    async def reply(self, text: str) -> AsyncIterator[str]:
        """A yes or no to saving a rule just offered, or to a task's request, is heard before the brain sees it: by the
        word lists, or failing them a short model call that sees only these words (jarvis.safety.consent). Anything
        else is the brain's."""
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
        self._said("".join(said))  # a reply ending in a question holds announcements until it's answered
        if self.brain.question_open():
            self.asks.close()
        self.asks.changed.set()  # the brain's own question may be over, freeing a request waiting
        ram.log_job()  # each process's memory after every reply, at debug level

    def _said(self, reply: str) -> str:
        self.floor.said(reply)
        return reply

    async def answer(self, said: str, approval: dict, decision: str, scope: str = "once") -> str:
        """Send a spoken yes or no to the runner, for every request of that task waiting; returns what to say. A "for
        this session" is remembered with it; an "always" says what the rule would cover and asks to save it
        (the conversation's `offer`)."""
        try:
            remember = "session" if scope == "session" else "once"
            outcome = await self.runner.answer(approval["id"], decision, "voice", remember=remember, whole_task=True)
            reply = approvals.answered(approval, decision, outcome)
            self.brain.note(approvals.note(approval, decision))
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
        """Save, or not, the rule just offered after an "always"; returns what to say."""
        if not yes:
            reply = "Okay, I won't remember it."
        else:
            try:
                if offer["approval"] is not None:
                    kept = await self.runner.remember(offer["approval"], offer["breadth"], "voice")
                else:  # the voice shell's own command
                    kept = {"rules": await asyncio.to_thread(remembered.save, offer["rules"], "always",
                                                             offer["example"], "voice")}
                reply = approvals.saved(kept)
            except Exception as e:
                log.warning("saving a remembered approval failed: %s", e)
                reply = "Sorry, I couldn't save that rule."
        self.brain.note(f"{profile.name()} {'agreed to' if yes else 'turned down'} remembering: "
                        f"{remembered.covers(offer['rules'])}.")
        memory.log_exchange(said, reply)
        return reply

    # ── Unprompted ─────────────────────────────────────────────

    async def greeting(self) -> str:
        greeting = f"Hello {profile.name()}. JARVIS online."
        try:
            async with asyncio.timeout(2):
                unseen, pending = await asyncio.gather(self.runner.tasks(unseen=True), self.runner.approvals())
        except Exception:  # the runner being down mustn't delay the greeting
            return greeting
        if n := len(unseen):
            greeting += f" There {'is one task update' if n == 1 else f'are {n} task updates'} waiting."
            self.brain.note(greeting)  # so "what are they?" leads to task_list
        for approval in pending:
            self.asks.add(approval)
        if approval := self.asks.due():  # the oldest request still waiting: the greeting ends with it
            spoken = self.asks.ask(approval)
            self.brain.note(spoken)
            greeting += " " + spoken
        return greeting

    async def ask_approvals(self, session) -> None:
        """Put tasks' requests to the user when the floor is free (jarvis.voice.floor), one at a time, while the call
        lasts: woken when one may have come due, never while the brain has a question of its own open."""
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
                self.talk.offer.close()  # a yes now is for this request, not a rule offered before it
                spoken = self.asks.ask(approval)
                self.brain.note(spoken)
                await self.floor.say(session, spoken)

    async def watch_tasks(self) -> None:
        """Queue background-task updates and heartbeat alerts for announcing, for as long as the call lasts. Tasks'
        requests to run risky commands go to `asks`, which ask_approvals puts to the user. When the runner's events stop
        (it's down or restarting) or fail, they're followed again as RUNNER_EVENTS says."""

        async def follow() -> None:
            async for event in self.runner.events():
                self._task_event(event)
            raise client.RunnerUnavailable(client.DOWN)  # the stream ended: the runner stopped

        await retry.retry(RUNNER_EVENTS, follow, "task announcements")

    def _task_event(self, event: dict) -> None:
        if event["type"] == "approval":
            self.asks.add(event["approval"])
        elif event["type"] == "approval_answered":
            self.asks.drop(event["approval"]["id"])
        elif event["type"] == "heartbeat":  # alerts and reminders, with how urgent and when they're about
            self.announcements.add(Announcement(event["text"], seen=list(event["tasks"] or []),
                                                urgency=event.get("urgency", "soon"), at=event.get("at"),
                                                pushed=event.get("pushed", True)))
        elif event["type"] == "status" and not event["task"]["seen"]:  # the runner marks what's worth saying
            # The brain also hears, unsaid, which task it was and each edit it suggests, by number. The runner pushes
            # it, and one left unheard stays unseen for the next greeting
            self.announcements.add(Announcement(event["text"], announce.context(event["task"]), [event["task"]["id"]],
                                                pushed=True))
