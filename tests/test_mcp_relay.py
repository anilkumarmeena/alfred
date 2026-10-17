"""Tests for mcp_relay and connection_jobs: read-only marks, MCP tasks, progress, cancelling, background calls."""

import asyncio
import json
import os
import sys
import textwrap

import pytest
from test_approvals import drained, events

from conftest import use_profile
from jarvis import config, tools
from jarvis.brain import sdk
from jarvis.integrations import mcp_relay, mcp_servers
from jarvis.integrations.mcp_relay import Relay
from jarvis.integrations.mcp_servers import Server, Tool
from jarvis.llm import claude
from jarvis.tasks import client, connection_jobs, events as task_events, runner
from jarvis.tasks.store import CONNECTION

SERVER = Server("acme", "Acme", tools={"find": Tool("read"), "report": Tool("read"), "post": Tool("write", "post")})
LISTED = [{"name": "find", "annotations": {"title": "Find"}},
          {"name": "report", "execution": {"taskSupport": "optional"}},
          {"name": "post", "annotations": {"readOnlyHint": True}},  # server: read-only; JARVIS sorted it a write
          {"name": "other"}]


class FakeServer:
    """Fake MCP server: answers relay messages, holding each tools/call until `release`d."""

    def __init__(self, relay: Relay, tasks: bool = False, statuses: tuple[str, ...] = ("working", "completed")) -> None:
        self.relay, self.tasks, self.statuses = relay, tasks, list(statuses)
        self.got: list[dict] = []
        self.release = asyncio.Event()
        relay.send = self.send

    async def answer(self, mid, result: dict) -> None:
        await self.relay.from_server({"jsonrpc": "2.0", "id": mid, "result": result})

    async def send(self, m: dict) -> None:
        self.got.append(m)
        method, mid, params = m.get("method"), m.get("id"), m.get("params") or {}
        if method == "initialize":
            tasks = {"tasks": {"list": {}, "cancel": {}, "requests": {"tools": {"call": {}}}}} if self.tasks else {}
            await self.answer(mid, {"protocolVersion": "2025-11-25", "capabilities": {"tools": {}} | tasks})
        elif method == "tools/list":
            await self.answer(mid, {"tools": json.loads(json.dumps(LISTED))})
        elif method == "tools/call" and "task" in params:
            await self.answer(mid, {"task": {"taskId": "t-1", "status": "working", "pollInterval": 10}})
        elif method == "tools/call":
            asyncio.create_task(self.finish(mid, params))
        elif method == "tasks/get":
            status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
            await self.answer(mid, {"taskId": "t-1", "status": status, "statusMessage": f"{status} on it"})
        elif method == "tasks/result":
            await self.answer(mid, {"content": [{"type": "text", "text": "3 reports"}]})
        elif method == "tasks/cancel":
            await self.answer(mid, {"taskId": "t-1", "status": "cancelled"})

    async def finish(self, mid, params: dict) -> None:
        token = params["_meta"]["progressToken"]
        await self.relay.from_server({"jsonrpc": "2.0", "method": "notifications/progress",
                                      "params": {"progressToken": token, "progress": 1, "total": 4}})
        await self.release.wait()
        await self.answer(mid, {"content": [{"type": "text", "text": f"found {params['arguments']['q']}"}]})


class FakeRunner:
    def __init__(self, down: bool = False) -> None:
        self.down, self.status = down, "running"
        self.started: list[tuple] = []
        self.updates: list[str] = []
        self.ended: list[tuple[str, str]] = []

    async def start_connection(self, title, instruction, pid):
        if self.down:
            raise client.RunnerUnavailable("down")
        self.started.append((title, instruction, pid))
        return {"id": 7}

    async def task(self, task_id):
        return {"id": task_id, "status": self.status}

    async def connection_update(self, task_id, activity):
        self.updates.append(activity)

    async def end_connection(self, task_id, outcome, result):
        self.ended.append((outcome, result))


def relay(wait: float = 0.0, runner_=None, **server) -> tuple[Relay, FakeServer, list[dict]]:
    """A relay whose CLI-side output is collected in a list."""
    said: list[dict] = []

    async def to_cli(m):
        said.append(m)

    r = Relay(SERVER, to_cli, wait, runner_ or FakeRunner())
    return r, FakeServer(r, **server), said


def call(mid: int, q: str = "invoices", tool: str = "find", token=None) -> dict:
    meta = {"_meta": {"progressToken": token}} if token is not None else {}
    return {"jsonrpc": "2.0", "id": mid, "method": "tools/call", "params": {"name": tool, "arguments": {"q": q}} | meta}


