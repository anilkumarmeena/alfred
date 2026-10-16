"""Tests for tasks.access grants beyond a task's kind: at start, on a reply, and via request_access."""

import asyncio
import json

import pytest
from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny
from test_in_place import NullReporter, env, worker_task  # noqa: F401  (env is a fixture)
from test_nudges import fake_run, settle
from test_task_access import call, denied_paths, mac  # noqa: F401  (mac is a fixture)

from jarvis import tools
from jarvis.brain import persona, sdk
from jarvis.core import profile
from jarvis.tasks import access, briefs, client, gate, reporting, runner, worker
from jarvis.tasks.worker import Outcome
from jarvis.tools import tasks as task_tools


@pytest.fixture
def box(mac, monkeypatch):
    """mac fixture folders, JARVIS's repo and data under projects, and a code_question project, video."""
    jarvis = mac["projects"] / "jarvis"
    monkeypatch.setattr(worker, "DATA_DIR", jarvis / "data")
    monkeypatch.setattr(gate, "ROOT", jarvis)
    monkeypatch.setattr(briefs, "ROOT", jarvis)
    video = mac["projects"] / "video"
    monkeypatch.setattr(worker.projects, "find", lambda name: type("P", (), {"path": video}))
    return mac | {"jarvis": jarvis, "video": video, "own": jarvis / "data" / "tasks" / "9"}


def granted(*names: str) -> dict:
    return {"grants": json.dumps(list(names))}


def question(*names: str) -> dict:
    return worker_task(kind="code_question", project="video", worktree=None, **granted(*names))


def allowed(decision) -> bool:
    return isinstance(decision, PermissionResultAllow)


# ── What a grant adds to a task's session ───────────────────────


def test_a_plain_code_question_is_unchanged_but_can_ask(box):
    opts = worker._options(question(), NullReporter())
    assert "Write" not in opts.tools and "Artifact" not in opts.tools and "host" not in opts.mcp_servers
    assert opts.sandbox["network"] == {"allowedDomains": []} and not opts.sandbox["autoAllowBashIfSandboxed"]
    prompt = opts.system_prompt["append"]
    assert "You can't edit anything" in prompt and "request_access" in prompt
    assert all(f"{name} (" in prompt for name in access.GRANTS)
    assert worker.REQUEST_ACCESS in opts.allowed_tools and worker.REQUEST_ACCESS in reporting.PAUSES


def test_a_code_question_granted_artifacts_publishes_from_its_own_folder(box):
    opts = worker._options(question("artifacts"), NullReporter())
    assert opts.env["CLAUDE_CODE_ARTIFACT"] == "1" and {"Artifact", "Write"} <= set(opts.tools)
    assert "Edit" not in opts.tools
    page = str(box["own"] / "features.html")
    assert allowed(call(opts, "Write", file_path=page)) and allowed(call(opts, "Artifact", file_path=page))
    for tool in ("Write", "Artifact"):
        assert not allowed(call(opts, tool, file_path=str(box["code"]))), tool  # its project stays as it is
    prompt = opts.system_prompt["append"]
    assert str(box["own"]) in prompt and "claude.ai/code/artifact" in prompt and "but not change them" in prompt
    assert "artifacts (" not in prompt  # nothing left to ask for it


def test_research_granted_shell_runs_sandboxed_commands_without_secrets(box):
    opts = worker._options(worker_task(kind="research", project=None, **granted("shell")), NullReporter())
    assert "Bash" in opts.tools and opts.sandbox["autoAllowBashIfSandboxed"]
    assert opts.sandbox["network"]["allowedDomains"] == ["*"] and not opts.sandbox["allowUnsandboxedCommands"]
    assert not allowed(call(opts, "Bash", command="ls", dangerouslyDisableSandbox=True))
    rules = denied_paths(opts)
    projects = str(box["projects"].resolve())
    assert f"{projects}/**/.env" in rules and "~/.ssh/**" in rules
    assert not allowed(call(opts, "Read", file_path=str(box["code"])))  # its file tools still read nothing
    assert "Bash commands run in a sandbox" in opts.system_prompt


def test_a_code_question_granted_shell_leaves_the_checkout_unwritten(box):
    opts = worker._options(question("shell"), NullReporter())
    assert opts.sandbox["autoAllowBashIfSandboxed"] and str(box["own"]) in opts.add_dirs
    rules = json.loads(opts.settings)["permissions"]["deny"]
    video = box["video"].resolve()
    assert f"Edit(/{video})" in rules and f"Edit(/{video}/**)" in rules
    assert "can't write there" in opts.system_prompt["append"]


