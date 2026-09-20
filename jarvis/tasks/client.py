"""Client for the task runner's API on its Unix socket (see jarvis.tasks.runner)."""

import json
from collections.abc import AsyncIterator

import aiohttp

from jarvis.core.config import RUNNER_SOCKET
from jarvis.tasks import merge_check


class RunnerUnavailable(RuntimeError):
    pass


class NotAllowed(ValueError):
    """The runner refused a request for who was asking (403): a wrong nonce."""


def _session(timeout: float | None) -> aiohttp.ClientSession:
    return aiohttp.ClientSession(
        connector=aiohttp.UnixConnector(path=str(RUNNER_SOCKET)),
        timeout=aiohttp.ClientTimeout(total=timeout),
    )


DOWN = "The task runner isn't running. Start it with `make services-install`, or `make runner`."


async def call(method: str, path: str, body: dict | None = None, timeout: float = 30):
    try:
        async with _session(timeout) as s, s.request(method, f"http://runner{path}", json=body) as r:
            data = await r.json()
    except aiohttp.ClientConnectionError as e:
        raise RunnerUnavailable(DOWN) from e
    if r.status >= 400:
        raise (NotAllowed if r.status == 403 else ValueError)(data.get("error") or f"runner answered {r.status}")
    return data


async def start(kind: str, title: str, instruction: str, project: str | None = None,
                access: list[str] | None = None, order: str = "", todo: str = "") -> dict:
    """A new task, granted `access` beyond its kind's (jarvis.tasks.access). `order` is the user's own words, when
    they order what the task may then do without asking ("and push it": jarvis.safety.commands.ordered). `todo` is the
    open to-do item it works on, ticked off when it's finished (jarvis.tasks.goal)."""
    return await call("POST", "/tasks", {"kind": kind, "title": title, "instruction": instruction, "project": project,
                                         "access": access or []} | ({"order": order} if order else {})
                      | ({"todo": todo} if todo else {}))


async def start_command(command: str, title: str, folder: str, timeout_minutes: int | None = None,
                        summarize: bool = True, auto_fix: bool = True, report: bool = False) -> dict:
    """A new command task (jarvis.tasks.command_jobs): `command` run in `folder`, a full path, with no model while it
    works. As it ends, a success is summed up (`summarize`) or, for one that `report`s, said with its last line; a
    failure is looked into, and fixed in its project if `auto_fix` lets it."""
    return await call("POST", "/commands", {"command": command, "title": title, "folder": folder,
                                            "timeout_minutes": timeout_minutes, "summarize": summarize,
                                            "auto_fix": auto_fix, "report": report})


async def start_connection(title: str, instruction: str, pid: int) -> dict:
    """A call on a connection still running after the voice brain's wait, as a connection task
    (jarvis.tasks.connection_jobs) that the relay making it, process `pid`, reports to."""
    return await call("POST", "/connections", {"title": title, "instruction": instruction, "pid": pid})


async def connection_update(task_id: int, activity: str) -> dict:
    """What a connection task's call last said of how it's going."""
    return await call("POST", f"/connections/{int(task_id)}", {"activity": activity})


async def end_connection(task_id: int, outcome: str, result: str) -> dict:
    """A connection task's call has ended, "done" or "failed", with what it returned."""
    return await call("POST", f"/connections/{int(task_id)}", {"outcome": outcome, "result": result})


async def rerun(task_id: int) -> dict:
    """Run a finished command task's command again, as a command task of its own."""
    return await call("POST", f"/tasks/{int(task_id)}/rerun")


async def tasks(unseen: bool = False) -> list[dict]:
    """Every active task plus any that changed in the last day; or only those the user hasn't heard about."""
    return await call("GET", "/tasks?unseen=1" if unseen else "/tasks")


async def runs(job_id: int) -> list[dict]:
    """A scheduled job's latest runs (jarvis.tasks.schedules), newest first, silent ones included."""
    return await call("GET", f"/tasks?job={int(job_id)}")


async def task(task_id: int) -> dict:
    """One task, with its log."""
    return await call("GET", f"/tasks/{int(task_id)}")


