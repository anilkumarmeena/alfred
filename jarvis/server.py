"""Web front end for the phone and browsers: uv run python -m jarvis.server

Serves web/dist and, for a valid login passcode (jarvis.core.logins), issues LiveKit tokens, stores Web Push and APNs
registrations, accepts photos (/api/picture), phone debug logs (/api/log) and control panel actions
(/api/panel/ACTION). /api/events streams runner events and activity as JSON lines. Exceptions to passcode auth:
/api/approvals/ID uses the push's one-time nonce; /api/files/TOKEN is a capability link; /api/google/callback is where
Google returns a panel sign-in, checked by its one-time state (jarvis.integrations.google_signin); a paired machine's
link (/api/node) is passed on to the task runner, which signs it in with its keys, and /api/node/pair takes a one-time
pairing code. Reached via Tailscale Serve (tailnet-only HTTPS) or http://localhost:8787. Wrong passcodes, nonces and
pairing codes share one rate limit.
"""

import asyncio
import base64
import contextlib
import json
import logging
import re
import secrets
import time
from collections.abc import Awaitable, Callable
from datetime import timedelta
from functools import wraps
from urllib.parse import quote

import aiohttp
from aiohttp import web
from livekit import api

from jarvis import panel
from jarvis.core import credentials, logins, vault
from jarvis.core.config import LIVEKIT_URL, PUSH_ENABLED, ROOT, WEB_PORT
from jarvis.integrations import google_signin
from jarvis.node import link as node_link, machines
from jarvis.notify import apns, fcm, push
from jarvis.state import activity, pictures, shared_files, todo
from jarvis.tasks import client
from jarvis.voice import quiet

log = logging.getLogger("jarvis.server")
WEB_DIR = ROOT / "web" / "dist"  # Vite build
NOT_BUILT = "The JARVIS web app isn't built. Run `make web-build`, then restart the web service."
MAX_FAILURES, FAILURE_WINDOW = 10, 300  # wrong credentials per 5 min, across all clients
MAX_LOG_LINES = 50  # per /api/log request
_failures: list[float] = []
PUSH_OFF = ("Notifications are off on the computer JARVIS runs on. Remove JARVIS_PUSH=off from .env, then restart "
            "JARVIS.")
PING_SECONDS = 25  # /api/events keepalive interval
ACTIVITY_SECONDS = 2  # /api/events poll interval for activity entries and to-dos (written by other processes)
RUNNER_DOWN = "JARVIS's task runner isn't running."
BUILD_META = re.compile(r'<meta name="jarvis-build" content="([0-9a-f]{1,64})"')
MAX_REQUEST = pictures.MAX_UPLOAD * 4 // 3 + 64 * 1024  # base64 photo plus JSON overhead


def _locked_out(now: float) -> bool:
    """Too many wrong passcodes or nonces within FAILURE_WINDOW."""
    _failures[:] = [t for t in _failures if now - t < FAILURE_WINDOW]
    return len(_failures) >= MAX_FAILURES


async def _json_body(request: web.Request) -> dict:
    """JSON object body, or {}."""
    try:
        body = await request.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


async def _authorised(request: web.Request) -> tuple[dict, web.Response | None]:
    """(body with "login" set, None) for a valid passcode, else ({}, error response)."""
    now = time.time()
    if _locked_out(now):
        return {}, web.json_response({"error": "Too many wrong passcodes. Try again in a few minutes."}, status=429)
    body = await _json_body(request)
    login = await asyncio.to_thread(logins.check, str(body.get("passcode", "")))
    if login is None:
        _failures.append(now)
        log.warning("wrong passcode from %s", request.remote)
        return {}, web.json_response({"error": "Wrong passcode."}, status=401)
    return body | {"login": login}, None


def authed(handler: Callable[[web.Request, dict], Awaitable[web.StreamResponse]]):
    """Decorator: require a valid passcode; the handler gets the JSON body with "login"."""
    @wraps(handler)
    async def checked(request: web.Request) -> web.StreamResponse:
        body, denied = await _authorised(request)
        return denied or await handler(request, body)
    return checked


def build_id() -> str:
    """Build id from index.html's <meta name="jarvis-build">, or "". Read on every call since rebuilds happen under
    a running server; open pages reload when it changes."""
    try:
        found = BUILD_META.search((WEB_DIR / "index.html").read_text(errors="replace"))
    except OSError:
        return ""
    return found[1] if found else ""


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())[:16]


def _device(body: dict) -> str:
    return _slug(str(body.get("device", ""))) or "device"


