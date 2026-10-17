"""Work on the user's other machines, as the task runner sees it. jarvis.node implements it (the links to the machines,
sending them work, the paired-machine list); the runner is handed one at start (jarvis.apps.runner), and runs
everything here without one (jarvis.tasks.local)."""

from dataclasses import dataclass
from typing import Any, Protocol

MAX_MESSAGE = 8 * 1024 * 1024  # bytes per message on a machine's link; files go in chunks well under it
COMMANDS, TASKS = "commands", "tasks"  # the job kinds the user places
HERE_WORD = "here"  # what "this computer" is called where a machine is named


@dataclass(frozen=True)
class Place:
    """Where a job runs: a machine's node id, or None for this computer."""
    node: int | None = None
    name: str = "this computer"
    said: str = ""  # why it isn't where the user would expect, said with the start; "" when there's nothing to add


HERE = Place()


class Offline(RuntimeError):
    """The task's machine didn't come back in time."""


class TurnedDown(RuntimeError):
    """The machine's own check refused the command: it runs nowhere."""


class Machines(Protocol):
    """The paired machines."""

    def paired(self) -> list[Any]: ...

    def find(self, name: str) -> Any: ...  # ValueError naming the ones there are


class Links(Protocol):
    """The links to the machines."""

    def connected(self, node: int) -> Any | None: ...

    async def accept_websocket(self, ws: Any) -> None: ...  # a machine linking in through the runner's socket

    async def listing(self) -> Any: ...

    async def remove(self, node: int) -> Any: ...

    async def close(self) -> None: ...


class Offload(Protocol):
    """Sending tasks and commands to the machines and following them."""

    @staticmethod
    def key(task_id: int) -> str: ...

    @staticmethod
    def name(node: int | None) -> str: ...

    def excuse(self, task: dict, now: float) -> str: ...

    def resume_following(self) -> None: ...

    def while_away(self, task_id: int, status: str, said: str, quiet: bool = False) -> bool: ...

    def lost(self, now: float | None = None) -> list[dict]: ...  # machines with work, unreachable too long

    def owner(self, job: dict) -> Any | None: ...  # the project a scheduled job's command runs in, if on a machine

    async def sync_schedules(self) -> None: ...  # send each machine its jobs

    async def folder_op(self, node: int, op: str, body: dict) -> dict: ...

    async def register_there(self, name: str, path: str | None, aliases: list[str] | None, about: str,
                             in_place: bool, machine: str) -> dict: ...

    async def place(self, kind: str, job: dict, asked: str = "") -> Place: ...

    async def forget(self, node: int, key: str) -> None: ...

    async def start_command(self, task: dict, node: int, project: Any, folder: Any, approved: str) -> None: ...

    async def stop_command(self, task: dict) -> None: ...

    async def run_task(self, task: dict, prompt: str, reattach: bool = False) -> Any: ...

    async def reachable(self, task: dict) -> Any: ...
