"""Relay between a Claude session and one MCP server (local process or HTTP via mcp_bridge).

Gives the CLI a uniform server whatever MCP features it has:
  read-only hints  readOnlyHint set from JARVIS's own sorting (mcp_servers), so the CLI runs reads in parallel
  MCP tasks        tools with execution.taskSupport are run as tasks (create, poll tasks/get, fetch tasks/result); the
                   tasks capability is hidden from the CLI. Every call carries a relay progress token.
  background       with `wait` (voice sessions, config.CONNECTION_WAIT), a call still running is handed to the task
                   runner as a connection task; the relay outlives the session (`detach`) until it ends.
Everything else passes through both ways. Relay request ids ("jarvis-N") never clash with the CLI's numeric ids.
"""

import asyncio
import itertools
import json
import logging
import os
import signal
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import httpx

from jarvis.contracts.runner import ConnectionRunner, RunnerUnavailable
from jarvis.integrations import mcp_bridge
from jarvis.integrations.mcp_servers import Server
from jarvis.storage import profile

log = logging.getLogger("jarvis.integrations.mcp_relay")

ERROR = mcp_bridge.ERROR
TASK_TTL_MS = 24 * 3600 * 1000  # requested task/result retention
POLL_SECONDS = 2.0  # task poll interval unless the server sets pollInterval
POLL_LIMITS = (0.5, 30.0)
FOLLOW_SECONDS = 5.0  # background call: progress report / stop check interval
BACKGROUND_HOURS = 24  # max background call duration
RESULT_CHARS = 20_000  # result chars stored on the task
REPORT_TRIES = 12  # ~1 min of retries to report the end while the runner restarts


@dataclass
class Call:
    """A CLI tools/call as the relay runs it."""
    tool: str
    args: dict
    token: str  # relay progress token
    cli_token: str | int | None  # CLI's progress token, if it asked
    request: str = ""  # relay request id
    task: str = ""  # server task id, if run as a task
    said: str = ""  # latest progress message
    background: bool = False


def _progress(params: dict) -> str:
    """Progress notification as text: its message, else a percentage."""
    if message := str(params.get("message") or "").strip():
        return message
    done, total = params.get("progress"), params.get("total")
    if isinstance(done, (int, float)) and isinstance(total, (int, float)) and total > 0:
        return f"{round(100 * min(done / total, 1))}% done"
    return ""


def _poll(task: dict) -> float:
    interval = task.get("pollInterval")
    seconds = interval / 1000 if isinstance(interval, (int, float)) and interval > 0 else POLL_SECONDS
    return min(max(seconds, POLL_LIMITS[0]), POLL_LIMITS[1])


def result_text(reply: dict) -> str:
    """Call result (text blocks, else structuredContent) or error message, as text."""
    if "error" in reply:
        return str((reply["error"] or {}).get("message") or "It failed.")
    result = reply.get("result") or {}
    parts = [b.get("text", "") if b.get("type") == "text" else f"[{b.get('type')}]"
             for b in result.get("content") or [] if isinstance(b, dict)]
    if not any(parts) and result.get("structuredContent") is not None:
        parts = [json.dumps(result["structuredContent"], ensure_ascii=False)]
    return "\n".join(parts).strip()[:RESULT_CHARS]


