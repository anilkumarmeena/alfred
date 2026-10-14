"""Tests for jarvis.panel and its routes: to-dos, memory, preferences, sources, accounts, projects, tasks, jobs."""

import asyncio
import json
from datetime import date

import pytest
from test_in_place import env  # noqa: F401  (fixture)

from conftest import use_profile
from jarvis import panel, server
from jarvis.core import config
from jarvis.core.database import APPROVED
from jarvis.integrations import accounts
from jarvis.notify import push
from jarvis.safety.commands import describe
from jarvis.state import memory, settings, todo
from jarvis.tasks import api, client, runner, schedules

TODAY = f"{date.today():%Y-%m-%d}"
IPHONE = {"endpoint": "https://web.push.apple.com/abc", "keys": {"p256dh": "B" * 87, "auth": "A" * 22}}


@pytest.fixture
def db(tmp_path, monkeypatch):
    use_profile(monkeypatch, tmp_path)
    return tmp_path


def call(name: str, body: dict | None = None):
    return asyncio.run(panel.call(name, body or {}))


# ── To-dos ──────────────────────────────────────────────────────


def test_todos_by_id_open_first_then_the_latest_done(db):
    for item in ("One", "Two", "Three"):
        todo.add(item)
    one, two, three = (i["id"] for i in todo.items())
    todo.check(one)
    todo.check(three)
    assert [(i["text"], i["done"]) for i in todo.items()] == [("Two", False), ("Three", True), ("One", True)]
    todo.check(three, done=False)
    todo.edit(two, "  Two,   rewritten ")
    todo.delete(one)
    assert [(i["text"], i["done"], i["done_at"] is None) for i in todo.items()] == [("Two, rewritten", False, True),
                                                                                  ("Three", False, True)]


def test_todo_changes_name_an_item_that_exists_and_say_something(db):
    with pytest.raises(ValueError, match="no to-do 99"):
        todo.check(99)
    todo.add("One")
    with pytest.raises(ValueError, match="empty"):
        todo.edit(todo.items()[0]["id"], "   ")


def test_the_panel_s_todo_actions_return_the_list(db):
    added = call("todo_add", {"text": "Call the bank"})
    assert [i["text"] for i in added] == ["Call the bank"]
    ticked = call("todo_check", {"id": added[0]["id"], "done": True})
    assert ticked[0]["done"] is True
    assert call("todo_delete", {"id": added[0]["id"]}) == []
    with pytest.raises(ValueError, match="id is missing"):
        call("todo_check", {})


def test_undoing_a_delete_puts_the_to_do_back_in_its_place(db):
    first, second = (call("todo_add", {"text": t})[-1] for t in ("Call the bank", "Book the car service"))
    call("todo_delete", {"id": first["id"]})
    back = call("todo_restore", first)
    assert [(i["id"], i["text"], i["created"]) for i in back] == [(first["id"], "Call the bank", first["created"]),
                                                                   (second["id"], "Book the car service", second["created"])]


def test_a_restored_to_do_whose_id_was_taken_goes_at_the_end(db):
    gone = call("todo_add", {"text": "Call the bank"})[-1]
    call("todo_delete", {"id": gone["id"]})
    taken = call("todo_add", {"text": "Renew the domain"})[-1]  # may reuse the freed id
    back = call("todo_restore", gone | {"id": taken["id"]})
    assert [i["text"] for i in back] == ["Renew the domain", "Call the bank"]


def test_a_done_to_do_comes_back_done(db):
    item = call("todo_add", {"text": "Call the bank"})[-1]
    done = call("todo_check", {"id": item["id"], "done": True})[0]
    call("todo_delete", {"id": item["id"]})
    (back,) = call("todo_restore", done)
    assert back["done"] is True and back["done_at"] == done["done_at"]


# ── Memory and "about you" ──────────────────────────────────────


def shown(user: list[str], environment: list[str]) -> dict:
    """Expected memory-action result for these lines."""
    return {"user": user, "environment": environment,
            "limits": {"user": {"used": sum(map(len, user)), "max": 2000},
                       "environment": {"used": sum(map(len, environment)), "max": 3000}, "fact": 400}}


