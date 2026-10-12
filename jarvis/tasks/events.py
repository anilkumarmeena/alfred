"""Runner mixin for telling the user about tasks: GET /events (voice), the panel bell, desktop notifications and pushes.

`_finish` records a run's end and announces it through all of these.
"""

import logging

from ai_armory.toolsets.mac import notify as mac_notify

from jarvis.core import desktop, platform
from jarvis.core.config import PUSH_ENABLED
from jarvis.core.database import APPROVED, DENIED
from jarvis.notify.push import send_push, task_target
from jarvis.state import activity as bell
from jarvis.tasks import hold
from jarvis.tasks.approvals import _VIA, SETTLED, _commands
from jarvis.tasks.runner_base import RunnerBase

log = logging.getLogger("jarvis.tasks.runner")
# Statuses that alert the user, with their push title
PUSH_TITLES = {"done": "Task {id} is done", "waiting": "Task {id} has a question", "failed": "Task {id} failed"}
STATUS_LIGHT = {"done": "ok", "failed": "bad", "waiting": "warn", "stopped": "warn"}


def bell_entry(event: dict) -> tuple[str, str, int | None] | None:
    """(text, light, task id) for the panel bell, or None for events it skips."""
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
        case "alert":
            return event["text"], "warn", None
    return None


class Events(RunnerBase):
    """Ending tasks and reaching the user."""

    def _finish(self, task_id: int, status: str, spoken: str, quiet: bool = False, title: str = "", alert: bool = True,
                **fields) -> None:
        """Record a run's end and announce it unless `quiet`.

        `title` replaces the task number in the push (e.g. a scheduled job). `alert=False` or do-not-disturb skips the
        desktop notification and push; it's still spoken in a live session or at the next greeting."""
        if self.offload.while_away(task_id, status, spoken, quiet):  # said in the machine's one summary instead
            quiet, fields["seen"] = True, 1
        fields.setdefault("seen", 0 if status in PUSH_TITLES else 1)
        if status != "waiting":  # orders apply only to the work just finished
            fields["orders"] = None
            if node := (self.store.get(task_id) or {}).get("node"):  # always say where it ran
                spoken = f"{spoken} It ran on {self.offload.name(node)}."
        task = self.store.update(task_id, status=status, spoken=spoken, activity=None, **fields)
        self.store.log(task_id, status, spoken)
        if status != "waiting":  # only an answered question resumes from the same snapshot
            self.snapshots.pop(task_id, None)
            self._close_approvals(task_id, status)
        if quiet:
            return
        self._emit("status", task, spoken)
        if status in PUSH_TITLES and alert and not hold.held():
            self._background(self._notify(f"JARVIS · {title or f'task {task_id}'}", spoken))
            self._background(self._push(title or PUSH_TITLES[status].format(id=task_id), spoken, *task_target(task_id)))

    async def _notify(self, title: str, text: str) -> None:
        """A Mac notification, or notify-send's on a Linux desktop; skipped where there's neither."""
        try:
            if platform.MAC:
                await mac_notify(title, text)
            elif desktop.can_notify():
                await desktop.notify(title, text)
        except Exception as e:
            log.warning("notification failed: %s", e)

    async def _push(self, title: str, text: str, url: str, tag: str, **extra) -> None:
        if not PUSH_ENABLED:
            return
        try:
            await send_push(title, text, url, tag, **extra)
        except Exception as e:  # log the type only: no keys or endpoints
            log.warning("push failed: %s", type(e).__name__)

    def _emit(self, kind: str, task: dict, text: str) -> None:
        self.broadcast({"type": kind, "text": text, "task": {k: v for k, v in task.items() if k not in ("instruction", "summary")}})

    def broadcast(self, event: dict) -> None:
        if entry := bell_entry(event):
            bell.add(*entry)
        for q in self.listeners:
            q.put_nowait(event)
