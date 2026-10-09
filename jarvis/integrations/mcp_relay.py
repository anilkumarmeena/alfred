"""Between a Claude session and one of its connections (an MCP server): the launcher (jarvis.integrations.mcp_launch)
runs the server, a local program or an HTTP endpoint through the bridge (jarvis.integrations.mcp_bridge), behind this
relay, so the CLI meets every server the same way, whatever MCP features it has.

- Reads side by side. The CLI runs several calls of one step at once only when the server's tools/list marks them
  readOnlyHint, which many servers leave out or get wrong. Here each tool is marked as JARVIS sorted it
  (jarvis.integrations.mcp_servers): a read as read-only, anything else not.
- Long-running calls. A tool the server runs as an MCP task (its execution.taskSupport, MCP 2025-11-25) is called as
  one: the relay creates the task, follows it with tasks/get until it ends, and fetches its tasks/result. The server's
  tasks capability is kept from the CLI, which makes a plain call either way. Every call carries a progress token of
  the relay's, so what the server says of its progress, or a task's status message, is known while it runs.
- Background. With a wait (the voice brain's sessions: config.CONNECTION_WAIT), a call still running after it is
  answered there and then: it carries on as a task of the task runner's (kind connection: jarvis.tasks.connection_jobs),
  which says how it went when it ends, like any task, and whose task_stop cancels it. The relay outlives the session
  that started it (`detach`) until its calls in the background have ended.

Everything else passes through as it is, both ways: the CLI's other requests and notifications, the server's own
requests (roots, sampling, elicitation) and the answers to them. The relay's requests have ids of their own
("jarvis-1"), which never clash with the CLI's numbers.
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

from jarvis.core import profile
from jarvis.integrations import mcp_bridge
from jarvis.integrations.mcp_servers import Server
from jarvis.tasks import client

log = logging.getLogger("jarvis.integrations.mcp_relay")

ERROR = mcp_bridge.ERROR
TASK_TTL_MS = 24 * 3600 * 1000  # how long a server is asked to keep a task and its result
POLL_SECONDS = 2.0  # between looks at a task, unless the server asks for another pace (pollInterval)
POLL_LIMITS = (0.5, 30.0)
FOLLOW_SECONDS = 5.0  # how often a call in the background tells the runner how it goes and hears if it was stopped
BACKGROUND_HOURS = 24  # the longest a call carries on in the background
RESULT_CHARS = 20_000  # of what a call in the background returned, kept on its task
REPORT_TRIES = 12  # a minute of tries to tell the runner a call's end, while it restarts


@dataclass
class Call:
    """One of the CLI's tools/call requests, as the relay makes it."""
    tool: str
    args: dict
    token: str  # the progress token the relay gave it
    cli_token: str | int | None  # the CLI's own, when it asked for progress
    request: str = ""  # the id of the tools/call the relay sent
    task: str = ""  # the server's task id, when it runs as one
    said: str = ""  # the latest the server said of how it's going
    background: bool = False


def _progress(params: dict) -> str:
    """A progress notification as words: its message, else how far along it is."""
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
    """What a call returned, or why it failed, as text: its text blocks, else its structured content."""
    if "error" in reply:
        return str((reply["error"] or {}).get("message") or "It failed.")
    result = reply.get("result") or {}
    parts = [b.get("text", "") if b.get("type") == "text" else f"[{b.get('type')}]"
             for b in result.get("content") or [] if isinstance(b, dict)]
    if not any(parts) and result.get("structuredContent") is not None:
        parts = [json.dumps(result["structuredContent"], ensure_ascii=False)]
    return "\n".join(parts).strip()[:RESULT_CHARS]