def test_memory_lines_are_edited_and_forgotten_exactly(db):
    memory.remember("Likes tea", "user")
    memory.remember("Likes tea, not coffee", "user")
    memory.remember("Runs JARVIS on a Mac mini", "environment")
    first, second = memory.facts()["user"]
    mac = f"- Runs JARVIS on a Mac mini ({TODAY})"
    assert first == f"- Likes tea ({TODAY})"
    assert call("memory") == shown([first, second], [mac])
    assert call("memory_edit", {"line": first, "fact": "Likes green tea"}) == shown([f"- Likes green tea ({TODAY})", second], [mac])
    assert call("memory_edit", {"line": second, "fact": ""}) == shown([f"- Likes green tea ({TODAY})"], [mac])  # only that line
    with pytest.raises(ValueError, match="changed or gone"):
        call("memory_edit", {"line": second, "fact": "again"})
    assert call("memory_edit", {"line": mac, "fact": "Runs JARVIS on an M2 MacBook"})["environment"] == \
        [f"- Runs JARVIS on an M2 MacBook ({TODAY})"]  # in its own section


def test_remember_from_the_panel_names_the_section_and_keeps_to_the_limits(db):
    assert call("remember", {"fact": "  The sync is   on Tuesdays ", "section": "user"}) == \
        shown([f"- The sync is on Tuesdays ({TODAY})"], [])
    assert call("remember", {"fact": "Uses zsh", "section": "environment"})["environment"] == [f"- Uses zsh ({TODAY})"]
    with pytest.raises(ValueError, match="empty"):
        call("remember", {"fact": " ", "section": "user"})
    with pytest.raises(ValueError, match="user or environment"):
        call("remember", {"fact": "Likes tea"})
    with pytest.raises(ValueError, match="at most 400 characters"):
        call("remember", {"fact": "x" * 390, "section": "user"})
    for n in range(5):
        call("remember", {"fact": f"{n} {'y' * 375}", "section": "user"})
    with pytest.raises(ValueError, match=r"^About .+ is full: this would make it [\d,]+ of 2,000 characters\. "
                                         r"Shorten or forget some of its facts first\.$"):
        call("remember", {"fact": "One more", "section": "user"})
    with pytest.raises(ValueError, match="is full"):  # an edit that grows a full section too
        call("memory_edit", {"line": f"- The sync is on Tuesdays ({TODAY})", "fact": "The sync is on Tuesdays " + "z" * 300})


def test_about_from_the_panel(db):
    before = settings.about()
    try:
        assert call("about_save", {"text": " Engineer. \n\n Two accounts. "}) == {"text": "Engineer.\nTwo accounts."}
        assert call("about") == {"text": "Engineer.\nTwo accounts."}
    finally:
        settings.set_about(before)


# ── Preferences ─────────────────────────────────────────────────


@pytest.fixture
def prefs(db, monkeypatch):
    """Scratch database, no preference set in .env."""
    for var in config.PREFERENCES.values():
        monkeypatch.delenv(var, raising=False)
    return db


def test_preferences_kept_from_the_panel_and_read_by_config(prefs):
    shown = call("preferences")
    assert shown["preferences"]["voice"] == {"value": "", "default": "bm_george", "env": ""}
    assert "bm_george" in shown["voices"] and all(v[:2] in ("af", "am", "bf", "bm") for v in shown["voices"])
    assert config.tts_voice() == "bm_george" and config.speech_gate() == "enforce"

    call("preference_save", {"key": "voice_speed", "value": "1.10"})
    shown = call("preference_save", {"key": "speech_gate", "value": "Shadow"})
    assert shown["preferences"]["voice_speed"]["value"] == "1.1" and shown["preferences"]["speech_gate"]["value"] == "shadow"
    assert (config.tts_speed(), config.speech_gate()) == (1.1, "shadow")

    call("preference_save", {"key": "voice_speed", "value": ""})  # back to the default
    assert config.tts_speed() == 1.05 and settings.get("voice_speed") == ""


def test_quiet_hours_kept_from_the_panel(prefs):
    assert call("preferences")["preferences"]["quiet_hours"] == {"value": "", "default": "23:00-08:00", "env": ""}
    shown = call("preference_save", {"key": "quiet_hours", "value": " 22:30 - 7:00 "})
    assert shown["preferences"]["quiet_hours"]["value"] == "22:30-07:00"
    shown = call("preference_save", {"key": "quiet_hours", "value": "No"})
    assert shown["preferences"]["quiet_hours"]["value"] == "off"


