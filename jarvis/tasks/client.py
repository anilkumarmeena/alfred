"""Client for the task runner's Unix-socket API."""

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import aiohttp

from jarvis.core.config import RUNNER_SOCKET
from jarvis.node.link import MAX_MESSAGE
from jarvis.tasks import merge_check


class RunnerUnavailable(RuntimeError):
    pass


class NotAllowed(ValueError):
    """Runner answered 403: nonce or assistant details didn't match."""


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
                access: list[str] | None = None, order: str = "", todo: str = "", machine: str = "",
                after: list[int] | None = None, after_merge: list[int] | None = None) -> dict:
    """Start a task with extra `access`.

    `order`: the user's words authorising risky actions without asking ("and push it"). `todo`: linked to-do id.
    `machine`: where the user said it runs ("here" or a machine's name). The reply's `where` says where it runs, when
    that's worth saying. `after` / `after_merge`: tasks to finish / be merged first; it waits for them, then starts
    by itself."""
    return await call("POST", "/tasks", {"kind": kind, "title": title, "instruction": instruction, "project": project,
                                         "access": access or []} | ({"order": order} if order else {})
                      | ({"todo": todo} if todo else {}) | ({"machine": machine} if machine else {})
                      | ({"after": after} if after else {}) | ({"after_merge": after_merge} if after_merge else {}),
                      timeout=60)


async def start_command(command: str, title: str, folder: str, timeout_minutes: int | None = None,
                        summarize: bool = True, auto_fix: bool = True, report: bool = False, placeable: bool = False,
                        machine: str = "", approved: str = "") -> dict:
    """Start a command task: run `command` in absolute `folder` with no model while it runs.

    On success: summarised (`summarize`) or its last line said (`report`). On failure: diagnosed, fixed if `auto_fix`.
    `placeable`: it may run on another machine (`machine` where the user said); `approved`: how a risky one got the
    user's yes."""
    return await call("POST", "/commands", {"command": command, "title": title, "folder": folder,
                                            "timeout_minutes": timeout_minutes, "summarize": summarize,
                                            "auto_fix": auto_fix, "report": report, "placeable": placeable,
                                            "machine": machine, "approved": approved}, timeout=60)


async def start_connection(title: str, instruction: str, pid: int) -> dict:
    """Track a slow connection call as a task; the relay process `pid` reports to it."""
    return await call("POST", "/connections", {"title": title, "instruction": instruction, "pid": pid})


async def connection_update(task_id: int, activity: str) -> dict:
    """Report a connection task's latest progress."""
    return await call("POST", f"/connections/{int(task_id)}", {"activity": activity})


async def end_connection(task_id: int, outcome: str, result: str) -> dict:
    """End a connection task: `outcome` is "done" or "failed"."""
    return await call("POST", f"/connections/{int(task_id)}", {"outcome": outcome, "result": result})


async def rerun(task_id: int) -> dict:
    """Rerun a finished command task as a new command task."""
    return await call("POST", f"/tasks/{int(task_id)}/rerun")


async def tasks(unseen: bool = False) -> list[dict]:
    """Active tasks plus those changed in the last day; or only unseen ones."""
    return await call("GET", "/tasks?unseen=1" if unseen else "/tasks")


async def runs(job_id: int) -> list[dict]:
    """A scheduled job's latest runs, newest first, silent ones included."""
    return await call("GET", f"/tasks?job={int(job_id)}")


async def task(task_id: int) -> dict:
    """One task, with its log."""
    return await call("GET", f"/tasks/{int(task_id)}")


async def log(task_id: int, lines: int | None = None) -> dict:
    """One task with the last `lines` log entries (runner default if None), steps included."""
    return await call("GET", f"/tasks/{int(task_id)}/log" + (f"?lines={int(lines)}" if lines else ""))


async def reply(task_id: int, message: str, access: list[str] | None = None, order: str = "") -> dict:
    """Send the task a message, optionally granting `access`; `order` as in `start`."""
    return await call("POST", f"/tasks/{int(task_id)}/reply", {"message": message, "access": access or []}
                      | ({"order": order} if order else {}))


async def stop(task_id: int) -> dict:
    return await call("POST", f"/tasks/{int(task_id)}/stop")