class Relay:
    """Carries one session's messages between the CLI (`to_cli`) and the server (`send`, set once it's open)."""

    def __init__(self, server: Server, to_cli: Callable[[dict], Awaitable[None]], wait: float = 0.0,
                 runner=client) -> None:
        self.server, self.to_cli, self.wait, self.runner = server, to_cli, wait, runner
        self.send: Callable[[dict], Awaitable[None]] | None = None
        self.tasks = False  # the server runs tools/call as tasks
        self.task_tools: set[str] = set()  # those of its tools it can run as one
        self.pending: dict[str, asyncio.Future] = {}  # the relay's own requests, by id
        self.asked: dict = {}  # the CLI's requests whose answers the relay changes: id -> method
        self.calls: dict = {}  # the CLI's tools/call requests being made, by its id
        self.by_token: dict[str, Call] = {}
        self.background: set[asyncio.Task] = set()  # following the calls carried on in the background
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
        one = message if isinstance(message, dict) else {}  # a batch passes as it is
        method, mid, params = one.get("method"), one.get("id"), one.get("params") or {}
        if method in ("initialize", "tools/list"):
            self.asked[mid] = method  # its answer is changed on the way back
        if method == "tools/call" and mid is not None:
            self.calls[mid] = self._spawn(self._call(message))
        elif method == "notifications/cancelled" and (call := self.calls.get(params.get("requestId"))):
            call.cancel()  # and the server is told, as the relay made the call (_run)
        elif method == "initialize":
            await self.send(message)  # first, so what follows carries its session
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
        """Whether the server runs calls as tasks, which the CLI isn't told: the relay does that."""
        capabilities = result.get("capabilities") or {}
        self.tasks = "call" in (((capabilities.get("tasks") or {}).get("requests") or {}).get("tools") or {})
        capabilities.pop("tasks", None)

    def _listed(self, result: dict) -> None:
        """Each tool marked read-only as JARVIS sorted it, and which run as tasks noted, then kept from the CLI."""
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
        """One request of the relay's own to the server, and its answer, with "result" or "error"."""
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
        except asyncio.CancelledError:  # the CLI cancelled it, or went
            run.cancel()
            raise
        finally:
            self.calls.pop(mid, None)
            if not call.background:
                self.by_token.pop(call.token, None)

    async def _run(self, call: Call, params: dict) -> dict:
        """Makes the call, as a task if the server runs it as one; its answer, with "result" or "error"."""
        call.request = self._id()
        try:
            if not (self.tasks and call.tool in self.task_tools):
                return await self.request("tools/call", params, call.request)
            made = await self.request("tools/call", params | {"task": {"ttl": TASK_TTL_MS}}, call.request)
            task = (made.get("result") or {}).get("task")
            if not isinstance(task, dict) or not task.get("taskId"):
                return made  # refused, or answered at once
            call.task = str(task["taskId"])
            while task.get("status") == "working":
                await asyncio.sleep(_poll(task))
                got = await self.request("tasks/get", {"taskId": call.task})
                if "error" in got:
                    return got
                task = got.get("result") or {}
                call.said = task.get("statusMessage") or call.said
            return await self.request("tasks/result", {"taskId": call.task})  # input_required waits there too
        except asyncio.CancelledError:
            self._spawn(self._cancel(call))
            raise
        except (OSError, ValueError, httpx.HTTPError) as e:  # the server gone
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

    # ── In the background ──────────────────────────────────

    async def _to_background(self, call: Call, run: asyncio.Task) -> str:
        """Hands a call still running to the task runner, as a connection task: what the CLI is told. '' if the runner
        can't take it, and the call is waited for after all."""
        title = f"{self.server.label} {call.tool.replace('_', ' ')}"
        instruction = f"{self.server.tool(call.tool)} {json.dumps(call.args, ensure_ascii=False)}"
        try:
            task = await self.runner.start_connection(title, instruction, os.getpid())
        except (client.RunnerUnavailable, ValueError, OSError) as e:
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
        """Until the call ends: its progress told to the runner, and the call cancelled if its task was stopped, or
        after BACKGROUND_HOURS. Then how it went."""
        told, until = "", time.monotonic() + BACKGROUND_HOURS * 3600
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
                        run.cancel()  # stopped: the runner has said so
                        return
                    if call.said != told:
                        await self.runner.connection_update(task_id, call.said)
                        told = call.said
                except (client.RunnerUnavailable, ValueError, OSError):
                    pass  # restarting: next time
            try:
                reply = run.result()
            except Exception as e:  # a fault of the relay's own: the task mustn't stay running
                log.exception("%s: %s failed", self.server.name, call.tool)
                reply = {"error": {"message": f"It failed ({type(e).__name__})."}}
            failed = "error" in reply or (reply.get("result") or {}).get("isError")
            await self._report(task_id, "failed" if failed else "done", result_text(reply))
        finally:
            self.by_token.pop(call.token, None)

    async def _report(self, task_id: int, outcome: str, result: str) -> None:
        for _ in range(REPORT_TRIES):
            try:
                await self.runner.end_connection(task_id, outcome, result)
                return
            except (client.RunnerUnavailable, OSError):
                await asyncio.sleep(FOLLOW_SECONDS)
            except ValueError:  # it's gone from the runner
                break
        log.warning("%s: task %s's end couldn't be told", self.server.name, task_id)

    # ── Ending ─────────────────────────────────────────────

    def closed(self) -> None:
        """The server has gone: what the relay asked it is answered so."""
        for future in self.pending.values():
            if not future.done():
                future.set_result({"error": {"code": ERROR, "message": f"{self.server.label} stopped."}})

    async def finish(self) -> None:
        """The CLI has gone: the calls it was waiting for stop, and those in the background carry on to their end."""
        for call in list(self.calls.values()):
            call.cancel()
        await asyncio.gather(*self.background, return_exceptions=True)


class Process:
    """A local server (stdio), started by the relay, each line it writes going to `deliver`."""

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
                transport: httpx.AsyncBaseTransport | None = None) -> int:
    """Relays stdin to the server and its answers to stdout until either ends, then waits for the calls carried on in
    the background. A local server is started with `command` (mcp_launch.command); an HTTP one is reached through the
    bridge. Raises vault.VaultError if a secret isn't set."""
    reader = reader or await mcp_bridge._stdin()
    out = out or sys.stdout
    lock = asyncio.Lock()

    async def write(message: dict) -> None:
        async with lock:
            try:
                out.write(json.dumps(message, ensure_ascii=False) + "\n")
                out.flush()
            except (BrokenPipeError, ValueError):  # the CLI has gone; calls in the background carry on
                pass

    relay = Relay(server, write, wait)
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
    """Carries on in a child process of a session of its own, so the CLI stopping the process it started as its
    session ends (SIGINT, then SIGTERM and SIGKILL, to that process alone) leaves calls in the background to finish.
    That process only waits for this one, and goes as soon as it's told to."""
    if pid := os.fork():
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, lambda *_: os._exit(0))
        _, status = os.waitpid(pid, 0)
        os._exit(max(os.waitstatus_to_exitcode(status), 0))
    os.setsid()