async def log(task_id: int, lines: int | None = None) -> dict:
    """One task, with the latest `lines` of its log (the runner's default if None), every step included."""
    return await call("GET", f"/tasks/{int(task_id)}/log" + (f"?lines={int(lines)}" if lines else ""))


async def reply(task_id: int, message: str, access: list[str] | None = None, order: str = "") -> dict:
    """A message for the task, granting it `access` too. `order` is the user's own words, when they order what the
    task may then do without asking ("merge and push")."""
    return await call("POST", f"/tasks/{int(task_id)}/reply", {"message": message, "access": access or []}
                      | ({"order": order} if order else {}))


async def stop(task_id: int) -> dict:
    return await call("POST", f"/tasks/{int(task_id)}/stop")


async def merge(task_id: int) -> dict:
    # JARVIS's own branches run its tests first (jarvis.tasks.merge_check): twice over at worst
    return await call("POST", f"/tasks/{int(task_id)}/merge", timeout=2 * merge_check.TIMEOUT + 360)


async def discard(task_id: int) -> dict:
    return await call("POST", f"/tasks/{int(task_id)}/discard", timeout=180)


async def edits(task_id: int, action: str, numbers: list[int]) -> dict:
    """Apply or dismiss edits to JARVIS's data files that the task suggested, by number."""
    return await call("POST", f"/tasks/{int(task_id)}/edits", {"action": action, "edits": numbers})


async def approvals() -> list[dict]:
    """Tasks' requests to run risky commands that are still waiting for an answer, oldest first."""
    return await call("GET", "/approvals")


async def answer(approval_id: int, decision: str, via: str, nonce: str | None = None, task: int | None = None,
                 commands: list[dict] | None = None, said: str | None = None, remember: str = "once",
                 breadth: str = "exact", whole_task: bool = False) -> dict:
    """Approve or deny a request ("approve" or "deny"), saying how the answer came (push, web, voice, cli,
    assistant). One from the phone carries the nonce its push had; one through the assistant, the request's
    task, its exact commands and the user's words. A yes can be remembered for the task's session or always
    (jarvis.safety.remembered), once the user has been shown what that covers. `whole_task`: the answer goes for the
    task's other requests waiting too, which come back as `also`."""
    body = {"decision": decision, "via": via} | ({"nonce": nonce} if nonce is not None else {})
    if via == "assistant":
        body |= {"task": task, "commands": commands, "said": said}
    if remember != "once":
        body |= {"remember": remember, "breadth": breadth}
    if whole_task:
        body |= {"whole_task": True}
    return await call("POST", f"/approvals/{int(approval_id)}", body)


async def remember(approval_id: int, breadth: str, via: str, scope: str = "always") -> dict:
    """Remember an approved request's commands as rules, once the user has heard what they cover and said yes:
    {"rules": [...]}, or {"rules": [], "never": why} for commands that always ask."""
    return await call("POST", f"/approvals/{int(approval_id)}/remember", {"breadth": breadth, "via": via, "scope": scope})


async def mark_seen(ids: list[int]) -> None:
    """The user has heard these tasks' latest updates."""
    await call("POST", "/tasks/seen", {"ids": ids})


async def register_project(name: str, path: str | None, aliases: list[str], about: str, in_place: bool) -> dict:
    return await call("POST", "/projects", {"name": name, "path": path, "aliases": aliases, "about": about,
                                            "in_place": in_place})


async def git_init(name: str) -> dict:
    return await call("POST", "/projects/git", {"name": name}, timeout=180)


async def session_ended() -> None:
    """A voice session has ended: the learning review (jarvis.tasks.learning) reads it shortly."""
    await call("POST", "/learn", timeout=5)


async def events() -> AsyncIterator[dict]:
    """Task events as they happen, until the connection drops."""
    try:
        async with _session(None) as s, s.get("http://runner/events") as r:
            async for line in r.content:
                if line.strip():
                    yield json.loads(line)
    except (aiohttp.ClientConnectionError, aiohttp.ClientPayloadError) as e:  # payload: the runner stopped mid-stream
        raise RunnerUnavailable(DOWN) from e
