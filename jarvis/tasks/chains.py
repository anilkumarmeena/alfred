"""Runner mixin for work that waits for other work: a task that starts by itself once others finish or are merged,
and a merge that goes ahead by itself once tasks finish.

  waits_on        JSON [{"task": id, "merged": bool}] on a task saved queued, with no run. Once every one is met (that
                  task done, or its branch merged) it starts by itself and says so. When one can't be (a task failed,
                  was stopped or discarded, a merge failed) it's held: waiting (WAIT_AFTER), asking whether to start it
                  anyway. Held, it still starts by itself if what it waits for comes right after all.
  merge_waits_on  the same for a task's merge, which then runs in the background and says how it went. When one
                  can't be met the merge is called off, holding whatever waits for it in turn.

Event-driven, so waiting costs nothing: `_settle` runs where the runner learns a task ended (Events._finish,
GitOps.discard) or was merged (GitOps.merge), `_merge_failed` where a merge fails, and `settle_all` once at start,
for what ended while the runner was down. Chains of any length, fan-out and fan-in follow from that; a wait that
would close a loop is refused.
"""

import asyncio
import json
import logging

from jarvis.core import profile
from jarvis.core.database import WAIT_AFTER
from jarvis.notify.push import task_target
from jarvis.tasks import hold
from jarvis.tasks.announce import joined
from jarvis.tasks.runner_base import RunnerBase

log = logging.getLogger("jarvis.tasks.runner")
START, MERGE = "waits_on", "merge_waits_on"  # the task's columns: what its start, or its merge, waits for
MET = "met"
ENDED_BADLY = ("failed", "stopped", "discarded")


def waits(task: dict, field: str = START) -> list[dict]:
    """What the task's start (or merge) waits for: [{"task": id, "merged": bool}]."""
    return json.loads(task.get(field) or "[]")


def on(d: dict) -> str:
    """What one wait is for, as said after "waiting for"."""
    return f"task {d['task']} to {'be merged' if d['merged'] else 'finish'}"


def on_all(needs: list[dict]) -> str:
    """What the waits are for, as said after "once"."""
    return joined([f"task {d['task']} {'is merged' if d['merged'] else 'finishes'}" for d in needs])


def _node(d: dict) -> tuple[str, int]:
    """A wait as a node of the graph _no_loop walks: a task's run, or its merge."""
    return ("merge" if d["merged"] else "run", d["task"])