@authed
async def token(request: web.Request, body: dict) -> web.Response:
    device, login = _device(body), body["login"]
    room = f"jarvis-{secrets.token_hex(4)}"  # fresh room per connection = fresh agent session
    await asyncio.to_thread(logins.used, login)
    jwt = (
        api.AccessToken(credentials.get(credentials.LIVEKIT_KEY), credentials.get(credentials.LIVEKIT_SECRET))
        .with_identity(f"{_slug(login) or 'user'}-{device}-{secrets.token_hex(2)}")
        .with_name(login)
        # Initial speaker switch (voice.quiet); the page updates it later, hence the metadata grant
        .with_attributes({quiet.SPEAKER: "off" if body.get("speaker") == "off" else "on"})
        .with_grants(api.VideoGrants(room_join=True, room=room, can_publish=True, can_subscribe=True,
                                     can_update_own_metadata=True))
        .with_ttl(timedelta(hours=12))
        .to_jwt()
    )
    # Local browsers hit LiveKit directly; others go via Tailscale Serve, which proxies /rtc to LiveKit
    host = request.headers.get("X-Forwarded-Host") or request.host
    url = LIVEKIT_URL if host.split(":")[0] in ("localhost", "127.0.0.1") else f"wss://{host}"
    return web.json_response({"token": jwt, "url": url, "build": build_id()}, headers={"Cache-Control": "no-store"})


async def push_key(_: web.Request) -> web.Response:
    """Push status and VAPID public key (public, no auth)."""
    body = {"enabled": True, "key": push.public_key()} if PUSH_ENABLED else {"enabled": False, "error": PUSH_OFF}
    return web.json_response(body, headers={"Cache-Control": "no-store"})


@authed
async def push_subscribe(_: web.Request, body: dict) -> web.Response:
    if not PUSH_ENABLED:
        return web.json_response({"error": PUSH_OFF}, status=409)
    try:
        sub = push.Subscriptions().add(body.get("subscription"), _device(body))
    except ValueError as e:
        return web.json_response({"error": f"That subscription wasn't accepted: {e}."}, status=400)
    log.info("push subscription saved for %s", sub["device"])
    return web.json_response({"ok": True})


@authed
async def push_unsubscribe(_: web.Request, body: dict) -> web.Response:
    removed = push.Subscriptions().remove([str(body.get("endpoint", ""))])
    return web.json_response({"ok": True, "removed": removed})


@authed
async def push_native(_: web.Request, body: dict) -> web.Response:
    """Register a native app's push token {"passcode", "device", "token", "topic"}: APNs for iOS ("environment"), FCM
    for Android ("service": "fcm"). Stored even before sending is set up; `sending` says whether pushes will go out."""
    if not PUSH_ENABLED:
        return web.json_response({"error": PUSH_OFF}, status=409)
    service = fcm if body.get("service") == "fcm" else apns
    try:
        dev = service.Devices().add(body, _device(body))
    except ValueError as e:
        return web.json_response({"error": f"That device token wasn't accepted: {e}."}, status=400)
    sending = await asyncio.to_thread(service.configured)
    where = dev.get("environment") or "fcm"
    log.info("app push token saved for %s (%s)%s", dev["device"], where, "" if sending else "; " + service.NOT_SET_UP)
    return web.json_response({"ok": True, "sending": sending} | ({} if sending else {"error": service.NOT_SET_UP}))


@authed
async def phone_log(_: web.Request, body: dict) -> web.Response:
    """Write the page's earphone/mic debug lines to logs/web.log."""
    lines = body.get("lines")
    if not isinstance(lines, list):
        return web.json_response({"error": "No lines."}, status=400)
    device = _device(body)
    for line in lines[:MAX_LOG_LINES]:
        log.info("%s: %s", device, re.sub(r"[^\x20-\x7e]", "?", str(line))[:300])
    return web.json_response({"ok": True})


async def approval(request: web.Request) -> web.Response:
    """Approve/Deny from a push button: {"nonce", "decision", "device"}. The one-time nonce (runner stores only its
    hash) is the credential, since the app doesn't keep the passcode. Wrong nonces count toward the lockout."""
    now = time.time()
    if _locked_out(now):
        return web.json_response({"error": "Too many wrong answers. Try again in a few minutes."}, status=429)
    body = await _json_body(request)
    if body.get("decision") not in ("approve", "deny") or not isinstance(body.get("nonce"), str):
        return web.json_response({"error": "Send the nonce and a decision, approve or deny."}, status=400)
    approval_id = int(request.match_info["id"])
    try:
        answered = await client.answer(approval_id, body["decision"], "push", body["nonce"][:100])
    except client.NotAllowed:
        _failures.append(now)
        log.warning("wrong approval nonce from %s", request.remote)
        return web.json_response({"error": "That answer doesn't match the request."}, status=403)
    except client.RunnerUnavailable:
        return web.json_response({"error": "JARVIS's task runner isn't running."}, status=503)
    except ValueError as e:
        return web.json_response({"error": str(e)}, status=400)
    log.info("request %s answered from %s: %s", approval_id, _device(body), answered["outcome"])
    return web.json_response({"ok": True, "state": answered["state"], "outcome": answered["outcome"]})