def test_a_code_question_on_jarvis_itself_can_still_write_its_own_folder(box, monkeypatch):
    monkeypatch.setattr(worker.projects, "find", lambda name: type("P", (), {"path": box["jarvis"]}))
    opts = worker._options(question("shell"), NullReporter())
    rules = json.loads(opts.settings)["permissions"]["deny"]
    edits = [r.removeprefix("Edit(/").removesuffix(")").removesuffix("/**") for r in rules if r.startswith("Edit(")]
    own = box["own"].resolve()
    assert not any(own.is_relative_to(e) for e in edits)
    assert str((box["jarvis"] / "logs").resolve()) in edits


def test_a_code_question_granted_mac_runs_host_commands_from_its_own_folder(box):
    opts = worker._options(question("mac"), NullReporter())
    assert "host" in opts.mcp_servers and worker.JOB_WAIT in opts.allowed_tools
    assert allowed(call(opts, worker.HOST_RUN, command="ls"))
    assert not allowed(call(opts, worker.HOST_RUN, command="ls", folder=str(box["video"])))
    refused = call(opts, worker.HOST_RUN, command=f"git -C {box['video']} commit -m x")
    assert not allowed(refused) and "own checkout" in refused.message  # as for in-place work
    assert "host_run" in opts.system_prompt["append"]


def test_projects_lets_a_task_edit_other_projects_but_never_secrets_or_jarvis(box):
    plain = worker._options(worker_task(worktree=str(box["worktree"])), NullReporter())
    assert not allowed(call(plain, "Write", file_path=str(box["code"])))
    opts = worker._options(worker_task(worktree=str(box["worktree"]), **granted("projects")), NullReporter())
    assert allowed(call(opts, "Edit", file_path=str(box["code"])))
    assert allowed(call(opts, "Write", file_path=str(box["worktree"] / "new.py")))  # its own worktree, in JARVIS's data
    for name in ("env_local", "key", "agent_log", "env"):
        denied = call(opts, "Write", file_path=str(box[name]))
        assert isinstance(denied, PermissionResultDeny) and "JARVIS's own files" in denied.message, name
    assert "edit files in" in opts.system_prompt["append"]


def test_research_granted_projects_reads_and_edits_them(box):
    opts = worker._options(worker_task(kind="research", project=None, **granted("projects")), NullReporter())
    assert {"Read", "Edit", "Write"} <= set(opts.tools)
    assert allowed(call(opts, "Read", file_path=str(box["code"]))) and allowed(call(opts, "Edit", file_path=str(box["code"])))
    assert not allowed(call(opts, "Read", file_path=str(box["env"])))
    assert "You can't contact anyone, and change only what the access below allows" in opts.system_prompt


def test_a_code_task_granted_accounts_reads_mail(box):
    opts = worker._options(worker_task(worktree=str(box["worktree"]), **granted("accounts")), NullReporter())
    assert "jarvis" in opts.mcp_servers
    assert "You can read" in opts.system_prompt["append"] and "accounts (" not in opts.system_prompt["append"]


SHEETS_DOCS_EDITS = {"sheets_write", "sheets_append", "sheets_format", "sheets_add_tab", "docs_insert",
                     "docs_replace_text", "docs_format", "docs_beautify", "docs_insert_image"}
# Deletes and sends need the user's spoken yes, and their drafts are no use without it
NEVER = {"sheets_delete_tab", "sheets_draft_delete_tab", "docs_delete", "docs_draft_delete", "gmail_send",
         "gmail_draft_send", "gmail_draft_reply", "gmail_create_draft", "chat_send", "chat_draft",
         "calendar_send_invite", "calendar_draft_invite", "calendar_send_update", "calendar_send_response",
         "calendar_delete_event", "calendar_draft_delete", "calendar_create_event"}


@pytest.fixture
def served(monkeypatch):
    """Names of the tools each task session's jarvis server offers."""
    monkeypatch.setattr(sdk, "create_sdk_mcp_server", lambda name, tools: {"type": "sdk", "name": name,
                                                                           "tools": [t.name for t in tools]})
    return lambda opts: set(opts.mcp_servers["jarvis"]["tools"])