class Chains(RunnerBase):
    """Tasks and merges that wait for other tasks."""

    merging: set[int]  # tasks whose waiting merge is under way

    def _state(self, d: dict) -> str | None:
        """MET, why the wait can't be met any more ("task 3, the login fix, failed"), or None while it still may be."""
        task = self.store.get(d["task"])
        if task is None:
            return f"task {d['task']} no longer exists"
        if self.store.merged(d["task"]) if d["merged"] else task["status"] == "done":
            return MET
        if task["status"] in ENDED_BADLY:
            return f"task {task['id']}, {task['title']}, {'failed' if task['status'] == 'failed' else 'was ' + task['status']}"
        return None

    def _needed(self, after: list[int] | None, after_merge: list[int] | None) -> list[dict]:
        """Checked waits for tasks to finish (`after`) or be merged (`after_merge`); ValueError for one that can't be
        met."""
        needs: list[dict] = []
        for d in [{"task": int(i), "merged": False} for i in after or []] + \
                 [{"task": int(i), "merged": True} for i in after_merge or []]:
            task = self._get(d["task"])
            if d in needs:
                continue
            if d["merged"] and (task["kind"] != "code_change" or task["in_place"]):
                raise ValueError(f"Task {task['id']} has no branch of its own, so there's no merge to wait for: wait for "
                                 "it to finish instead.")
            if (why := self._state(d)) not in (None, MET):
                raise ValueError(f"{why[0].upper()}{why[1:]}, so nothing can wait for it. Ask {profile.name()} what to "
                                 "do instead.")
            needs.append(d)
        return needs

    def _no_loop(self, node: tuple[str, int], needs: list[dict]) -> None:
        """ValueError if `node` waiting for `needs` would close a loop: work waiting, in the end, for itself.

        A merge waits for its task's run too; a run waits for nothing once it has started."""
        for first in needs:
            seen, ahead = set(), [_node(first)]
            while ahead:
                if (at := ahead.pop()) == node:
                    raise ValueError(f"{'Task ' if node[0] == 'run' else 'The merge of task '}{node[1]} can't wait for "
                                     f"{on(first)}: that already waits, in turn, for {'it' if node[0] == 'run' else 'this merge'}, "
                                     "so neither would ever happen.")
                if at in seen or not (task := self.store.get(at[1])):
                    continue
                seen.add(at)
                if at[0] == "merge":
                    ahead += [("run", at[1]), *map(_node, waits(task, MERGE))]
                elif task["status"] in ("queued", "waiting"):
                    ahead += map(_node, waits(task))

    def _wait(self, task_id: int, needs: list[dict]) -> dict:
        """Save a new task to start once `needs` are met: at once if they already are."""
        pending = [d for d in needs if self._state(d) != MET]
        if not pending:
            self.store.log(task_id, "waits", f"was to start once {on_all(needs)}, which has already happened")
            return self._launch(task_id, self.store.get(task_id)["instruction"])
        self.store.log(task_id, "waits", f"starts by itself once {on_all(needs)}")
        return self.store.update(task_id, waits_on=json.dumps(needs), activity=self._waiting_for(pending))

    @staticmethod
    def _waiting_for(pending: list[dict]) -> str:
        return f"waiting for {joined([on(d) for d in pending])}, then it starts by itself"

    def waits_for(self, task: dict) -> dict:
        """What a task not yet started (`waits_for`), or its merge (`merge_waits_for`), still waits for, as listed."""
        out = {}
        if task.get("waits_on") and task["status"] in ("queued", "waiting"):
            out["waits_for"] = [on(d) for d in waits(task) if self._state(d) != MET]
        if task.get("merge_waits_on"):
            out["merge_waits_for"] = [on(d) for d in waits(task, MERGE) if self._state(d) != MET]
        return out

    def merge_later(self, task_id: int, after: list[int] | None = None, after_merge: list[int] | None = None) -> dict:
        """Merge a worktree task's branch by itself once it and the tasks in `after` have finished, and the ones in
        `after_merge` have been merged; replaces a merge already waiting. Said when it's done."""
        task = self._get(task_id)
        if task["status"] == "discarded":
            raise ValueError(f"Task {task_id} was discarded, so there's nothing to merge.")
        if task["kind"] != "code_change" or task["in_place"]:
            raise ValueError(f"Task {task_id} has no branch of its own to merge; in-place changes are already in the "
                             "project's folder.")
        if task_id in (after_merge or []):
            raise ValueError(f"Task {task_id}'s merge can't wait for itself.")
        needs = self._needed([task_id, *(i for i in after or [] if i != task_id)], after_merge)
        self._no_loop(("merge", task_id), needs)
        self.store.log(task_id, "merge waits", f"merges by itself once {on_all(needs)}")
        task = self.store.update(task_id, merge_waits_on=json.dumps(needs))
        self._settle_merge(task)
        return self.store.get(task_id)

    # ── When something ends ─────────────────────────────────────

    def _settle(self, ended: int) -> None:
        """Start, hold or call off what waits for task `ended`, which has just ended or been merged."""
        if not self.closing:  # else settle_all picks it up at the next start
            self.settle_all(ended)

    def settle_all(self, ended: int | None = None) -> None:
        """Settle what waits for task `ended`, or everything waiting: once at start, since what it waits for may have
        ended while the runner was down (a run cut off by the restart is stopped, say)."""
        for found in self.store.waiting():
            for field, settle in ((START, self._settle_start), (MERGE, self._settle_merge)):
                # Fresh: settling one can hold or call off another. Every merge is looked at again: one whose waits
                # were met may have been put off while a task worked in its project's folder (_merge_now)
                if ended is None or field == MERGE or any(d["task"] == ended for d in waits(found, field)):
                    settle(self.store.get(found["id"]))

    def _settle_start(self, task: dict) -> None:
        if not task["waits_on"] or task["status"] not in ("queued", "waiting"):
            return
        needs = waits(task)
        states = [self._state(d) for d in needs]
        try:
            if all(s == MET for s in states):
                self._begin(task)
            elif task["status"] == "queued":  # a held one stays held
                if bad := next((s for s in states if s not in (None, MET)), None):
                    self._hold(task, bad)
                else:
                    self.store.set(task["id"], activity=self._waiting_for(
                        [d for d, s in zip(needs, states) if s is None]))
        except Exception:  # never breaks the ending that settled it
            log.exception("couldn't settle task %s, which waits for other tasks", task["id"])

    def _begin(self, task: dict) -> None:
        """Start a task whose waits are all met, saying so briefly."""
        why = joined([f"task {d['task']} {'is merged' if d['merged'] else 'has finished'}" for d in waits(task)])
        said = f"Task {task['id']}, {task['title']}, starts now that {why}."
        self.store.log(task["id"], "waited", said)
        self._launch(task["id"], task["instruction"])
        self._emit("status", self.store.update(task["id"], spoken=said, seen=0), said)

    def _hold(self, task: dict, why: str) -> None:
        """Hold a task that can't start as planned, and ask whether to start it anyway."""
        after_it = [t["id"] for t in self.store.waiting()
                    if t["id"] != task["id"] and any(d["task"] == task["id"] for d in waits(t) + waits(t, MERGE))]
        also = f" Task {joined([str(i) for i in after_it])} waits for it in turn." if after_it else ""
        self.store.log(task["id"], "held", why)
        self._finish(task["id"], "waiting",
                     f"Task {task['id']}, {task['title']}, hasn't started: it was waiting for other work, but {why}."
                     f"{also} Shall I start it anyway, or stop it?",
                     question=f"It was to start once other work finished, but {why}. Start it anyway, or stop it?",
                     wait_kind=WAIT_AFTER)

    # ── Merges that wait ────────────────────────────────────────

    def _settle_merge(self, task: dict) -> None:
        if task["id"] in self.merging or not task["merge_waits_on"]:
            return
        states = [self._state(d) for d in waits(task, MERGE)]
        if all(s == MET for s in states):
            self.merging.add(task["id"])
            self._background(self._merge_now(task["id"]))
        elif bad := next((s for s in states if s not in (None, MET)), None):
            self._call_off(task, bad)

    async def _merge_now(self, task_id: int) -> None:
        """Run a waiting merge whose waits are met, and say how it went. While a task works in the project's folder
        it's put off, not called off: settle_all tries it again when a task ends."""
        if busy := self._jobs_on(self.store.get(task_id)["project"], in_place=True):
            self.merging.discard(task_id)
            self.store.log(task_id, "merge waits", f"put off while task {joined([str(i) for i in busy])} works in "
                                                   "the project's folder")
            return
        try:
            task = await self.merge(task_id)  # settles what waits for the merge, or holds it when the merge fails
            said, title = task["spoken"], f"Task {task_id} is merged"
        except asyncio.CancelledError:  # shutting down: left waiting, so settle_all merges it at the next start
            self.merging.discard(task_id)
            raise
        except Exception as e:
            task = self.store.get(task_id)
            said, title = f"Task {task_id}, {task['title']}, wasn't merged after all: {e}", f"Task {task_id} wasn't merged"
            self.store.log(task_id, "merge failed", said)
            # merge() holds what waits only when git itself fails; here a refusal before that counts too (no-op if done)
            self._merge_failed(task_id, f"task {task_id}'s merge failed: {e}")
        self.merging.discard(task_id)
        self.store.set(task_id, merge_waits_on=None)
        self._tell(task_id, said, title)

    def _call_off(self, task: dict, why: str) -> None:
        """Drop a waiting merge that can't happen as planned, and hold what waits for it in turn."""
        said = (f"Task {task['id']}, {task['title']}, won't be merged by itself: its merge was waiting for other work, "
                f"but {why}.")
        self.store.log(task["id"], "merge called off", said)
        self.store.set(task["id"], merge_waits_on=None)
        self._tell(task["id"], said, f"Task {task['id']} won't be merged")
        self._merge_failed(task["id"], f"task {task['id']}'s merge was called off")

    def _merge_failed(self, task_id: int, why: str) -> None:
        """A task's merge failed or was called off (`why`): hold the tasks, and call off the merges, waiting for it."""
        wait = {"task": task_id, "merged": True}
        for found in self.store.waiting():
            task = self.store.get(found["id"])  # fresh: a call-off may have held it already
            if wait in waits(task) and task["status"] == "queued":
                self._hold(task, why)
            if task["id"] != task_id and wait in waits(task, MERGE):
                self._call_off(task, why)

    def _tell(self, task_id: int, said: str, title: str) -> None:
        """Say something that happened by itself to a task (spoken in a session, the bell, a notification and push)."""
        self._emit("status", self.store.update(task_id, spoken=said, seen=0), said)
        if not hold.held():
            self._background(self._notify(f"JARVIS · task {task_id}", said))
            self._background(self._push(title, said, *task_target(task_id)))
