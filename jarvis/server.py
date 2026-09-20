"""Front end for the phone and browsers: uv run python -m jarvis.server

Serves the web app's build (web/dist, from `make web-build`) and issues LiveKit tokens to whoever has a login's passcode
(jarvis.core.logins: each person has their own, hashed in secrets/; the passcode says whose login it is). Reached from
the iPhone through Tailscale Serve (HTTPS, tailnet devices only), or on the Mac at
http://localhost:8787. Also takes the phone's Web Push subscription (jarvis.notify.push), or the JARVIS
app's APNs token (jarvis.notify.apns), with the same passcode, so the task runner can notify it when JARVIS isn't open, and
logs what the page makes of earphone presses (/api/log), to debug them on the iPhone. The app's
Approve and Deny buttons on a task's request to run a risky command answer it through
/api/approvals/ID, with the one-time nonce from the push instead of the passcode. A photo the user sends JARVIS comes
in on /api/picture and waits for their next turn (jarvis.state.pictures). A file JARVIS made for the phone, such as
an audio file, is fetched from /api/files/TOKEN, by the link in the push that told of it (jarvis.state.shared_files).

The page's control panel (tasks, to-dos, projects, past conversations, settings) calls /api/panel/ACTION (jarvis.panel), and follows
the task runner's events, as they happen, on /api/events: a stream of JSON lines, with a ping now and then so
proxies keep it open.
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

from aiohttp import web
from livekit import api

from jarvis import panel
from jarvis.core import credentials, logins
from jarvis.core.config import LIVEKIT_URL, PUSH_ENABLED, ROOT, WEB_PORT
from jarvis.notify import apns, push
from jarvis.state import activity, pictures, shared_files
from jarvis.tasks import client
from jarvis.voice import quiet

log = logging.getLogger("jarvis.server")
WEB_DIR = ROOT / "web" / "dist"  # the Vite build of web/
NOT_BUILT = "The JARVIS web app isn't built. Run `make web-build`, then restart the web service."
MAX_FAILURES, FAILURE_WINDOW = 10, 300  # wrong passcodes allowed per 5 minutes, across all clients
MAX_LOG_LINES = 50  # per /api/log request
_failures: list[float] = []
PUSH_OFF = "Notifications are off on the Mac. Remove JARVIS_PUSH=off from .env, then restart JARVIS."
PING_SECONDS = 25  # how often /api/events sends a ping while nothing happens
ACTIVITY_SECONDS = 2  # how often /api/events looks for new activity entries, which other processes write
RUNNER_DOWN = "JARVIS's task runner isn't running."
BUILD_META = re.compile(r'<meta name="jarvis-build" content="([0-9a-f]{1,64})"')
MAX_REQUEST = pictures.MAX_UPLOAD * 4 // 3 + 64 * 1024  # a photo, in base64, and the rest of its JSON


def _locked_out(now: float) -> bool:
    """Whether too many wrong passcodes (or approval nonces) came in the last FAILURE_WINDOW seconds."""
    _failures[:] = [t for t in _failures if now - t < FAILURE_WINDOW]
    return len(_failures) >= MAX_FAILURES


async def _json_body(request: web.Request) -> dict:
    """The request's JSON body, or {} if it isn't a JSON object."""
    try:
        body = await request.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


async def _authorised(request: web.Request) -> tuple[dict, web.Response | None]:
    """The request's JSON body, with "login" set to whose passcode it carries, and an error response unless it carries
    one."""
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
    """A handler that runs only for requests carrying a login's passcode, and gets their JSON body, with the login."""
    @wraps(handler)
    async def checked(request: web.Request) -> web.StreamResponse:
        body, denied = await _authorised(request)
        return denied or await handler(request, body)
    return checked


def build_id() -> str:
    """The web app's build being served, from the <meta name="jarvis-build"> the build puts in index.html, or "".
    Read each time, as a rebuild changes it under the running server; an open page that sees a new one reloads."""
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
    room = f"jarvis-{secrets.token_hex(4)}"  # a fresh room per connection gets a fresh agent session
    await asyncio.to_thread(logins.used, login)
    jwt = (
        api.AccessToken(credentials.get(credentials.LIVEKIT_KEY), credentials.get(credentials.LIVEKIT_SECRET))
        .with_identity(f"{_slug(login) or 'user'}-{device}-{secrets.token_hex(2)}")
        .with_name(login)
        # The speaker switch (jarvis.voice.quiet) as the session starts; the page changes it later, hence the metadata grant
        .with_attributes({quiet.SPEAKER: "off" if body.get("speaker") == "off" else "on"})
        .with_grants(api.VideoGrants(room_join=True, room=room, can_publish=True, can_subscribe=True,
                                     can_update_own_metadata=True))
        .with_ttl(timedelta(hours=12))
        .to_jwt()
    )
    # Browsers on the Mac talk to LiveKit directly; everything else comes through
    # Tailscale Serve, which proxies /rtc on the same HTTPS host to the LiveKit server.
    host = request.headers.get("X-Forwarded-Host") or request.host
    url = LIVEKIT_URL if host.split(":")[0] in ("localhost", "127.0.0.1") else f"wss://{host}"
    return web.json_response({"token": jwt, "url": url, "build": build_id()}, headers={"Cache-Control": "no-store"})