@pytest.mark.parametrize("kind", ["research", "code_change", "code_question"])
def test_tasks_with_accounts_edit_sheets_and_docs_but_never_delete_or_send(box, served, kind):
    tasks = {"research": lambda: worker_task(kind="research", project=None),  # accounts by default
             "code_change": lambda: worker_task(worktree=str(box["worktree"]), **granted("accounts")),
             "code_question": lambda: question("accounts")}
    opts = worker._options(tasks[kind](), NullReporter())
    assert SHEETS_DOCS_EDITS <= served(opts) and "sheets_read" in served(opts)
    assert {f"mcp__jarvis__{n}" for n in SHEETS_DOCS_EDITS} <= set(opts.allowed_tools)  # as the voice has them
    assert not NEVER & served(opts) and not {f"mcp__jarvis__{n}" for n in NEVER} & set(opts.allowed_tools)
    prompt = opts.system_prompt if kind == "research" else opts.system_prompt["append"]
    assert "edit their Google Sheets and Docs" in prompt and "delete sheet tabs or delete doc content" in prompt


def test_tasks_without_accounts_get_no_google_tools(box):
    opts = worker._options(worker_task(worktree=str(box["worktree"])), NullReporter())
    assert "jarvis" not in opts.mcp_servers and not any(t.startswith("mcp__jarvis__") for t in opts.allowed_tools)


def test_doc_images_come_from_links_or_files_the_task_may_read_never_secrets(box):
    code = worker._options(worker_task(worktree=str(box["worktree"]), **granted("accounts")), NullReporter())
    research = worker._options(worker_task(kind="research", project=None), NullReporter())
    doc = {"account": "work", "document": "Plan"}

    def insert(opts, *images) -> bool:
        return allowed(call(opts, gate.DOC_IMAGES, **doc, images=list(images)))

    chart = str(box["worktree"] / "chart.png")
    assert insert(code, chart, {"image": "https://example.com/a.png", "caption": "A"}, {"image": chart})
    assert insert(research, "https://example.com/a.png")
    assert not insert(research, chart)  # no folder of its own to take images from
    assert not insert(code, "/etc/hosts") and not insert(code, {"image": "~/Desktop/x.png"})
    refused = call(code, gate.DOC_IMAGES, **doc, images=[str(box["key"])])  # readable folder, but a key
    assert isinstance(refused, PermissionResultDeny) and "never secrets" in refused.message
    hooked = asyncio.run(code.hooks["PreToolUse"][0].hooks[0](
        {"tool_name": gate.DOC_IMAGES, "tool_input": doc | {"images": ["/etc/hosts"]}}, "t1", None))
    assert hooked["hookSpecificOutput"]["permissionDecision"] == "deny"  # even if a rule allowed it outright


def test_request_access_asks_the_runner_and_pauses(monkeypatch):
    servers: dict = {}
    monkeypatch.setattr(reporting, "create_sdk_mcp_server", lambda name, tools: servers.setdefault(name, tools))
    asked = []

    class Reporter(NullReporter):
        def request_access(self, task_id, names, reason):
            asked.append((task_id, names, reason))
            if "mac" in names:
                raise ValueError("this task has that access already, so use it")
            return names

    reporting._task_server(9, Reporter())
    tool = next(t for t in servers["task"] if t.name == "request_access")
    out = asyncio.run(tool.handler({"access": ["artifacts"], "reason": "To publish the list, as asked."}))
    assert asked == [(9, ["artifacts"], "To publish the list, as asked.")]
    assert "End your turn" in out["content"][0]["text"] and not out.get("is_error")
    assert asyncio.run(tool.handler({"access": ["mac"], "reason": "x"}))["is_error"]


# ── Granting, asking and upgrading via the runner ──────────────


def runs(*bodies):
    """Bodies for fake_run that note the task each run saw."""
    seen: list[dict] = []

    def noting(body):
        async def run(task, prompt, reporter) -> Outcome:
            seen.append(dict(task))
            return await body(task, prompt, reporter)
        return run

    return seen, [noting(b) for b in bodies]


async def done(task, prompt, reporter) -> Outcome:
    reporter.session(task["id"], "s1")
    return Outcome("Did it.", "", 0.01)