@pytest.mark.skipif(not settings.VOICES_FILE.is_file(), reason="needs Kokoro's voices file (make models)")
def test_a_voice_picked_in_the_panel_is_spoken_unless_env_sets_one(prefs, monkeypatch):
    assert {"bm_george", "bm_lewis", "af_heart"} <= set(call("preferences")["voices"])
    call("preference_save", {"key": "voice", "value": "bm_lewis"})
    assert config.tts_voice() == "bm_lewis"

    monkeypatch.setenv("JARVIS_TTS_VOICE", "af_heart")  # .env wins
    assert config.tts_voice() == "af_heart" and call("preferences")["preferences"]["voice"]["env"] == "af_heart"


@pytest.mark.parametrize("key, value", [("timezone", "Mars/Olympus"), ("voice", "zz_nobody"), ("voice_speed", "5"),
                                        ("speech_gate", "loud"), ("max_tasks", "0"), ("max_tasks", "two"),
                                        ("name", "x" * 41), ("colour", "blue"), ("quiet_hours", "late"),
                                        ("quiet_hours", "10:00-10:00"), ("quiet_hours", "22:00:10-22:00:50")])
def test_a_preference_it_cant_take_is_refused(prefs, key, value):
    with pytest.raises(ValueError):
        call("preference_save", {"key": key, "value": value})


def test_name_and_time_zone_default_to_the_macs(prefs):
    shown = call("preferences")["preferences"]
    assert shown["name"]["default"] == (config.mac_name() or "the user")
    assert shown["timezone"]["default"] == (config.mac_timezone() or "UTC")
    assert not config.mac_timezone() or config.zone_ok(config.mac_timezone())


def test_the_push_contact_is_the_default_google_account(monkeypatch):
    monkeypatch.setattr(push, "PUSH_CONTACT", "")
    assert push.contact() == "mailto:me@acme.example"  # the tests' default account (conftest)
    monkeypatch.setattr(push, "PUSH_CONTACT", "https://example.org/jarvis")
    assert push.contact() == "https://example.org/jarvis"


# ── Accounts and devices ────────────────────────────────────────


def test_account_notes_and_the_default(db):
    with accounts.change() as found:
        found.add(accounts.Account("work", "me@acme.example", "for work"))
        found.add(accounts.Account("home", "me@mail.example"))
    shown = call("account_note", {"label": "home", "note": "family"})
    assert [(a["label"], a["note"], a["default"]) for a in shown] == [("work", "for work", True),
                                                                      ("home", "family", False)]
    assert [a["default"] for a in call("account_default", {"label": "home"})] == [False, True]
    with pytest.raises(ValueError, match="no nope account"):
        call("account_default", {"label": "nope"})


def test_devices_are_listed_without_their_endpoints_and_removed_by_id(tmp_path, monkeypatch):
    use_profile(monkeypatch, tmp_path)
    push.Subscriptions().add(IPHONE, "iphone")
    shown = call("devices", {"endpoint": IPHONE["endpoint"]})
    assert [(d["kind"], d["device"], d["this"]) for d in shown] == [("web", "iphone", True)]
    assert IPHONE["endpoint"] not in json.dumps(shown)
    assert call("device_remove", {"id": shown[0]["id"]}) == []
    assert push.Subscriptions().all() == []


# ── Projects ────────────────────────────────────────────────────


def test_projects_show_their_git_state_and_save_without_a_folder(env):  # noqa: F811
    shown = {p["name"]: p for p in call("projects")}
    assert shown["other"]["git"] == "repo" and shown["other"]["changed"] == 0 and shown["other"]["in_place"] is False
    (env["other"] / "new.txt").write_text("x")
    saved = {p["name"]: p for p in call("project_save", {"name": "other", "aliases": "the other one, another",
                                                         "about": "A test repo", "in_place": True})}
    assert saved["other"]["aliases"] == ["the other one", "another"]
    assert (saved["other"]["about"], saved["other"]["in_place"], saved["other"]["changed"]) == ("A test repo", True, 1)


# ── Tasks, through the runner ───────────────────────────────────