async def push_key(_: web.Request) -> web.Response:
    """Whether push is on and, if so, the VAPID public key the page subscribes with. Public by nature."""
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
    """The JARVIS app's APNs token: {"passcode", "device", "token", "environment", "topic"}. Kept even before
    APNs is set up on the Mac, so the app's notifications start once it is; `sending` says whether they do."""
    if not PUSH_ENABLED:
        return web.json_response({"error": PUSH_OFF}, status=409)
    try:
        dev = apns.Devices().add(body, _device(body))
    except ValueError as e:
        return web.json_response({"error": f"That device token wasn't accepted: {e}."}, status=400)
    sending = apns.configured()
    log.info("app push token saved for %s (%s)%s", dev["device"], dev["environment"], "" if sending else "; " + apns.NOT_SET_UP)
    return web.json_response({"ok": True, "sending": sending} | ({} if sending else {"error": apns.NOT_SET_UP}))


@authed
async def phone_log(_: web.Request, body: dict) -> web.Response:
    """Lines the page traces about earphone presses and the mic, written to this log (logs/web.log)."""
    lines = body.get("lines")
    if not isinstance(lines, list):
        return web.json_response({"error": "No lines."}, status=400)
    device = _device(body)
    for line in lines[:MAX_LOG_LINES]:
        log.info("%s: %s", device, re.sub(r"[^\x20-\x7e]", "?", str(line))[:300])
    return web.json_response({"ok": True})


async def approval(request: web.Request) -> web.Response:
    """The JARVIS app's answer to a task's request to run a risky command, from the Approve or Deny button on its
    push: {"nonce", "decision": "approve" | "deny", "device"}. The app doesn't keep the passcode, so the nonce the
    push carried is the credential: one per request, checked by the runner, which keeps only its hash. Wrong ones
    count towards the same lockout as wrong passcodes."""
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
    """A photo the user sends JARVIS: {"passcode", "data": base64 of the picture}. It waits for their next turn,
    spoken or typed, which shows it to Claude (jarvis.state.pictures). Returns its id, to take it back with
    /api/picture/drop."""
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
    """Take back a photo still waiting for the next turn: {"passcode", "id"}."""
    try:
        dropped = await asyncio.to_thread(pictures.drop, str(body.get("id", "")))
    except ValueError as e:
        return web.json_response({"error": str(e)}, status=400)
    return web.json_response({"ok": True, "dropped": dropped})


@authed
async def panel_action(request: web.Request, body: dict) -> web.Response:
    """One of the control panel's actions (jarvis.panel), with the request's body."""
    name = request.match_info["action"]
    if name not in panel.ACTIONS:
        return web.json_response({"error": f"There's no {name} in the panel."}, status=404)
    try:
        result = await panel.call(name, body)
    except client.RunnerUnavailable:
        return web.json_response({"error": RUNNER_DOWN}, status=503)
    except (ValueError, PermissionError) as e:  # what the runner refused (NotAllowed too), or a bad request
        return web.json_response({"error": str(e)}, status=400)
    return web.json_response({"result": result}, headers={"Cache-Control": "no-store"})


@authed
async def events(request: web.Request, _: dict) -> web.StreamResponse:
    """The task runner's events as JSON lines, as they happen, and new entries for the bell ("activity", from
    jarvis.state.activity, which the voice agent and the runner add to), with a ping every PING_SECONDS while there's
    nothing else. Starts with a "hello" line saying which build of the web app is served, so a page left open across a
    rebuild reloads. Ends with an "error" line if the runner goes away; the page then tries again."""
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
        last = await asyncio.to_thread(activity.latest_id)
        while True:
            await asyncio.sleep(ACTIVITY_SECONDS)
            for entry in reversed(await asyncio.to_thread(activity.recent, last)):  # oldest first
                last = entry["id"]
                await queue.put({"type": "activity", "entry": entry})

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
        pass  # the page went away
    finally:
        for task in (reader, watcher):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
    return resp


async def shared_file(request: web.Request) -> web.StreamResponse:
    """A file JARVIS made and shared with the phone, such as an audio file (jarvis.state.shared_files), to whoever has
    its link, which only JARVIS's pushes carry. aiohttp answers range requests, which the phone's player makes."""
    path = shared_files.shared(request.match_info["token"])
    if path is None:
        return web.Response(status=404, text="That file isn't shared any more.")
    return web.FileResponse(path, headers={"Cache-Control": "private, max-age=3600",
                                           "Content-Disposition": f"inline; filename*=UTF-8''{quote(path.name)}"})


def _built(name: str, headers: dict[str, str]) -> web.StreamResponse:
    """A file from the web app's build, or a page saying to build it."""
    if not (WEB_DIR / name).is_file():
        return web.Response(status=503, text=NOT_BUILT)
    return web.FileResponse(WEB_DIR / name, headers=headers)


async def index(_: web.Request) -> web.StreamResponse:
    return _built("index.html", {"Cache-Control": "no-cache"})


async def service_worker(_: web.Request) -> web.StreamResponse:
    # Served from the root, not /static/, so its scope covers the whole app
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
    if WEB_DIR.is_dir():  # aiohttp refuses a missing folder; the page then says to build it
        a.router.add_static("/static/", WEB_DIR)
    else:
        log.warning(NOT_BUILT)
    return a


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    # What .env still holds moves into the vault, and its passcode becomes the user's login
    for label in credentials.adopt():
        log.warning("moved the %s from .env into the vault", label)
    logins.adopt()
    if not logins.ready():
        raise SystemExit("Nobody can log in: run make setup, which makes the first login.")
    if PUSH_ENABLED:
        push.vapid()  # make the key pair now, not on the first page load
    web.run_app(app(), host="127.0.0.1", port=WEB_PORT)