class Relay:
    """One session's traffic between the CLI (`to_cli`) and the server (`send`, set once open)."""

    def __init__(self, server: Server, to_cli: Callable[[dict], Awaitable[None]], wait: float = 0.0,
                 runner: ConnectionRunner | None = None) -> None:
        self.server, self.to_cli, self.wait, self.runner = server, to_cli, wait, runner
        self.send: Callable[[dict], Awaitable[None]] | None = None
        self.tasks = False  # server supports tools/call as tasks
        self.task_tools: set[str] = set()  # tools that can run as tasks
        self.pending: dict[str, asyncio.Future] = {}  # relay's own requests by id
        self.asked: dict = {}  # CLI requests whose replies get rewritten: id -> method
        self.calls: dict = {}  # in-flight CLI tools/call by CLI id
        self.by_token: dict[str, Call] = {}
        self.background: set[asyncio.Task] = set()  # followers of background calls
        self._tasks: set[asyncio.Task] = set()
        self._ids = itertools.count(1)

    def _id(self) -> str:
        return f"jarvis-{next(self._ids)}"

    def _spawn(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    # ── From the CLI ───────────────────────────────────────

    async def from_cli(self, message) -> None:
        one = message if isinstance(message, dict) else {}  # batches pass through
        method, mid, params = one.get("method"), one.get("id"), one.get("params") or {}
        if method in ("initialize", "tools/list"):
            self.asked[mid] = method  # reply rewritten on the way back
        if method == "tools/call" and mid is not None:
            self.calls[mid] = self._spawn(self._call(message))
        elif method == "notifications/cancelled" and (call := self.calls.get(params.get("requestId"))):
            call.cancel()  # _run tells the server
        elif method == "initialize":
            await self.send(message)  # awaited so later requests carry its session
        else:
            self._spawn(self.send(message))

    # ── From the server ────────────────────────────────────

    async def from_server(self, message) -> None:
        if not isinstance(message, dict):
            return await self.to_cli(message)
        mid, method = message.get("id"), message.get("method")
        params = message.get("params") or {}
        if method is None and mid in self.pending:
            if not (future := self.pending.pop(mid)).done():
                future.set_result(message)
            return
        if method is None and (asked := self.asked.pop(mid, None)) and isinstance(message.get("result"), dict):
            (self._initialized if asked == "initialize" else self._listed)(message["result"])
        elif method == "notifications/progress" and (call := self.by_token.get(params.get("progressToken"))):
            call.said = _progress(params) or call.said
            if call.cli_token is None or call.background:
                return
            message = message | {"params": params | {"progressToken": call.cli_token}}
        elif method == "notifications/tasks/status":
            for call in self.by_token.values():
                if call.task and call.task == params.get("taskId"):
                    call.said = params.get("statusMessage") or call.said
            return
        await self.to_cli(message)

    def _initialized(self, result: dict) -> None:
        """Note task support and hide it from the CLI; the relay handles tasks."""
        capabilities = result.get("capabilities") or {}
        self.tasks = "call" in (((capabilities.get("tasks") or {}).get("requests") or {}).get("tools") or {})
        capabilities.pop("tasks", None)

    def _listed(self, result: dict) -> None:
        """Set readOnlyHint from JARVIS's sorting; record and hide taskSupport."""
        for tool in result.get("tools") or []:
            if not isinstance(tool, dict):
                continue
            name, execution = tool.get("name"), tool.get("execution") or {}
            if execution.get("taskSupport") in ("optional", "required"):
                self.task_tools.add(name)
            if "taskSupport" in execution:
                tool["execution"] = {k: v for k, v in execution.items() if k != "taskSupport"}
            if spec := self.server.tools.get(name):
                tool["annotations"] = (tool.get("annotations") or {}) | {"readOnlyHint": spec.access == "read"}

    # ── Calls ──────────────────────────────────────────────

    async def request(self, method: str, params: dict, rid: str = "") -> dict:
        """Send a relay request; returns the reply ("result" or "error")."""
        rid = rid or self._id()
        self.pending[rid] = future = asyncio.get_running_loop().create_future()
        try:
            await self.send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
            return await future
        finally:
            self.pending.pop(rid, None)

    async def _call(self, message: dict) -> None:
        mid, params = message["id"], message.get("params") or {}
        meta = params.get("_meta") or {}
        call = Call(str(params.get("name", "")), params.get("arguments") or {}, self._id(), meta.get("progressToken"))
        self.by_token[call.token] = call
        run = self._spawn(self._run(call, params | {"_meta": meta | {"progressToken": call.token}}))
        try:
            done, _ = await asyncio.wait({run}, timeout=self.wait or None)
            if not done and (said := await self._to_background(call, run)):
                reply = {"result": {"content": [{"type": "text", "text": said}]}}
            else:
                reply = await run
            await self.to_cli({"jsonrpc": "2.0", "id": mid} | {k: reply[k] for k in ("result", "error") if k in reply})
        except asyncio.CancelledError:  # CLI cancelled or disconnected
            run.cancel()
            raise
        finally:
            self.calls.pop(mid, None)
            if not call.background:
                self.by_token.pop(call.token, None)

    async def _run(self, call: Call, params: dict) -> dict:
        """Make the call (as a task if supported); returns "result" or "error"."""
        call.request = self._id()
        try:
            if not (self.tasks and call.tool in self.task_tools):
                return await self.request("tools/call", params, call.request)
            made = await self.request("tools/call", params | {"task": {"ttl": TASK_TTL_MS}}, call.request)
            task = (made.get("result") or {}).get("task")
            if not isinstance(task, dict) or not task.get("taskId"):
                return made  # refused or answered immediately
            call.task = str(task["taskId"])
            while task.get("status") == "working":
                await asyncio.sleep(_poll(task))
                got = await self.request("tasks/get", {"taskId": call.task})
                if "error" in got:
                    return got
                task = got.get("result") or {}
                call.said = task.get("statusMessage") or call.said
            return await self.request("tasks/result", {"taskId": call.task})  # also blocks through input_required
        except asyncio.CancelledError:
            self._spawn(self._cancel(call))
            raise
        except (OSError, ValueError, httpx.HTTPError) as e:  # server gone
            why = type(e).__name__
            log.warning("%s: %s failed: %s", self.server.name, call.tool, why)
            return {"error": {"code": ERROR, "message": f"{self.server.label} couldn't be reached ({why})."}}

    async def _cancel(self, call: Call) -> None:
        try:
            if call.task:
                await asyncio.wait_for(self.request("tasks/cancel", {"taskId": call.task}), 10)
            elif call.request:
                await self.send({"jsonrpc": "2.0", "method": "notifications/cancelled",
                                 "params": {"requestId": call.request, "reason": "Stopped."}})
        except (OSError, ValueError, TimeoutError, httpx.HTTPError):
            pass

    # ── Background ─────────────────────────────────────────

    async def _to_background(self, call: Call, run: asyncio.Task) -> str:
        """Hand a running call to the task runner as a connection task; returns the CLI's reply text, or '' if the
        runner is unavailable or none was given (the call is then awaited in the foreground)."""
        if self.runner is None:
            return ""
        title = f"{self.server.label} {call.tool.replace('_', ' ')}"
        instruction = f"{self.server.tool(call.tool)} {json.dumps(call.args, ensure_ascii=False)}"
        try:
            task = await self.runner.start_connection(title, instruction, os.getpid())
        except (RunnerUnavailable, ValueError, OSError) as e:
            log.warning("%s: %s carries on in the foreground: %s", self.server.name, call.tool, e)
            return ""
        call.background = True
        follow = asyncio.create_task(self._follow(call, run, task["id"]))
        self.background.add(follow)
        follow.add_done_callback(self.background.discard)
        so_far = f" ({call.said})" if call.said else ""
        return (f"Still running on {self.server.label} after {self.wait:g} seconds{so_far}, so it carries on in the "
                f"background as task {task['id']}, {title}: JARVIS tells {profile.name()} how it went when it ends. Don't "
                f"call it again or wait for it; say it's under way and carry on.")

    async def _follow(self, call: Call, run: asyncio.Task, task_id: int) -> None:
        """Report progress until the call ends, cancelling it if the task is stopped or after BACKGROUND_HOURS; then
        report the outcome."""
        told, until = "", time.monotonic() + BACKGROUND_HOURS * 3600
        assert self.runner is not None  # only calls handed to a runner are followed
        try:
            while not run.done():
                await asyncio.wait({run}, timeout=FOLLOW_SECONDS)
                if run.done():
                    break
                if time.monotonic() > until:
                    run.cancel()
                    return await self._report(task_id, "failed", f"Gave up after {BACKGROUND_HOURS} hours.")
                try:
                    if (await self.runner.task(task_id))["status"] != "running":
                        run.cancel()  # stopped; the runner already announced it
                        return
                    if call.said != told:
                        await self.runner.connection_update(task_id, call.said)
                        told = call.said
                except (RunnerUnavailable, ValueError, OSError):
                    pass  # runner restarting; retry next round
            try:
                reply = run.result()
            except Exception as e:  # relay bug: never leave the task running
                log.exception("%s: %s failed", self.server.name, call.tool)
                reply = {"error": {"message": f"It failed ({type(e).__name__})."}}
            failed = "error" in reply or (reply.get("result") or {}).get("isError")
            await self._report(task_id, "failed" if failed else "done", result_text(reply))
        finally:
            self.by_token.pop(call.token, None)

    async def _report(self, task_id: int, outcome: str, result: str) -> None:
        assert self.runner is not None
        for _ in range(REPORT_TRIES):
            try:
                await self.runner.end_connection(task_id, outcome, result)
                return
            except (RunnerUnavailable, OSError):
                await asyncio.sleep(FOLLOW_SECONDS)
            except ValueError:  # task no longer exists
                break
        log.warning("%s: task %s's end couldn't be told", self.server.name, task_id)

    # ── Shutdown ───────────────────────────────────────────

    def closed(self) -> None:
        """Server gone: fail all pending relay requests."""
        for future in self.pending.values():
            if not future.done():
                future.set_result({"error": {"code": ERROR, "message": f"{self.server.label} stopped."}})

    async def finish(self) -> None:
        """CLI gone: cancel foreground calls, wait for background ones."""
        for call in list(self.calls.values()):
            call.cancel()
        await asyncio.gather(*self.background, return_exceptions=True)


class Process:
    """Local stdio server; each stdout line goes to `deliver`."""

    def __init__(self, proc: asyncio.subprocess.Process, deliver: Callable[[dict], Awaitable[None]]) -> None:
        self.proc = proc
        self.ended = asyncio.create_task(self._read(deliver))

    @classmethod
    async def start(cls, argv: list[str], env: dict[str, str], deliver: Callable[[dict], Awaitable[None]]) -> "Process":
        proc = await asyncio.create_subprocess_exec(*argv, env=env, stdin=asyncio.subprocess.PIPE,
                                                    stdout=asyncio.subprocess.PIPE, limit=mcp_bridge.LINE_LIMIT)
        return cls(proc, deliver)

    async def send(self, message: dict) -> None:
        self.proc.stdin.write(json.dumps(message, ensure_ascii=False).encode() + b"\n")
        await self.proc.stdin.drain()

    async def _read(self, deliver: Callable[[dict], Awaitable[None]]) -> None:
        while line := await self.proc.stdout.readline():
            try:
                message = json.loads(line)
            except ValueError:
                continue
            await deliver(message)

    async def close(self) -> None:
        if self.proc.returncode is None:
            self.proc.stdin.close()
            try:
                await asyncio.wait_for(self.proc.wait(), 3)
            except TimeoutError:
                self.proc.terminate()
                try:
                    await asyncio.wait_for(self.proc.wait(), 3)
                except TimeoutError:
                    self.proc.kill()
        self.ended.cancel()


async def serve(server: Server, wait: float = 0.0, command: tuple[list[str], dict[str, str]] | None = None,
                reader: asyncio.StreamReader | None = None, out=None,
                transport: httpx.AsyncBaseTransport | None = None, runner: ConnectionRunner | None = None) -> int:
    """Relay stdin/stdout to the server until either ends, then wait for background calls. Local servers start from
    `command` (mcp_launch.command); HTTP ones go through the bridge. Raises vault.VaultError if a secret isn't set."""
    reader = reader or await mcp_bridge._stdin()
    out = out or sys.stdout
    lock = asyncio.Lock()

    async def write(message: dict) -> None:
        async with lock:
            try:
                out.write(json.dumps(message, ensure_ascii=False) + "\n")
                out.flush()
            except (BrokenPipeError, ValueError):  # CLI gone; background calls continue
                pass

    relay = Relay(server, write, wait, runner)
    if server.transport == "http":
        bridge = mcp_bridge.Bridge(server, relay.from_server, transport)
        relay.send, ended, close = bridge.send, asyncio.get_running_loop().create_future(), bridge.close
    else:
        process = await Process.start(*command, relay.from_server)  # pyright: ignore[reportCallIssue]  (stdio: given)
        relay.send, ended, close = process.send, process.ended, process.close

    async def pump() -> None:
        while line := await reader.readline():
            try:
                message = json.loads(line)
            except ValueError:
                continue
            await relay.from_cli(message)

    cli = asyncio.create_task(pump())
    try:
        await asyncio.wait({cli, ended}, return_when=asyncio.FIRST_COMPLETED)
        if ended.done():
            relay.closed()
        await relay.finish()
    finally:
        cli.cancel()
        await close()
    return 0


def detach() -> None:
    """Fork into a new session so background calls survive the CLI killing its child at session end. The parent
    just waits and exits as soon as it is signalled."""
    if pid := os.fork():
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, lambda *_: os._exit(0))
        _, status = os.waitpid(pid, 0)
        os._exit(max(os.waitstatus_to_exitcode(status), 0))
    os.setsid()
