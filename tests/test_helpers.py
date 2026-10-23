"""Tests for the helpers shown beside tasks: lookups (alfred.brain.side) and tool calls carrying on (alfred.brain.kept)
as the voice session reports them, the runner's list of them (alfred.tasks.helpers) and its API, stopping one from the
control panel as background_stop would, and task_list giving the running ones."""

import asyncio
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp import web
from test_kept import MERGE, run, settle, taking, unscreened  # noqa: F401  (run and unscreened are fixtures)
from test_voice_session import Brain, Runner, call as voice_call

from alfred.brain import side
from alfred.brain.kept import Kept
from alfred.contracts import call as contracts
from alfred.panel import tasks as panel_tasks
from alfred.state import conversation
from alfred.tasks import api, client, helpers as runner_helpers
from alfred.tasks.helpers import Helpers
from alfred.tools import side as side_tools, tasks as task_tools
from alfred.voice import session as voice_session


@pytest.fixture(autouse=True)
def no_runner(monkeypatch):
    """No task runner to reach unless a test fakes one."""
    async def start(*_a, **_k):
        raise client.RunnerUnavailable(client.DOWN)
    monkeypatch.setattr(client, "start", start)


def jobs(look, **parts) -> tuple[side.Jobs, list[str]]:
    said: list[str] = []
    return side.Jobs(lambda text, note: said.append(text), look, **parts), said


def lookup(steps: list[str] = (), answer: str = "Port 8080.", gate: asyncio.Event | None = None, fails: bool = False):
    """A fake lookup run: takes `steps` (as a session's would be noted), waits for `gate`, then answers or fails."""
    async def look(brief: str) -> str:
        side._steps.get().extend(steps)
        if gate is not None:
            await gate.wait()
        if fails:
            raise RuntimeError("the side job failed: error_during_execution")
        return answer
    return look


def view(h: contracts.Helper) -> tuple:
    v = h.view()
    return v["kind"], v["about"], v["status"], v["activity"]


# ── Lookups ─────────────────────────────────────────────────────


def test_a_lookup_is_listed_with_its_latest_step_while_it_runs_then_with_how_it_ended():
    async def main():
        gate = asyncio.Event()
        j, _ = jobs(lookup(["WebSearch: query Lisbon flights", "noted: Two airlines fly before nine."], gate=gate))
        j.start("Find morning flights to Lisbon.", "flights to Lisbon")
        await asyncio.sleep(0.01)
        running = [view(h) for h in j.listed()]
        gate.set()
        await asyncio.sleep(0.01)
        return running, [view(h) for h in j.listed()], j.listed()[0]

    running, ended, helper = asyncio.run(main())
    assert running == [("background_lookup", "flights to Lisbon", "running", "noted: Two airlines fly before nine.")]
    assert ended == [("background_lookup", "flights to Lisbon", "done", "noted: Two airlines fly before nine.")]
    assert helper.started <= helper.ended <= time.time()


def test_a_failed_lookup_is_listed_as_failed_and_a_stopped_one_as_stopped():
    async def main():
        failing, _ = jobs(lookup(fails=True))
        failing.start("x", "the weather")
        stopping, _ = jobs(lookup(gate=asyncio.Event()))
        stopping.start("y", "the news")
        await asyncio.sleep(0.01)
        [job] = stopping.running
        await stopping.stop(job)
        return [view(h)[2] for h in failing.listed()], [view(h)[2] for h in stopping.listed()]

    assert asyncio.run(main()) == (["failed"], ["stopped"])


def test_a_lookup_out_of_time_is_listed_as_timed_out_saying_which_task_carries_it_on(monkeypatch):
    monkeypatch.setattr(side, "SIDE_MINUTES", 0.002)

    async def start(kind, title, instruction, project, extra) -> dict:
        return {"id": 12}

    async def main():
        j, _ = jobs(lookup(gate=asyncio.Event()), start_task=start)
        j.start("slow", "flights to Lisbon")
        await asyncio.sleep(0.4)
        return [view(h) for h in j.listed()]

    assert asyncio.run(main()) == [("background_lookup", "flights to Lisbon", "timed_out", "carries on as task 12")]


def test_a_finished_lookup_drops_off_the_list_after_a_while(monkeypatch):
    async def main():
        j, _ = jobs(lookup())
        j.start("x", "the port")
        await asyncio.sleep(0.01)
        shown = len(j.listed())
        monkeypatch.setattr(contracts, "HELPER_LINGER", 0)
        return shown, j.listed()

    assert asyncio.run(main()) == (1, [])