@authed
async def picture(_: web.Request, body: dict) -> web.Response:
    """Queue a photo {"passcode", "data": base64} for the user's next turn; returns its id for /api/picture/drop."""
    try:
        data = base64.b64decode(str(body.get("data", "")), validate=True)
    except ValueError:
        return web.json_response({"error": "That photo didn't arrive whole."}, status=400)
    try:
        photo = await asyncio.to_thread(pictures.save_photo, data)
    except (ValueError, RuntimeError) as e:
        return web.json_response({"error": str(e)}, status=400)
    log.info("photo %s from %s", photo, _device(body))
    return web.json_response({"ok": True, "id": photo})


@authed
async def picture_drop(_: web.Request, body: dict) -> web.Response:
    """Withdraw a queued photo: {"passcode", "id"}."""
    try:
        dropped = await asyncio.to_thread(pictures.drop, str(body.get("id", "")))
    except ValueError as e:
        return web.json_response({"error": str(e)}, status=400)
    return web.json_response({"ok": True, "dropped": dropped})


@authed
async def panel_action(request: web.Request, body: dict) -> web.Response:
    """Run a control panel action (jarvis.panel) with the request body."""
    name = request.match_info["action"]
    if name not in panel.ACTIONS:
        return web.json_response({"error": f"There's no {name} in the panel."}, status=404)
    try:
        result = await panel.call(name, body)
    except client.RunnerUnavailable:
        return web.json_response({"error": RUNNER_DOWN}, status=503)
    except (ValueError, PermissionError) as e:  # bad request or runner refusal (incl. NotAllowed)
        return web.json_response({"error": str(e)}, status=400)
    return web.json_response({"result": result}, headers={"Cache-Control": "no-store"})


@authed
async def events(request: web.Request, _: dict) -> web.StreamResponse:
    """NDJSON stream: "hello" (build id, so stale pages reload), then runner events, "activity" entries, "todos" (the
    whole list, whenever it changes) and "ping" every PING_SECONDS when idle. Ends with an "error" line if the runner
    goes away; the page reconnects."""
    resp = web.StreamResponse(headers={"Content-Type": "application/x-ndjson", "Cache-Control": "no-store",
                                       "X-Accel-Buffering": "no"})
    await resp.prepare(request)
    queue: asyncio.Queue = asyncio.Queue()

    async def relay() -> None:
        try:
            async for event in client.events():
                await queue.put(event)
        except client.RunnerUnavailable:
            await queue.put({"type": "error", "text": RUNNER_DOWN})
        await queue.put(None)

    async def bell() -> None:
        last, todos = await asyncio.to_thread(activity.latest_id), await asyncio.to_thread(todo.items)
        while True:
            await asyncio.sleep(ACTIVITY_SECONDS)
            for entry in reversed(await asyncio.to_thread(activity.recent, last)):  # oldest first
                last = entry["id"]
                await queue.put({"type": "activity", "entry": entry})
            # To-dos change by voice, from tasks and on other devices: send the list when it does
            if (now := await asyncio.to_thread(todo.items)) != todos:
                todos = now
                await queue.put({"type": "todos", "todos": todos})

    reader, watcher = asyncio.create_task(relay()), asyncio.create_task(bell())
    try:
        await resp.write(json.dumps({"type": "hello", "build": build_id()}).encode() + b"\n")
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), PING_SECONDS)
            except TimeoutError:
                event = {"type": "ping"}
            if event is None:
                break
            await resp.write(json.dumps(event).encode() + b"\n")
    except ConnectionResetError:
        pass  # client disconnected
    finally:
        for task in (reader, watcher):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
    return resp


async def node_pair(request: web.Request) -> web.Response:
    """A machine pairs: {"code", "public_key", "facts"} -> this computer's public key. The code is the credential;
    the machine connects only once the user confirms its fingerprint in the panel. Wrong codes count toward the
    lockout."""
    now = time.time()
    if _locked_out(now):
        return web.json_response({"error": "Too many wrong codes. Try again in a few minutes."}, status=429)
    body = await _json_body(request)
    facts = body.get("facts") if isinstance(body.get("facts"), dict) else {}
    try:
        found = await asyncio.to_thread(machines.redeem, str(body.get("code", ""))[:40], str(body.get("public_key", "")),
                                        facts)
        main_public = await asyncio.to_thread(machines.main_public)
    except ValueError as e:
        _failures.append(now)
        log.warning("wrong pairing code from %s", request.remote)
        return web.json_response({"error": str(e)}, status=403)
    except vault.VaultError as e:
        return web.json_response({"error": f"This computer's vault isn't ready: {e}"}, status=503)
    await asyncio.to_thread(activity.add, f"{found.name} asks to be paired: check its fingerprint, {found.fingerprint}, "
                                          "and confirm it in Settings › Machines.", "warn")
    return web.json_response({"node": found.id, "name": found.name, "main_public": main_public,
                              "fingerprint": found.fingerprint})