async def asks_for_artifacts(task, prompt, reporter) -> Outcome:
    reporter.session(task["id"], "s1")
    reporter.request_access(task["id"], ["artifacts"], "To publish the list, as you asked.")
    return Outcome("Asked for access.", "", 0.01)


def test_tasks_start_with_access_beyond_their_kind(env, monkeypatch):
    seen, bodies = runs(done, done)
    fake_run(monkeypatch, *bodies)

    async def main():
        r = runner.Runner()
        first = r.start("code_question", "feature list", "List them", "video-agent", grants=["mac", "artifacts"])
        second = r.start("code_change", "a fix", "Fix it", "video-agent", grants=["shell", "projects"])
        with pytest.raises(ValueError, match="Unknown access"):
            r.start("research", "x", "Look it up", grants=["root"])
        await settle(r)
        return r.store.get(first["id"]), r.store.get(second["id"])

    first, second = asyncio.run(main())
    assert access.granted(first) == ["artifacts", "mac"] and access.granted(second) == ["projects"]  # shell it has
    assert {"artifacts", "mac"} <= access.has(seen[0]) and first["status"] == "done"
    assert task_tools._brief(first)["access"] == ["artifacts", "mac"]


def test_a_task_asks_for_access_and_carries_on_once_granted(env, monkeypatch):
    seen, bodies = runs(asks_for_artifacts, done)
    prompts = fake_run(monkeypatch, *bodies)

    async def main():
        r = runner.Runner()
        task = r.start("code_question", "feature list", "List the features as a link", "video-agent")
        await settle(r)
        waiting = r.store.get(task["id"])
        r.reply(task["id"], "Yes, go ahead.", ["artifacts"])
        await settle(r)
        with pytest.raises(ValueError, match="already"):
            r.request_access(task["id"], ["artifacts"], "To publish it again.")
        return waiting, r.store.get(task["id"])

    waiting, after = asyncio.run(main())
    assert waiting["status"] == "waiting"
    assert waiting["question"] == "may it also publish pages to claude.ai with the Artifact tool? To publish the list, as you asked."
    assert waiting["wait_kind"] == "access"
    assert task_tools._brief(waiting)["access_requested"] == ["artifacts"]
    assert "artifacts" in access.has(seen[1]) and seen[1]["session_id"] == "s1"  # the same session, with the tool
    assert prompts[1].startswith(f"Answer from {profile.name()}: Yes, go ahead.") and "You now also have artifacts" in prompts[1]
    assert after["status"] == "done" and not access.wanted(after) and "access_requested" not in task_tools._brief(after)
    assert after["wait_kind"] is None


def test_a_finished_task_is_upgraded_on_a_follow_up(env, monkeypatch):
    seen, bodies = runs(done, done)
    prompts = fake_run(monkeypatch, *bodies)

    async def main():
        r = runner.Runner()
        task = r.start("code_question", "feature list", "List the features", "video-agent")
        await settle(r)
        monkeypatch.setattr(access, "TASK_ARTIFACTS", False)
        with pytest.raises(ValueError, match="switched off"):
            r.reply(task["id"], "Publish it", ["artifacts"])
        monkeypatch.setattr(access, "TASK_ARTIFACTS", True)
        r.reply(task["id"], "Now publish that as a link.", ["artifacts"])
        await settle(r)
        return r.store.get(task["id"]), r.store.history(task["id"])

    after, history = asyncio.run(main())
    assert len(seen) == 2 and "artifacts" not in access.has(seen[0]) and "artifacts" in access.has(seen[1])
    assert prompts[1].startswith(f"Follow-up from {profile.name()}: Now publish that as a link.")
    assert access.granted(after) == ["artifacts"] and after["status"] == "done"
    assert any(e["kind"] == "access" and "granted artifacts" in e["text"] for e in history)


def test_a_running_task_granted_more_carries_on_with_it_after_its_turn(env, monkeypatch):
    gate = asyncio.Event()

    async def works(task, prompt, reporter) -> Outcome:
        reporter.session(task["id"], "s1")
        await gate.wait()
        return Outcome("Wrote the list; can't publish it.", "", 0.01)

    seen, bodies = runs(works, done)
    prompts = fake_run(monkeypatch, *bodies)

    async def main():
        r = runner.Runner()
        task = r.start("code_question", "feature list", "List the features", "video-agent")
        while not prompts:  # its run has begun
            await asyncio.sleep(0.01)
        r.reply(task["id"], "Publish it as a link too.", ["artifacts"])
        nudge = r.store.get(task["id"])["nudges"]
        gate.set()
        await settle(r)
        return nudge, r.store.get(task["id"])

    nudge, after = asyncio.run(main())
    assert "You now also have artifacts" in nudge and "next turn" in nudge
    assert len(seen) == 2 and "artifacts" in access.has(seen[1])
    assert "You now have artifacts" in prompts[1] and after["status"] == "done"