# ── Tool calls carrying on ──────────────────────────────────────


def test_a_tool_call_is_listed_once_it_carries_on_outside_its_turn_then_with_its_outcome(run):  # noqa: F811
    async def main():
        kept = Kept(seconds=0.01)
        taking(kept)
        run.gate = asyncio.Event()
        await kept.run(MERGE, {"id": 5})
        running = [view(h) for h in kept.listed()]
        run.gate.set()
        await settle()
        return running, [view(h) for h in kept.listed()]

    running, ended = asyncio.run(main())
    assert running == [("tool_call", "task_merge (id 5)", "running", None)]
    assert ended == [("tool_call", "task_merge (id 5)", "done", None)]


def test_a_quick_tool_call_never_shows_and_a_stopped_one_shows_as_stopped(run):  # noqa: F811
    async def main():
        kept = Kept(seconds=1)
        taking(kept)
        await kept.run(MERGE, {"id": 4})
        quick = kept.listed()
        kept.seconds = 0.01
        run.gate = asyncio.Event()
        await kept.run(MERGE, {"id": 5})
        await kept.stop(kept.going())
        return quick, [view(h)[2] for h in kept.listed()]

    assert asyncio.run(main()) == ([], ["stopped"])


# ── Stopping one by its id, as background_stop would ────────────


def test_a_helper_is_stopped_by_its_id_the_way_background_stop_stops_it():
    async def main():
        j, said = jobs(lookup(gate=asyncio.Event()))
        talk = conversation.begin()
        talk.lookups = j
        j.start("x", "the deploy port")
        await asyncio.sleep(0.01)
        [helper] = j.listed()
        missing = await side_tools.stop_helper(talk, "0000")
        stopped = await side_tools.stop_helper(talk, helper.id)
        return missing, stopped, helper.view()["status"], said, j.running

    missing, stopped, status, said, running = asyncio.run(main())
    assert missing is None and stopped == "Stopped the lookup on the deploy port; nothing is announced from it."
    assert status == "stopped" and said == [] and not running


# ── The runner's list ───────────────────────────────────────────


def helper(id: str, started: float, status: str = "running", about: str = "the port") -> dict:
    return {"id": id, "kind": "file_lookup", "about": about, "status": status, "activity": None, "started": started,
            "ended": None if status == "running" else started + 1}


def test_the_runner_lists_every_conversation_s_helpers_oldest_first_and_forgets_an_ended_conversation():
    h = Helpers()
    assert h.report("call-a", [helper("a1", 30)]) and h.report("call-b", [helper("b1", 10)])
    assert [x["id"] for x in h.listed()] == ["b1", "a1"]
    assert not h.report("call-a", [helper("a1", 30)])  # a heartbeat with nothing new changes nothing
    assert h.report("call-b", []) and [x["id"] for x in h.listed()] == ["a1"]


def test_a_conversation_not_heard_from_for_a_while_is_forgotten(monkeypatch):
    clock = SimpleNamespace(monotonic=lambda: 100.0)
    monkeypatch.setattr(runner_helpers, "time", clock)
    h = Helpers()
    h.report("call-a", [helper("a1", 1)])
    clock.monotonic = lambda: 100.0 + runner_helpers.STALE + 1
    assert h.listed() == []


def test_one_asked_to_stop_shows_as_stopping_until_its_conversation_says_it_stopped():
    h = Helpers()
    h.report("call-a", [helper("a1", 1)])
    assert h.stop("a1")["status"] == "stopping"
    h.report("call-a", [helper("a1", 1)])  # a heartbeat from before the stop took
    assert h.listed()[0]["status"] == "stopping"
    h.report("call-a", [helper("a1", 1, "stopped")])
    assert h.listed()[0]["status"] == "stopped"
    with pytest.raises(ValueError, match="isn't running"):
        h.stop("a1")
    with pytest.raises(ValueError, match="isn't running"):
        h.stop("zz")