async def node_relay(request: web.Request) -> web.WebSocketResponse:
    """A machine's link, passed on to the task runner as is: the runner signs it in, so nothing here is trusted."""
    ws = web.WebSocketResponse(heartbeat=PING_SECONDS, max_msg_size=node_link.MAX_MESSAGE)
    await ws.prepare(request)
    try:
        async with client.node_link() as upstream:
            await _pipe(ws, upstream)
    except client.RunnerUnavailable:
        await ws.close(code=aiohttp.WSCloseCode.TRY_AGAIN_LATER, message=b"task runner down")
    return ws


async def _pipe(a, b) -> None:
    """Pass text messages both ways between two WebSockets until either closes, then close both."""
    async def one_way(source, sink) -> None:
        async for msg in source:
            if msg.type != aiohttp.WSMsgType.TEXT:
                break
            await sink.send_str(msg.data)

    ways = [asyncio.create_task(one_way(a, b)), asyncio.create_task(one_way(b, a))]
    try:
        await asyncio.wait(ways, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for way in ways:
            way.cancel()
        for ws in (a, b):
            with contextlib.suppress(Exception):
                await ws.close()


async def shared_file(request: web.Request) -> web.StreamResponse:
    """Serve a shared file by capability token (sent only in pushes). aiohttp handles the player's range requests."""
    path = shared_files.shared(request.match_info["token"])
    if path is None:
        return web.Response(status=404, text="That file isn't shared any more.")
    return web.FileResponse(path, headers={"Cache-Control": "private, max-age=3600",
                                           "Content-Disposition": f"inline; filename*=UTF-8''{quote(path.name)}"})


async def google_callback(request: web.Request) -> web.Response:
    """Where Google sends a panel sign-in back (public: the one-time state is the credential)."""
    return web.Response(text=await google_signin.signins.callback(request.query), content_type="text/html",
                        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})


def _built(name: str, headers: dict[str, str]) -> web.StreamResponse:
    """File from the web build, or 503 asking to build it."""
    if not (WEB_DIR / name).is_file():
        return web.Response(status=503, text=NOT_BUILT)
    return web.FileResponse(WEB_DIR / name, headers=headers)


async def index(_: web.Request) -> web.StreamResponse:
    return _built("index.html", {"Cache-Control": "no-cache"})


async def service_worker(_: web.Request) -> web.StreamResponse:
    # Served from / so its scope covers the whole app
    return _built("sw.js", {"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"})


def app() -> web.Application:
    a = web.Application(client_max_size=MAX_REQUEST)
    a.router.add_get("/", index)
    a.router.add_get("/sw.js", service_worker)
    a.router.add_post("/api/token", token)
    a.router.add_get("/api/push/key", push_key)
    a.router.add_post("/api/push/subscribe", push_subscribe)
    a.router.add_post("/api/push/unsubscribe", push_unsubscribe)
    a.router.add_post("/api/push/native", push_native)
    a.router.add_post("/api/log", phone_log)
    a.router.add_post("/api/approvals/{id:\\d+}", approval)
    a.router.add_post("/api/picture", picture)
    a.router.add_post("/api/picture/drop", picture_drop)
    a.router.add_post("/api/panel/{action:[a-z_]+}", panel_action)
    a.router.add_post("/api/events", events)
    a.router.add_get(f"/api/files/{{token:{shared_files.TOKEN}}}", shared_file)
    a.router.add_get(google_signin.CALLBACK, google_callback)
    a.router.add_post("/api/node/pair", node_pair)
    a.router.add_get("/api/node", node_relay)
    if WEB_DIR.is_dir():  # aiohttp rejects a missing folder
        a.router.add_static("/static/", WEB_DIR)
    else:
        log.warning(NOT_BUILT)
    return a


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    # Move remaining .env credentials into the vault; JARVIS_PASSCODE becomes the first login
    for label in credentials.adopt():
        log.warning("moved the %s from .env into the vault", label)
    logins.adopt()
    if not logins.ready():
        raise SystemExit("Nobody can log in: run make setup, which makes the first login.")
    if PUSH_ENABLED:
        push.vapid()  # generate now rather than on first page load
    web.run_app(app(), host="127.0.0.1", port=WEB_PORT)