def test_a_panel_answer_needs_no_nonce_and_says_where_it_came_from(env):  # noqa: F811
    async def main():
        r = runner.Runner()
        task = r.store.create("code_change", "build", "Build it", "other")
        r.store.set(task["id"], status="running")
        approval = r.request_approval(task["id"], {"tool": "host_run", "command": "brew install jq", "folder": "/repo",
                                                    "why": describe("brew install jq")})
        return r, r.answer(approval["id"], "approve", "panel")

    r, answered = asyncio.run(main())
    assert answered["state"] == APPROVED and answered["via"] == "panel"
    assert "in the control panel" in runner.approval_result(r.store.approval(answered["id"]))


def test_tasks_are_listed_without_their_brief_and_requests_only_waits_are_told_apart(monkeypatch):
    base = {"id": 3, "kind": "research", "title": "Look", "project": None, "status": "waiting",
            "instruction": "secret brief", "summary": "long", "nudges": None, "edits": None, "session_id": "s",
            "worktree": None, "wait_kind": "approval",
            "question": runner.approval_question([{"commands": [{"why": "installs software (brew install)"}]}])}

    async def tasks(unseen=False):
        # The wait is given by wait_kind, never by the question's wording
        return [base, base | {"id": 4, "question": "Work or personal?", "wait_kind": "question"},
                base | {"id": 5, "question": "may it run the migration on the live database?", "wait_kind": "question"},
                base | {"id": 6, "wait_kind": "access"}]

    async def approvals():
        return []

    monkeypatch.setattr(client, "tasks", tasks)
    monkeypatch.setattr(client, "approvals", approvals)
    shown = call("tasks")["tasks"]
    assert [t["requests_only"] for t in shown] == [True, False, False, False]
    assert "instruction" not in shown[0] and "summary" not in shown[0] and "session_id" not in shown[0]


def test_retry_runs_a_failed_task_again_with_what_went_wrong(monkeypatch):
    sent = []

    async def task(task_id):
        return {"id": task_id, "status": "failed", "error": "tests failed"}

    async def reply(task_id, message):
        sent.append((task_id, message))
        return {"id": task_id, "status": "queued", "question": None, "worktree": None, "edits": None}

    monkeypatch.setattr(client, "task", task)
    monkeypatch.setattr(client, "reply", reply)
    call("retry", {"id": 5})
    assert sent == [(5, "Try again, picking up where you left off. Last time it ended with: tests failed")]


def test_retry_runs_a_shell_jobs_command_again_as_a_task_of_its_own(monkeypatch):
    rerun = []

    async def task(task_id):
        return {"id": task_id, "kind": "command", "status": "done", "error": None}

    async def again(task_id):
        rerun.append(task_id)
        return {"id": 9, "kind": "command", "status": "running", "question": None, "worktree": None, "edits": None}

    monkeypatch.setattr(client, "task", task)
    monkeypatch.setattr(client, "rerun", again)
    assert call("retry", {"id": 5})["id"] == 9 and rerun == [5]


# ── Scheduled jobs ──────────────────────────────────────────────


def no_runner(monkeypatch, found: list[dict] = (), runs: list[dict] = ()):
    async def tasks(unseen=False):
        return list(found)

    async def approvals():
        return []

    async def job_runs(job_id):
        return list(runs)

    monkeypatch.setattr(client, "tasks", tasks)
    monkeypatch.setattr(client, "approvals", approvals)
    monkeypatch.setattr(client, "runs", job_runs)


def test_tasks_come_with_the_scheduled_jobs_and_are_tagged_with_theirs(db, monkeypatch):
    brief = schedules.add("task", "Morning brief", "Brief me.", when="09:00", repeat="weekdays")
    watch = schedules.add("watch", "Deploy live", "v2 is live", probe="url", target="https://app.example")
    done = schedules.add("reminder", "Old", "x", in_minutes=5)
    schedules.cancel(done["id"])
    run = {"id": 7, "kind": "research", "title": "Morning brief", "status": "done", "worktree": None, "edits": None,
           "job": brief["id"]}
    no_runner(monkeypatch, [run, run | {"id": 8, "title": "Look", "job": None}])
    shown = call("tasks")
    assert [(j["title"], j["kind"], j["status"]) for j in shown["schedules"]] == [
        ("Deploy live", "watch", "active"), ("Morning brief", "task", "active")]  # soonest first; finished ones left out
    assert shown["schedules"][0]["check"] == "JARVIS checks the page: https://app.example"
    assert shown["schedules"][1]["when"] == "every weekday at 09:00"
    assert [t["job_title"] for t in shown["tasks"]] == ["Morning brief", None]
    assert watch["id"] != brief["id"]
    assert shown["hold"] is None and shown["usage"]["waiting"] == ""  # do not disturb off, and no usage hold


