"""Shared base for the task runner's parts: common state, plus stubs for methods each part defines (module noted)."""

import asyncio
from typing import TYPE_CHECKING

from jarvis.core.database import Store
from jarvis.tasks import command_jobs

SHUTTING_DOWN = "JARVIS's task runner is shutting down; try again once it's back, in a moment."


class RunnerBase:
    """Shared runner state (documented in Runner.__init__) and helpers."""

    store: Store
    jobs: dict[int, asyncio.Task]
    folders: dict[str, asyncio.Lock]
    merges: dict[int, asyncio.Task]
    snapshots: dict[int, dict]
    projects_lock: asyncio.Lock
    listeners: set[asyncio.Queue]
    _bg: set[asyncio.Task]
    commands: command_jobs.Runs
    ending: dict[int, asyncio.Task]
    closing: bool
    placing: dict[int, asyncio.Task]
    limit_waits: dict[int, float]
    if TYPE_CHECKING:
        from jarvis.node.hub import Hub
        from jarvis.node.offload import Offload
        from jarvis.node.placement import Place
        offload: Offload
        hub: Hub

    if TYPE_CHECKING:
        # jarvis.tasks.runner
        def start(self, kind: str, title: str, instruction: str, project: str | None = None, job: int | None = None,
                  hidden: bool = False, grants: list[str] | None = None, order: str | None = None,
                  fixes: int | None = None, todo: str | None = None) -> dict: ...
        def _launch(self, task_id: int, prompt: str, after: float | None = None) -> dict: ...
        def _placed(self, task_id: int, place: "Place") -> None: ...
        # jarvis.tasks.approvals
        def _close_approvals(self, task_id: int, why: str = "stopped") -> None: ...
        # jarvis.tasks.events
        def _finish(self, task_id: int, status: str, spoken: str, quiet: bool = False, title: str = "",
                    alert: bool = True, **fields) -> None: ...
        async def _notify(self, title: str, text: str) -> None: ...
        async def _push(self, title: str, text: str, url: str, tag: str, **extra) -> None: ...
        def broadcast(self, event: dict) -> None: ...

    def _get(self, task_id: int) -> dict:
        if not (task := self.store.get(task_id)):
            raise ValueError(f"There's no task {task_id}.")
        return task

    def _open(self) -> None:
        """Raise RuntimeError once shutdown has begun."""
        if self.closing:
            raise RuntimeError(SHUTTING_DOWN)

    def _background(self, coro) -> asyncio.Task:
        t = asyncio.create_task(coro)
        self._bg.add(t)
        t.add_done_callback(self._bg.discard)
        return t
