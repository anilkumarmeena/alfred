"""The task runner on its own, with no other machines wired in (jarvis.contracts.remote): no machine is paired,
everything runs on this computer, and a machine linking in is turned away."""

from typing import Any

from jarvis.contracts.remote import HERE, Offline, Place


class NoMachines:
    def paired(self) -> list[Any]:
        return []

    def find(self, name: str) -> Any:
        raise ValueError(f"There's no machine called {name}: no other machine is paired.")


class NoLinks:
    def connected(self, node: int) -> Any | None:
        return None

    async def accept_websocket(self, ws: Any) -> None:
        await ws.close()

    async def listing(self) -> dict:
        return {"machines": [], "placement": {}, "kinds": {}, "commit": "", "fingerprint": ""}

    async def remove(self, node: int) -> Any:
        raise ValueError(f"There's no machine {node}.")

    async def close(self) -> None:
        pass


class NoOffload:
    @staticmethod
    def key(task_id: int) -> str:
        return f"task-{task_id}"

    @staticmethod
    def name(node: int | None) -> str:
        return HERE.name if node is None else f"machine {node}"

    def excuse(self, task: dict, now: float) -> str:
        return ""

    def resume_following(self) -> None:
        pass

    def while_away(self, task_id: int, status: str, said: str, quiet: bool = False) -> bool:
        return False

    def lost(self, now: float | None = None) -> list[dict]:
        return []

    def owner(self, job: dict) -> Any | None:
        return None

    async def sync_schedules(self) -> None:
        pass

    async def folder_op(self, node: int, op: str, body: dict) -> dict:
        raise ValueError("That project lives on another machine, and no other machine is wired in.")

    async def register_there(self, name: str, path: str | None, aliases: list[str] | None, about: str,
                             in_place: bool, machine: str) -> dict:
        raise ValueError(f"There's no machine called {machine}: no other machine is paired.")

    async def place(self, kind: str, job: dict, asked: str = "") -> Place:
        return HERE

    async def forget(self, node: int, key: str) -> None:
        pass

    async def start_command(self, task: dict, node: int, project: Any, folder: Any, approved: str) -> None:
        raise Offline("No other machine is wired in.")

    async def stop_command(self, task: dict) -> None:
        pass

    async def run_task(self, task: dict, prompt: str, reattach: bool = False) -> Any:
        raise Offline("No other machine is wired in.")

    async def reachable(self, task: dict) -> Any:
        raise Offline("No other machine is wired in.")