async def settle(r: Relay) -> None:
    for _ in range(50):
        await asyncio.sleep(0.01)
        if not r.calls:
            return


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    monkeypatch.setattr(mcp_relay, "FOLLOW_SECONDS", 0.02)
    monkeypatch.setattr(mcp_relay, "POLL_LIMITS", (0.01, 0.05))


# ── What the CLI sees ───────────────────────────────────────────

def test_tools_are_marked_read_only_as_jarvis_sorted_them_and_tasks_are_kept_from_the_cli():
    async def main():
        r, server, said = relay(tasks=True)
        await r.from_cli({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}})
        await r.from_cli({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        await asyncio.sleep(0.01)
        return r, said

    r, said = asyncio.run(main())
    assert said[0]["result"]["capabilities"] == {"tools": {}}  # the relay runs the tasks
    listed = {t["name"]: t for t in said[1]["result"]["tools"]}
    assert listed["find"]["annotations"] == {"title": "Find", "readOnlyHint": True}
    assert listed["report"]["annotations"] == {"readOnlyHint": True} and listed["report"]["execution"] == {}
    assert listed["post"]["annotations"] == {"readOnlyHint": False}  # held for approval, so one at a time
    assert "annotations" not in listed["other"]  # unsorted: refused anyway
    assert r.tasks and r.task_tools == {"report"}


def test_a_call_passes_with_its_progress_under_the_clis_own_token():
    async def main():
        r, server, said = relay()
        await r.from_cli(call(5, token="cli-1"))
        await asyncio.sleep(0.01)
        server.release.set()
        await settle(r)
        return server, said

    server, said = asyncio.run(main())
    sent = server.got[0]
    assert sent["method"] == "tools/call" and sent["id"].startswith("jarvis-")  # the relay's own id
    assert sent["params"]["_meta"]["progressToken"].startswith("jarvis-") and "task" not in sent["params"]
    assert said[0] == {"jsonrpc": "2.0", "method": "notifications/progress",
                       "params": {"progressToken": "cli-1", "progress": 1, "total": 4}}
    assert said[1] == {"jsonrpc": "2.0", "id": 5, "result": {"content": [{"type": "text", "text": "found invoices"}]}}


def test_a_tool_the_server_runs_as_a_task_is_followed_to_its_result():
    async def main():
        r, server, said = relay(tasks=True, statuses=("working", "working", "completed"))
        await r.from_cli({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}})
        await r.from_cli({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        await r.from_cli(call(2, tool="report"))
        await settle(r)
        return server, said

    server, said = asyncio.run(main())
    methods = [m["method"] for m in server.got]
    assert methods == ["initialize", "tools/list", "tools/call", "tasks/get", "tasks/get", "tasks/get", "tasks/result"]
    assert server.got[2]["params"]["task"]["ttl"] == mcp_relay.TASK_TTL_MS
    assert said[-1] == {"jsonrpc": "2.0", "id": 2, "result": {"content": [{"type": "text", "text": "3 reports"}]}}


def test_a_tool_that_isnt_a_task_on_a_server_that_runs_them_is_a_plain_call():
    async def main():
        r, server, said = relay(tasks=True)
        await r.from_cli({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}})
        await r.from_cli({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        await r.from_cli(call(2))
        await asyncio.sleep(0.01)
        server.release.set()
        await settle(r)
        return server

    assert "task" not in asyncio.run(main()).got[2]["params"]


def test_a_call_the_cli_cancels_is_cancelled_at_the_server():
    async def main():
        r, server, said = relay()
        await r.from_cli(call(5))
        await asyncio.sleep(0.01)
        await r.from_cli({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 5}})
        await settle(r)
        await asyncio.sleep(0.01)
        return server, said

    server, said = asyncio.run(main())
    cancelled = server.got[-1]
    assert cancelled["method"] == "notifications/cancelled"
    assert cancelled["params"]["requestId"] == server.got[0]["id"]  # the relay's id for it, not the CLI's
    assert not [m for m in said if m.get("id") == 5]  # a cancelled request isn't answered


def test_the_servers_own_requests_and_the_clis_answers_pass_through():
    async def main():
        r, server, said = relay()
        await r.from_server({"jsonrpc": "2.0", "id": 0, "method": "roots/list"})
        await r.from_cli({"jsonrpc": "2.0", "id": 0, "result": {"roots": []}})
        await asyncio.sleep(0.01)
        return server, said

    server, said = asyncio.run(main())
    assert said == [{"jsonrpc": "2.0", "id": 0, "method": "roots/list"}]
    assert server.got == [{"jsonrpc": "2.0", "id": 0, "result": {"roots": []}}]


# ── In the background ───────────────────────────────────────────

def test_a_slow_call_carries_on_as_a_task_said_when_it_ends():
    fake = FakeRunner()

    async def main():
        r, server, said = relay(wait=0.05, runner_=fake)
        await r.from_cli(call(5))
        await settle(r)
        answered = list(said)
        await asyncio.sleep(0.05)  # the runner hears its progress
        server.release.set()
        await r.finish()  # CLI gone: background work ends first
        return answered

    answered = asyncio.run(main())
    text = answered[-1]["result"]["content"][0]["text"]
    assert answered[-1]["id"] == 5 and "task 7, Acme find" in text and "25% done" in text and "Don't call it again" in text
    title, instruction, pid = fake.started[0]
    assert (title, pid) == ("Acme find", os.getpid()) and instruction == 'mcp__acme__find {"q": "invoices"}'
    assert fake.updates == ["25% done"]
    assert fake.ended == [("done", "found invoices")]


def test_a_task_stopped_in_the_runner_cancels_its_call():
    fake = FakeRunner()

    async def main():
        r, server, said = relay(wait=0.02, runner_=fake, tasks=True, statuses=("working",))
        await r.from_cli({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}})
        await r.from_cli({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        await r.from_cli(call(2, tool="report"))
        await settle(r)
        await asyncio.sleep(0.06)  # the runner hears how it goes
        fake.status = "stopped"
        await r.finish()
        await asyncio.sleep(0.02)
        return server

    server = asyncio.run(main())
    assert server.got[-1]["method"] == "tasks/cancel" and server.got[-1]["params"] == {"taskId": "t-1"}
    assert fake.updates == ["working on it"] and fake.ended == []  # the runner said it stopped


def test_a_failed_call_in_the_background_ends_its_task_as_failed():
    fake = FakeRunner()

    async def main():
        r, server, said = relay(wait=0.02, runner_=fake)

        async def refuse(mid, params):
            await asyncio.sleep(0.05)
            await r.from_server({"jsonrpc": "2.0", "id": mid, "result": {"isError": True, "content": [
                {"type": "text", "text": "rate limited"}]}})

        server.finish = refuse
        await r.from_cli(call(5))
        await settle(r)
        await r.finish()

    asyncio.run(main())
    assert fake.ended == [("failed", "rate limited")]


def test_with_the_runner_down_a_slow_call_is_waited_for():
    async def main():
        r, server, said = relay(wait=0.02, runner_=FakeRunner(down=True))
        await r.from_cli(call(5))
        await asyncio.sleep(0.05)
        server.release.set()
        await settle(r)
        return said

    assert asyncio.run(main())[-1]["result"]["content"][0]["text"] == "found invoices"


def test_results_read_as_text():
    assert mcp_relay.result_text({"error": {"code": -1, "message": "nope"}}) == "nope"
    assert mcp_relay.result_text({"result": {"content": [{"type": "text", "text": "a"}, {"type": "image"}]}}) == "a\n[image]"
    assert mcp_relay.result_text({"result": {"content": [], "structuredContent": {"n": 2}}}) == '{"n": 2}'


# ── A real local server, relayed ────────────────────────────────

FAKE_SERVER = textwrap.dedent("""
    import json, sys
    for line in sys.stdin:
        m = json.loads(line)
        if "id" not in m:
            continue
        if m["method"] == "initialize":
            result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}}
        elif m["method"] == "tools/list":
            result = {"tools": [{"name": "find", "inputSchema": {"type": "object"}}]}
        else:
            result = {"content": [{"type": "text", "text": "echo " + m["params"]["arguments"]["q"]}]}
        print(json.dumps({"jsonrpc": "2.0", "id": m["id"], "result": result}), flush=True)
""")


def test_the_launcher_relays_a_local_server_and_goes_when_the_cli_does(tmp_path):
    script = tmp_path / "server.py"
    script.write_text(FAKE_SERVER)
    mcp_servers.save(Server("echo", "Echo", command=sys.executable, args=[str(script)],
                            tools={"find": Tool("read")}), new=True)
    sent = ({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, call(2))

    async def main():
        launcher = mcp_servers.launcher("echo", "--wait=5")  # detached, as the voice brain's CLI starts it
        proc = await asyncio.create_subprocess_exec(
            launcher["command"], *launcher["args"], cwd=config.ROOT, env=os.environ | launcher["env"],
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE)
        proc.stdin.write(b"".join(json.dumps(m).encode() + b"\n" for m in sent))
        got = [json.loads(await asyncio.wait_for(proc.stdout.readline(), 20)) for _ in range(3)]
        proc.stdin.close()  # CLI exits: with nothing in the background, the relay exits too
        await asyncio.wait_for(proc.wait(), 20)
        return got, proc.returncode

    got, code = asyncio.run(main())
    answers = {m["id"]: m["result"] for m in got}
    assert answers[1]["tools"][0]["annotations"] == {"readOnlyHint": True}
    assert answers[2] == {"content": [{"type": "text", "text": "echo invoices"}]} and code == 0
    mcp_servers.remove("echo")


# ── The runner's connection tasks ───────────────────────────────

@pytest.fixture
def summed(monkeypatch, tmp_path):
    """Isolated task database, scripted summing up, no desktop notification or push."""
    use_profile(monkeypatch, tmp_path)
    asked = []

    async def ask(prompt, system, model, **options):
        asked.append(prompt)
        return "Found three overdue invoices.", 0.0

    async def no_notify(title, text):
        pass

    monkeypatch.setattr(claude, "ask", ask)
    monkeypatch.setattr(task_events, "mac_notify", no_notify)
    monkeypatch.setattr(task_events, "PUSH_ENABLED", False)
    return asked


def test_a_connection_task_is_in_the_list_and_said_when_its_call_ends(summed):
    async def main():
        r = runner.Runner()
        q = events(r)
        task = r.start_connection("Acme find", 'mcp__acme__find {"q": "invoices"}', os.getpid())
        assert (task["kind"], task["status"], r.view(task)["stalled"]) == (CONNECTION, "running", None)
        assert r.connection_update(task["id"], "25% done")["activity"] == "25% done"
        with pytest.raises(ValueError, match="nothing to carry on"):
            r.reply(task["id"], "and the rest")
        done = await r.end_connection(task["id"], "done", "3 invoices: #12, #14, #19")
        return done, drained(q)

    done, said = asyncio.run(main())
    assert done["status"] == "done" and done["summary"] == "3 invoices: #12, #14, #19"
    assert said[-1]["text"] == f"Task {done['id']}, Acme find, is done, after 0 seconds. Found three overdue invoices."
    assert not said[-1]["task"]["seen"]  # announced
    assert "#12" in summed[0] and "mcp__acme__find" in summed[0]


def test_a_stopped_connection_task_stays_stopped_when_its_call_ends(summed):
    async def main():
        r = runner.Runner()
        task = r.start_connection("Acme report", "mcp__acme__report {}", os.getpid())
        stopped = await r.halt(task["id"])
        return stopped, await r.end_connection(task["id"], "done", "late")

    stopped, after = asyncio.run(main())
    assert stopped["status"] == after["status"] == "stopped" and after["summary"] != "late"


def test_a_connection_task_whose_relay_is_gone_fails_and_survives_a_runner_restart(summed):
    async def main():
        r = runner.Runner()
        lost = r.start_connection("Acme find", "mcp__acme__find {}", 2 ** 22 + 12345)  # no such process
        kept = r.start_connection("Acme report", "mcp__acme__report {}", os.getpid())
        assert runner.Runner().store.get(kept["id"])["status"] == "running"  # a restart leaves the relay's call alone
        assert connection_jobs.alive(kept) and not connection_jobs.alive(lost)
        r.check_connections()
        assert r.store.get(lost["id"])["status"] == "failed" and r.store.get(kept["id"])["status"] == "running"
        with pytest.raises(ValueError, match="isn't a call on a connection"):
            r.connection_update(r.store.create("research", "x", "y", None)["id"], "z")
        await r.halt(kept["id"])

    asyncio.run(main())


# ── JARVIS's own tools ──────────────────────────────────────────

def test_only_tools_that_change_nothing_are_marked_read_only_for_the_cli():
    reads = {t.name for t in tools.all_tools() if t.reads}
    assert {"task_list", "todo_list", "file_read", "conversation_search", "schedule_list"} <= reads
    assert not reads & {"task_start", "task_stop", "remember", "browser_open", "voice_enroll"}
    assert not [t.name for t in tools.all_tools() if t.reads and (t.gated or t.changes or t.shell)]
    assert sdk._sdk_tool(tools.get("task_list")).annotations is claude.READS
    assert sdk._sdk_tool(tools.get("task_start")).annotations is None
