"""The task runner as other processes reach it over its socket (jarvis.tasks.client), named here so parts below the
tasks layer can take a runner without importing it."""

from typing import Any, Protocol


class RunnerUnavailable(RuntimeError):
    """The task runner isn't answering on its socket."""


class ConnectionRunner(Protocol):
    """What a connection's relay (jarvis.integrations.mcp_relay) needs to carry a slow call on as a task."""

    async def start_connection(self, title: str, instruction: str, pid: int) -> dict[str, Any]: ...

    async def task(self, task_id: int) -> dict[str, Any]: ...

    async def connection_update(self, task_id: int, said: str) -> Any: ...

    async def end_connection(self, task_id: int, outcome: str, result: str) -> Any: ...