async def merge(task_id: int, after: list[int] | None = None, after_merge: list[int] | None = None) -> dict:
    """Merge now, or, with `after` / `after_merge` (tasks to finish / be merged first), by itself once they are."""
    if after or after_merge:
        return await call("POST", f"/tasks/{int(task_id)}/merge", {"after": after or [], "after_merge": after_merge or []})
    # JARVIS's own branches run its tests first, up to twice
    return await call("POST", f"/tasks/{int(task_id)}/merge", timeout=2 * merge_check.TIMEOUT + 360)


async def discard(task_id: int) -> dict:
    return await call("POST", f"/tasks/{int(task_id)}/discard", timeout=180)


async def edits(task_id: int, action: str, numbers: list[int]) -> dict:
    """Apply or dismiss the task's suggested data edits, by number."""
    return await call("POST", f"/tasks/{int(task_id)}/edits", {"action": action, "edits": numbers})


async def approvals() -> list[dict]:
    """Pending approval requests, oldest first."""
    return await call("GET", "/approvals")


async def answer(approval_id: int, decision: str, via: str, nonce: str | None = None, task: int | None = None,
                 commands: list[dict] | None = None, said: str | None = None, remember: str = "once",
                 breadth: str = "exact", whole_task: bool = False) -> dict:
    """Answer an approval request ("approve"/"deny") via push, web, voice, cli or assistant.

    Phone answers need the push's nonce; assistant answers need task, exact commands and the user's words.
    `remember` (session/always) only after showing the user what it covers. `whole_task` also answers the task's
    other pending requests, returned in `also`."""
    body = {"decision": decision, "via": via} | ({"nonce": nonce} if nonce is not None else {})
    if via == "assistant":
        body |= {"task": task, "commands": commands, "said": said}
    if remember != "once":
        body |= {"remember": remember, "breadth": breadth}
    if whole_task:
        body |= {"whole_task": True}
    return await call("POST", f"/approvals/{int(approval_id)}", body)


async def remember(approval_id: int, breadth: str, via: str, scope: str = "always") -> dict:
    """Save rules from an approved request, after the user agreed to what they cover.

    Returns {"rules": [...]}, or {"rules": [], "never": why} for always-ask commands."""
    return await call("POST", f"/approvals/{int(approval_id)}/remember", {"breadth": breadth, "via": via, "scope": scope})


async def mark_seen(ids: list[int]) -> None:
    """Mark these tasks' latest updates as heard."""
    await call("POST", "/tasks/seen", {"ids": ids})


async def register_project(name: str, path: str | None, aliases: list[str], about: str, in_place: bool,
                           machine: str = "") -> dict:
    """Add a project; with `machine`, one that lives on that machine (its folder checked there)."""
    return await call("POST", "/projects", {"name": name, "path": path, "aliases": aliases, "about": about,
                                            "in_place": in_place} | ({"machine": machine} if machine else {}),
                      timeout=60)


async def git_init(name: str) -> dict:
    return await call("POST", "/projects/git", {"name": name}, timeout=180)


async def session_ended() -> None:
    """Signal a voice session ended so the learning review picks it up."""
    await call("POST", "/learn", timeout=5)


async def machines() -> dict:
    """Paired machines, each with a live online check, plus where each kind of job runs."""
    return await call("GET", "/machines", timeout=20)


async def machine_remove(node: int) -> dict:
    """Forget a machine, dropping its link."""
    return await call("POST", f"/machines/{int(node)}/remove")


@asynccontextmanager
async def node_link() -> AsyncIterator[aiohttp.ClientWebSocketResponse]:
    """A WebSocket to the runner's machine link, for the web server to pass a machine's connection on to."""
    try:
        async with _session(None) as s, s.ws_connect("http://runner/node", max_msg_size=MAX_MESSAGE) as ws:
            yield ws
    except aiohttp.ClientConnectionError as e:
        raise RunnerUnavailable(DOWN) from e


async def events() -> AsyncIterator[dict]:
    """Stream task events; RunnerUnavailable if the connection fails."""
    try:
        async with _session(None) as s, s.get("http://runner/events") as r:
            async for line in r.content:
                if line.strip():
                    yield json.loads(line)
    except (aiohttp.ClientConnectionError, aiohttp.ClientPayloadError) as e:  # payload error: runner stopped mid-stream
        raise RunnerUnavailable(DOWN) from e