# ── JARVIS's side ───────────────────────────────────────────────


def test_jarvis_passes_access_on_and_knows_when_to_grant_it(monkeypatch):
    sent = []

    async def start(kind, title, instruction, project=None, access=None, order="", todo="", machine="", after=None,
                    after_merge=None):
        sent.append(("start", access))
        return {"id": 1, "title": title, "kind": kind, "status": "queued", "updated": 0}

    async def reply(task_id, message, access=None, order=""):
        sent.append(("reply", access))
        return {"id": task_id, "title": "x", "kind": "research", "status": "queued", "updated": 0}

    monkeypatch.setattr(client, "start", start)
    monkeypatch.setattr(client, "reply", reply)
    asyncio.run(task_tools.task_start("code_question", "list", "List them", "video", ["artifacts"]))
    asyncio.run(task_tools.task_reply(1, "Go ahead", ["artifacts"]))
    assert sent == [("start", ["artifacts"]), ("reply", ["artifacts"])]
    for name in ("task_start", "task_reply"):
        assert tools.get(name).properties["access"]["items"]["enum"] == list(access.GRANTS)
    assert "task_start's access" in persona.persona() and "access_requested" in persona.persona()


# ── Read-only tasks: carrying on a lookup, with its access and no more ──


def test_a_read_only_task_has_only_the_access_it_started_with_and_is_never_given_more(env, monkeypatch):
    seen, bodies = runs(done, done)
    fake_run(monkeypatch, *bodies)

    async def main():
        r = runner.Runner()
        task = r.start("research", "flights", "Find them.", grants=["accounts"], read_only=True)
        for kind, grants in (("research", ["artifacts"]), ("code_question", ["mac"]), ("code_change", [])):
            with pytest.raises(ValueError, match="only reads"):
                r.start(kind, "x", "Look.", "video-agent", grants=grants, read_only=True)
        await settle(r)
        for more in (lambda: r.reply(task["id"], "Publish it.", ["artifacts"]),
                     lambda: r.request_access(task["id"], ["shell"], "To check.")):
            with pytest.raises(ValueError, match="can't be given more access"):
                more()
        r.reply(task["id"], "Also look at trains.")  # a follow-up without access still carries on
        await settle(r)
        return r.store.get(task["id"])

    after = asyncio.run(main())
    assert after["read_only"] == 1 and access.granted(after) == ["accounts"] and len(seen) == 2
    assert all(access.has(t) == {"accounts"} for t in seen)  # not research's artifacts, on either run
    assert access.missing(after) == [] and "can't be given more" in task_tools._brief(after)["read_only"]


def test_a_read_only_task_reads_accounts_without_edits_and_runs_commands_without_the_network(box):
    reads = worker._options(worker_task(kind="research", project=None, read_only=1, **granted("accounts")),
                            NullReporter())
    assert "mcp__jarvis__gmail_search" in reads.allowed_tools
    assert not {f"mcp__jarvis__{t}" for t in worker.GOOGLE_EDIT_TOOLS} & set(reads.allowed_tools)
    assert not {"Artifact", "Write", "Bash"} & set(reads.tools) and "CLAUDE_CODE_ARTIFACT" not in reads.env
    assert "You can only read" in reads.system_prompt and "edit their Google" not in reads.system_prompt
    assert "request_access" not in reads.system_prompt
    looks = worker._options(worker_task(kind="research", project=None, read_only=1, **granted("shell")),
                            NullReporter())
    assert "Bash" in looks.tools and looks.sandbox["network"] == {"allowedDomains": []}
    assert "can't reach the network" in looks.system_prompt and "any host" not in looks.system_prompt
    assert not any(t.startswith("mcp__jarvis__") for t in looks.allowed_tools)  # no mail without accounts
    files = worker._options(question() | {"read_only": 1}, NullReporter())
    assert "Artifact" not in files.tools and "request_access" not in files.system_prompt["append"]