def test_the_runner_api_takes_reports_and_sends_a_stop_to_the_conversations(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)  # socket paths must be short (104 bytes on macOS)
    monkeypatch.setattr(client, "RUNNER_SOCKET", Path("runner.sock"))
    sent: list[dict] = []
    fake = SimpleNamespace(helpers=Helpers(), broadcast=sent.append)

    async def main():
        app = web.Application(middlewares=[api.errors])
        app.add_routes(api.routes(fake))  # type: ignore[arg-type]
        site = web.AppRunner(app)
        await site.setup()
        try:
            try:
                await web.UnixSite(site, str(client.RUNNER_SOCKET)).start()
            except PermissionError as e:
                pytest.skip(f"can't serve a Unix socket here (a sandbox may not allow it): {e}")
            await client.report_helpers("call-a", [helper("a1", 1)])
            await client.report_helpers("call-a", [helper("a1", 1)])
            listed = await client.helpers()
            stopping = await panel_tasks.helper_stop({"id": "a1"})
            with pytest.raises(ValueError, match="isn't running"):
                await client.stop_helper("b2")
            with pytest.raises(ValueError, match="Say which one"):
                await panel_tasks.helper_stop({"id": "../tasks/1"})
        finally:
            await site.cleanup()
        return listed, stopping

    listed, stopping = asyncio.run(main())
    assert [x["id"] for x in listed] == ["a1"] and stopping["status"] == "stopping"
    assert [e["type"] for e in sent] == ["helpers", "helper_stop", "helpers"]  # the repeat report changed nothing
    assert sent[1] == {"type": "helper_stop", "id": "a1"} and sent[2]["helpers"][0]["status"] == "stopping"


# ── The voice session ───────────────────────────────────────────


class Reports(Runner):
    """Fake runner that keeps the helpers each report sends."""

    def __init__(self) -> None:
        super().__init__()
        self.reports: list[list[dict]] = []

    async def report_helpers(self, session: str, found: list[dict]) -> None:
        self.reports.append(found)


def test_the_call_reports_its_helpers_when_they_change_and_takes_them_off_when_it_ends(monkeypatch):
    monkeypatch.setattr(voice_session, "HELPERS_TICK", 0.005)

    async def main():
        runner = Reports()
        voice = voice_call(runner=runner)
        voice.open()
        reporting = asyncio.create_task(voice.report_helpers())
        gate = asyncio.Event()
        voice.lookups.run = lookup(gate=gate)
        voice.lookups.start("x", "the news")
        await asyncio.sleep(0.05)
        gate.set()
        await asyncio.sleep(0.05)
        reporting.cancel()
        await voice.close()
        return runner.reports

    reports = asyncio.run(main())
    assert [[(h["about"], h["status"]) for h in r] for r in reports] == [
        [("the news", "running")], [("the news", "done")], []]  # only on changes; emptied as the call ends


def test_a_stop_from_the_panel_stops_the_call_s_own_lookup_and_tells_the_brain(monkeypatch):
    monkeypatch.setattr(voice_session, "HELPERS_TICK", 0.005)

    async def main():
        brain = Brain()
        voice = voice_call(brain)
        voice.open()
        voice.lookups.run = lookup(gate=asyncio.Event())
        voice.lookups.start("x", "the news")
        voice.lookups.start("y", "the weather")
        await asyncio.sleep(0.01)
        news, weather = voice.lookups.listed()
        voice._task_event({"type": "helper_stop", "id": "not-ours"})
        voice._task_event({"type": "helper_stop", "id": news.id})
        await asyncio.sleep(0.02)
        statuses = [h.view()["status"] for h in (news, weather)]
        await voice.close()
        return statuses, brain.notes

    statuses, notes = asyncio.run(main())
    assert statuses == ["stopped", "running"]
    assert len(notes) == 1 and "stopped it from the control panel" in notes[0] and "the news" in notes[0]


# ── task_list ───────────────────────────────────────────────────


def test_task_list_gives_the_running_helpers_after_the_tasks(monkeypatch):
    task = {"id": 3, "title": "Look", "kind": "research", "status": "running", "seen": 1, "updated": time.time()}

    async def tasks(unseen=False):
        return [task]

    async def listed():
        return [helper("a1", time.time() - 90, about="the deploy port") | {"activity": "Grep: pattern PORT"},
                helper("b2", time.time() - 5, "done", about="the news")]

    async def nothing(*_a):
        return []

    monkeypatch.setattr(client, "tasks", tasks)
    monkeypatch.setattr(client, "helpers", listed)
    monkeypatch.setattr(client, "approvals", nothing)
    monkeypatch.setattr(client, "mark_seen", nothing)
    out = asyncio.run(task_tools.task_list())
    assert [t.get("id") for t in out] == [3, None]  # the finished helper was announced already
    found = out[1]
    assert (found["helper"], found["about"], found["status"], found["activity"]) == (
        "file_lookup", "the deploy port", "running", "Grep: pattern PORT")
    assert 89 <= found["running_seconds"] <= 92
