"""How the task runner (jarvis.tasks.runner) tells the user about its tasks: events on GET /events, which the voice
agent speaks, the control panel's bell (jarvis.state.activity), a Mac notification, and a push to the phone when
JARVIS_PUSH is on (jarvis.notify.push); and a run's end (`_finish`), announced those ways.
"""

import logging

from ai_armory.toolsets.mac import notify as mac_notify

from jarvis.core.config import PUSH_ENABLED
from jarvis.core.database import APPROVED, DENIED
from jarvis.notify.push import send_push, task_target
from jarvis.state import activity as bell
from jarvis.tasks import hold
from jarvis.tasks.approvals import _VIA, SETTLED, _commands
from jarvis.tasks.runner_base import RunnerBase

log = logging.getLogger("jarvis.tasks.runner")
# The statuses worth interrupting the user for, with the phone notification's title for each
PUSH_TITLES = {"done": "Task {id} is done", "waiting": "Task {id} has a question", "failed": "Task {id} failed"}
STATUS_LIGHT = {"done": "ok", "failed": "bad", "waiting": "warn", "stopped": "warn"}


def bell_entry(event: dict) -> tuple[str, str, int | None] | None:
    """What one of the runner's events says in the control panel's bell (jarvis.state.activity): its text, its light
    and its task; None for events it leaves out."""
    match event["type"]:
        case "status":
            return event["text"], STATUS_LIGHT.get(event["task"]["status"], "run"), event["task"]["id"]
        case "approval":
            a = event["approval"]
            return f"Task {a['task']} asks to run {_commands(a)}", "warn", a["task"]
        case "approval_answered":
            a = event["approval"]
            (word, light), via = SETTLED[a["state"]], _VIA.get(a.get("via") or "", "")
            if a["state"] in (APPROVED, DENIED):
                said = f"{word.capitalize()} {_commands(a)} for task {a['task']}" + (f", {via}" if via else "")
            else:
                said = f"Task {a['task']}'s request to run {_commands(a)}: {word}"
            return said, light, a["task"]
        case "heartbeat":
            return event["text"], "warn", None
    return None


class Events(RunnerBase):
    """A task's end, announced, and the ways the runner reaches the user."""

    def _finish(self, task_id: int, status: str, spoken: str, quiet: bool = False, title: str = "", alert: bool = True,
                **fields) -> None:
        """A run's end, announced unless `quiet` (a wait for requests the user has already been asked, or a scheduled
        run with nothing to say). `title` heads its push instead of the task's number (a scheduled job's). Without
        `alert` it's only said in a live session, or at the next one's greeting: no Mac notification or push. Do not
        disturb (jarvis.tasks.hold) takes the alert away too."""
        fields.setdefault("seen", 0 if status in PUSH_TITLES else 1)
        if status != "waiting":  # what the user's words ordered was for the work they sent it on, now over
            fields["orders"] = None
        task = self.store.update(task_id, status=status, spoken=spoken, activity=None, **fields)
        self.store.log(task_id, status, spoken)
        if status != "waiting":  # an answer carries on from the same snapshot; anything else starts afresh
            self.snapshots.pop(task_id, None)
            self._close_approvals(task_id, status)
        if quiet:
            return
        self._emit("status", task, spoken)
        if status in PUSH_TITLES and alert and not hold.held():
            self._background(self._notify(f"JARVIS · {title or f'task {task_id}'}", spoken))
            self._background(self._push(title or PUSH_TITLES[status].format(id=task_id), spoken, *task_target(task_id)))

    async def _notify(self, title: str, text: str) -> None:
        try:
            await mac_notify(title, text)
        except Exception as e:
            log.warning("notification failed: %s", e)

    async def _push(self, title: str, text: str, url: str, tag: str, **extra) -> None:
        if not PUSH_ENABLED:
            return
        try:
            await send_push(title, text, url, tag, **extra)
        except Exception as e:  # the type alone: nothing about keys or endpoints reaches the log
            log.warning("push failed: %s", type(e).__name__)

    def _emit(self, kind: str, task: dict, text: str) -> None:
        self.broadcast({"type": kind, "text": text, "task": {k: v for k, v in task.items() if k not in ("instruction", "summary")}})

    def broadcast(self, event: dict) -> None:
        if entry := bell_entry(event):
            bell.add(*entry)
        for q in self.listeners:
            q.put_nowait(event)