def test_jobs_are_paused_resumed_and_cancelled_from_the_panel(db):
    job = schedules.add("reminder", "Stretch", "Stretch.", every_minutes=30)
    assert [j["status"] for j in call("schedule_pause", {"id": job["id"]})] == ["paused"]
    assert [j["status"] for j in call("schedule_resume", {"id": job["id"]})] == ["active"]
    assert call("schedule_cancel", {"id": job["id"]}) == []
    with pytest.raises(ValueError, match="already cancelled"):
        call("schedule_cancel", {"id": job["id"]})
    with pytest.raises(ValueError, match="no scheduled job 99"):
        call("schedule_pause", {"id": 99})


def test_do_not_disturb_from_the_panel(db, monkeypatch):
    from jarvis.brain import usage

    monkeypatch.setattr(usage, "USAGE_FILE", db / "usage.json")
    assert call("hold_status")["hold"] is None
    on = call("hold_on", {"minutes": 60})
    assert on["hold"]["until"] - on["hold"]["since"] == pytest.approx(3600, abs=2)
    assert on["hold"]["said"].startswith("on until ") and "hasn't reported" in on["usage"]["said"]
    assert call("hold_status") == on  # read on its own, as Settings does
    assert call("hold_on", {})["hold"]["said"] == "on until resumed"  # changes its end
    assert call("hold_off")["hold"] is None
    assert call("hold_status")["hold"] is None
    with pytest.raises(ValueError, match="whole number"):
        call("hold_on", {"minutes": "soon"})


def test_a_job_shows_its_notepad_in_the_panel(db):
    job = schedules.add("task", "PR digest", "Which PRs await me?", when="09:00", repeat="daily", only_changes=True)
    schedules.update(job["id"], notepad=json.dumps({"at": 1.0, "text": "PRs 12, 14."}))
    shown = call("schedule", {"id": job["id"]})
    assert shown["notepad"] == {"at": 1.0, "text": "PRs 12, 14."} and shown["only_changes"] == 1


def test_a_job_shows_its_latest_runs_silent_ones_included(db, monkeypatch):
    job = schedules.add("watch", "Priya's reply", "Priya replied")
    runs = [{"id": 9, "status": "done", "created": 2, "updated": 3, "hidden": 0,
             "spoken": "Priya's reply: She said **yes**.", "error": None},
            {"id": 8, "status": "done", "created": 1, "updated": 2, "hidden": 1,
             "spoken": "Priya's reply: not yet. No reply so far.", "error": None}]
    no_runner(monkeypatch, runs=runs)
    shown = call("schedule", {"id": job["id"]})
    assert shown["title"] == "Priya's reply" and shown["runs"] == 0 and shown["runs_problem"] == ""
    assert [(r["id"], r["hidden"], r["result"]) for r in shown["recent"]] == [
        (9, False, "She said **yes**."), (8, True, "not yet. No reply so far.")]
    reminder = schedules.add("reminder", "Call", "Call Priya.", in_minutes=5)
    assert call("schedule", {"id": reminder["id"]})["recent"] == []  # a reminder starts no tasks


def test_a_job_still_shows_when_the_runner_is_down(db, monkeypatch):
    job = schedules.add("task", "Digest", "Digest.", every_minutes=60)

    async def down(job_id):
        raise client.RunnerUnavailable(client.DOWN)

    monkeypatch.setattr(client, "runs", down)
    shown = call("schedule", {"id": job["id"]})
    assert shown["title"] == "Digest" and shown["recent"] == [] and shown["runs_problem"] == client.DOWN


def test_the_runner_lists_a_job_s_runs_hidden_ones_too(env):  # noqa: F811
    r = runner.Runner()
    seen = r.store.create("research", "Morning brief", "x", None, job=5)
    hidden = r.store.create("research", "Check: deploy", "x", None, job=5, hidden=True)
    r.store.create("research", "Other", "x", None)

    async def main():
        handler = next(route.handler for route in api.routes(r) if route.path == "/tasks" and route.method == "GET")

        class Req:
            query = {"job": "5"}

        return json.loads((await handler(Req())).text)

    assert [t["id"] for t in asyncio.run(main())] == [hidden["id"], seen["id"]]


