"""Task runner HTTP API on its Unix socket; each route calls the Runner. Client: jarvis.tasks.client."""

import asyncio
import json
from typing import TYPE_CHECKING

from aiohttp import web

from jarvis.node.link import MAX_MESSAGE, WebSocketChannel
from jarvis.safety import remembered
from jarvis.tasks import projects
from jarvis.tasks.approvals import NotAllowed
from jarvis.tasks.runner_base import SHUTTING_DOWN

if TYPE_CHECKING:
    from jarvis.tasks.runner import Runner

# GET /tasks/ID/log: default and max lines
LOG_LINES, MAX_LOG_LINES = 12, 40


@web.middleware
async def errors(request: web.Request, handler):
    try:
        return await handler(request)
    except NotAllowed as e:
        return web.json_response({"error": str(e)}, status=403)
    except (ValueError, KeyError, TypeError, RuntimeError, OSError) as e:
        return web.json_response({"error": str(e)}, status=400)


def routes(r: "Runner") -> list[web.RouteDef]:
    async def list_tasks(req: web.Request) -> web.Response:
        if job := req.query.get("job"):
            return web.json_response([r.view(t) for t in r.store.runs(int(job))])
        return web.json_response([r.view(t) for t in (r.store.unseen() if req.query.get("unseen") else r.store.recent())])

    async def show(req: web.Request) -> web.Response:
        task = r.view(r._get(int(req.match_info["id"])))
        return web.json_response(task | {"log": r.store.history(task["id"])})

    async def tail(req: web.Request) -> web.Response:
        task = r.view(r._get(int(req.match_info["id"])))
        lines = min(max(int(req.query.get("lines", LOG_LINES)), 1), MAX_LOG_LINES)
        return web.json_response(task | {"log": r.store.history(task["id"], lines, steps=True)})

    async def create(req: web.Request) -> web.Response:
        b = await req.json()
        task = r.start(b["kind"], b.get("title", ""), b["instruction"], b.get("project"), grants=b.get("access"),
                       order=b.get("order"), todo=b.get("todo"), machine=str(b.get("machine") or ""))
        return web.json_response(await r.where(task["id"]))

    async def reply(req: web.Request) -> web.Response:
        b = await req.json()
        return web.json_response(r.reply(int(req.match_info["id"]), b["message"], b.get("access"), b.get("order")))

    async def stop(req: web.Request) -> web.Response:
        return web.json_response(r.view(await r.halt(int(req.match_info["id"]))))

    async def start_command(req: web.Request) -> web.Response:
        b = await req.json()
        timeout = b.get("timeout_minutes")
        task = r.start_command(
            str(b["command"]), str(b.get("title", "")), str(b["folder"]),
            timeout_minutes=int(timeout) if timeout else None, summarize=bool(b.get("summarize", True)),
            auto_fix=bool(b.get("auto_fix", True)), report=bool(b.get("report", False)),
            placeable=bool(b.get("placeable")), machine=str(b.get("machine") or ""), approved=str(b.get("approved") or ""))
        return web.json_response(await r.where(task["id"]))

    async def start_connection(req: web.Request) -> web.Response:
        b = await req.json()
        return web.json_response(r.start_connection(str(b.get("title", "")), str(b["instruction"]), int(b["pid"])))

    async def connection(req: web.Request) -> web.Response:
        b, task_id = await req.json(), int(req.match_info["id"])
        if "outcome" in b:
            return web.json_response(await r.end_connection(task_id, str(b["outcome"]), str(b.get("result", ""))))
        return web.json_response(r.connection_update(task_id, str(b.get("activity", ""))))

    async def rerun(req: web.Request) -> web.Response:
        return web.json_response(r.view(r.rerun(int(req.match_info["id"]))))

    async def seen(req: web.Request) -> web.Response:
        r.store.mark_seen([int(i) for i in (await req.json())["ids"]])
        return web.json_response({"ok": True})

    async def register_project(req: web.Request) -> web.Response:
        b = await req.json()
        return web.json_response(await r.register_project(
            b["name"], b.get("path") or None, b.get("aliases") or [], b.get("about", ""),
            b.get("in_place", projects.IN_PLACE_DEFAULT), str(b.get("machine") or "")))

    async def merge(req: web.Request) -> web.Response:
        return web.json_response(await r.merge(int(req.match_info["id"])))

    async def discard(req: web.Request) -> web.Response:
        return web.json_response(await r.discard(int(req.match_info["id"])))

    async def change_edits(req: web.Request) -> web.Response:
        b = await req.json()
        return web.json_response(await r.edits(int(req.match_info["id"]), b["action"], [int(n) for n in b["edits"]]))

    async def git_init(req: web.Request) -> web.Response:
        return web.json_response(await r.init_git((await req.json())["name"]))

    async def list_approvals(req: web.Request) -> web.Response:
        return web.json_response(r.approvals())

    async def answer(req: web.Request) -> web.Response:
        b = await req.json()
        nonce = b.get("nonce")
        return web.json_response(r.answer(int(req.match_info["id"]), b["decision"], b.get("via", "cli"),
                                          None if nonce is None else str(nonce), b.get("task"), b.get("commands"),
                                          b.get("said"), b.get("remember", "once"), b.get("breadth", "exact"),
                                          bool(b.get("whole_task"))))

    async def remember(req: web.Request) -> web.Response:
        b = await req.json()
        try:
            kept = r.remember(int(req.match_info["id"]), b.get("breadth", "exact"), b.get("via", "cli"),
                              b.get("scope", "always"))
        except remembered.NotRememberable as e:
            return web.json_response({"rules": [], "never": str(e)})
        return web.json_response({"rules": kept})

    async def session_ended(req: web.Request) -> web.Response:
        if r.learning:
            r.learning.ended()
        return web.json_response({"ok": True, "learning": r.learning is not None})

    async def events(req: web.Request) -> web.StreamResponse:
        resp = web.StreamResponse(headers={"Content-Type": "application/x-ndjson"})
        await resp.prepare(req)
        q: asyncio.Queue = asyncio.Queue()
        r.listeners.add(q)
        try:
            while True:
                await resp.write(json.dumps(await q.get()).encode() + b"\n")
        finally:
            r.listeners.discard(q)

    async def node_link(req: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(max_msg_size=MAX_MESSAGE)
        await ws.prepare(req)
        await r.hub.accept(WebSocketChannel(ws))
        return ws

    async def list_machines(req: web.Request) -> web.Response:
        return web.json_response(await r.hub.listing())

    async def remove_machine(req: web.Request) -> web.Response:
        return web.json_response(await r.hub.remove(int(req.match_info["id"])))

    return [
        web.get("/node", node_link), web.get("/machines", list_machines),
        web.post("/machines/{id:\\d+}/remove", remove_machine),
        web.get("/tasks", list_tasks), web.post("/tasks", create), web.get("/tasks/{id:\\d+}", show),
        web.get("/tasks/{id:\\d+}/log", tail),
        web.post("/tasks/{id:\\d+}/reply", reply), web.post("/tasks/{id:\\d+}/stop", stop),
        web.post("/commands", start_command), web.post("/tasks/{id:\\d+}/rerun", rerun),
        web.post("/connections", start_connection), web.post("/connections/{id:\\d+}", connection),
        web.post("/tasks/{id:\\d+}/merge", merge), web.post("/tasks/{id:\\d+}/discard", discard),
        web.post("/tasks/{id:\\d+}/edits", change_edits),
        web.post("/tasks/seen", seen), web.get("/events", events),
        web.post("/projects", register_project), web.post("/projects/git", git_init),
        web.get("/approvals", list_approvals), web.post("/approvals/{id:\\d+}", answer),
        web.post("/approvals/{id:\\d+}/remember", remember),
        web.post("/learn", session_ended),
    ]


def closing(r: "Runner"):
    """Middleware: 503 on non-GET requests once the runner is shutting down."""
    @web.middleware
    async def refuse(request: web.Request, handler):
        if r.closing and request.method != "GET":
            return web.json_response({"error": SHUTTING_DOWN}, status=503)
        return await handler(request)

    return refuse


def app(r: "Runner") -> web.Application:
    """The aiohttp app for runner `r`."""
    made = web.Application(middlewares=[errors, closing(r)])
    made.add_routes(routes(r))
    return made