# ── The routes ──────────────────────────────────────────────────


class Request:
    remote = "127.0.0.1"

    def __init__(self, action: str, body: dict):
        self.match_info, self.body = {"action": action}, body

    async def json(self):
        return self.body


def reply(action: str, body: dict) -> tuple[int, dict]:
    r = asyncio.run(server.panel_action(Request(action, body)))
    return r.status, json.loads(r.text)


@pytest.fixture
def site(db, monkeypatch):
    monkeypatch.setenv("JARVIS_PASSCODE", "123456")  # no login yet: the first-setup passcode
    monkeypatch.setattr(server, "_failures", [])


def test_the_panel_route_needs_the_passcode_and_names_an_action(site):
    assert reply("todos", {"passcode": "000000"})[0] == 401
    assert reply("nope", {"passcode": "123456"})[0] == 404
    assert reply("todo_add", {"passcode": "123456", "text": "Milk"})[1]["result"][0]["text"] == "Milk"
    assert reply("todo_add", {"passcode": "123456", "text": " "}) == (400, {"error": "The to-do is empty."})
    routes = {(r.method, r.resource.canonical) for r in server.app().router.routes()}
    assert {("POST", "/api/panel/{action}"), ("POST", "/api/events")} <= routes


def test_the_panel_route_says_when_the_runner_is_down(site, monkeypatch):
    async def down(unseen=False):
        raise client.RunnerUnavailable(client.DOWN)

    monkeypatch.setattr(client, "tasks", down)
    assert reply("tasks", {"passcode": "123456"}) == (503, {"error": server.RUNNER_DOWN})


def test_system_asks_launchd_by_full_label(db, monkeypatch):
    asked = []

    async def launchd(label):
        asked.append(label)
        return {"name": label, "state": "running"}

    monkeypatch.setattr(panel, "_launchd", launchd)
    monkeypatch.setattr(panel.setup_local, "MANAGER", "launchd")
    monkeypatch.setattr(panel.shutil, "which", lambda cmd: "/bin/launchctl")
    monkeypatch.setattr(panel, "_vault", lambda: {"state": "ok", "secrets": 0})
    call("system")
    assert asked == [config.service_label(name) for name in config.SERVICE_NAMES]
    assert all(label.startswith(config.SERVICE_PREFIX + ".") for label in asked)


def test_on_linux_system_reads_systemd_user_units(db, monkeypatch):
    shown = {"livekit": "LoadState=loaded\nActiveState=active\nExecMainStatus=0\nExecMainStartTimestampMonotonic={}\n",
             "agent": "LoadState=loaded\nActiveState=activating\nExecMainStatus=1\n"
                      "ExecMainStartTimestampMonotonic=0\n",
             "web": "LoadState=not-found\nActiveState=inactive\nExecMainStatus=0\n"}
    asked = []
    ago = min(3600, panel.time.monotonic() / 2)  # an hour ago, or less on a computer up for less

    class Proc:
        def __init__(self, out: str):
            self.out, self.returncode = out, 0 if out else 1

        async def communicate(self):
            return self.out.encode(), b""

    async def exec_(*cmd, **kwargs):
        asked.append(cmd)
        name = cmd[-1].removesuffix(".service").rpartition(".")[2]
        started = int((panel.time.monotonic() - ago) * 1e6)
        return Proc(shown.get(name, "").format(started))

    monkeypatch.setattr(panel.setup_local, "MANAGER", "systemd")
    monkeypatch.setattr(panel.shutil, "which", lambda cmd: "/usr/bin/systemctl" if cmd == "systemctl" else None)
    monkeypatch.setattr(panel.asyncio, "create_subprocess_exec", exec_)
    monkeypatch.setattr(panel, "_vault", lambda: {"state": "ok", "secrets": 0})
    livekit, agent, web, tasks = call("system")["services"]
    assert asked[0][:3] == ("systemctl", "--user", "show")
    assert asked[0][-1] == config.service_label("livekit") + ".service"
    assert livekit["state"] == "running" and abs(panel.time.time() - ago - livekit["since"]) < 5
    assert agent == {"name": "Voice agent", "state": "stopped", "exit": 1}
    assert web == {"name": "Web", "state": "not loaded"} and tasks == {"name": "Task runner", "state": "not loaded"}
