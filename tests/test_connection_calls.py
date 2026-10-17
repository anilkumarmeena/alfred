"""Tests for a task's connection tools: changes each wait for the user's yes; GitLab files and projects come whole."""

import asyncio
import io
import json
import tarfile

import httpx
import pytest
from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny, ToolPermissionContext
from test_approvals import announced, drained, events, pushes  # noqa: F401  (pushes is a fixture)
from test_in_place import NullReporter, env, worker_task  # noqa: F401  (env is a fixture)
from test_mcp_servers import GL_TOKEN, fresh, gitlab, seeded  # noqa: F401  (fresh is a fixture)

from conftest import use_task_cache
from jarvis.gates import mcp_gate
from jarvis.integrations.mcp_servers import Server, Tool
from jarvis.storage import profile
from jarvis.tasks import approvals, repo_files, runner, worker
from jarvis.tasks.connection_calls import NEVER, WHY, ConnectionGate, call_item
from jarvis.tasks.gate import TaskGate
from jarvis.tasks.store import APPROVED, nudges

THREAD = "mcp__gitlab__create_merge_request_thread"
TODO = "mcp__gitlab__mark_todo_done"
ARGS = {"project_id": "web/player", "merge_request_iid": "280", "body": "This resumes twice."}
INLINE = ARGS | {"position": {"base_sha": "a1", "start_sha": "b2", "head_sha": "c3", "position_type": "text",
                              "new_path": "src/ads.ts", "new_line": 42}}
SAID = "comment on web/player merge request 280 at src/ads.ts line 42, saying 'This resumes twice.'"


def lab() -> Server:
    return Server("gitlab", "GitLab", command="npx", tools={
        "get_merge_request": Tool("read"),
        "create_merge_request_thread": Tool("write", "comment on {project_id} merge request {merge_request_iid}"
                                                     "[ {position}], saying {body}"),
        "mark_todo_done": Tool("own"),
        "delete_issue": Tool("blocked"),
    })


def ctx(agent: str | None = None, use: str = "u1") -> ToolPermissionContext:
    return ToolPermissionContext(tool_use_id=use, agent_id=agent)


# ── Saying what a call does ─────────────────────────────────────

def test_an_inline_comment_is_read_out_by_its_file_and_line_not_its_commit_hashes():
    assert mcp_gate.action(lab(), "create_merge_request_thread", INLINE) == SAID
    assert mcp_gate.describe(lab(), "create_merge_request_thread", ARGS) == (
        "On GitLab, comment on web/player merge request 280, saying 'This resumes twice.'.")


# ── Waiting for a yes ───────────────────────────────────────────

def test_a_tasks_comment_waits_for_a_yes_read_out_from_the_call_then_goes_through_once(env, pushes):
    async def main():
        r = runner.Runner()
        q = events(r)
        task = r.store.create("code_change", "the MR review", "Review it", "other")
        gate = ConnectionGate(task["id"], [lab()], r)
        first = await gate(THREAD, INLINE, ctx())
        [pending] = r.approvals()
        await announced(r, pending)
        answer = r.answer(pending["id"], "approve", "cli")
        again = await gate(THREAD, dict(reversed(list(INLINE.items()))), ctx())  # same arguments, in any order
        twice = await gate(THREAD, INLINE, ctx())
        return r, task, pending, first, answer, again, twice, drained(q)

    r, task, pending, first, answer, again, twice, sent = asyncio.run(main())
    assert isinstance(first, PermissionResultDeny) and f"JARVIS has asked them (request {pending['id']})" in first.message
    assert pending["summary"] == f"a GitLab call to {SAID}" and pending["remember"]["never"] == NEVER
    [event] = [e for e in sent if e["type"] == "approval"]
    assert event["text"] == f"Your task, the MR review, wants to run a GitLab call to {SAID}. Okay to go ahead?"
    [push, *_] = pushes
    assert push["category"] == "JARVIS_APPROVAL" and push["body"] == f"{SAID}\nin GitLab" and push["subtitle"] == WHY
    assert answer["state"] == APPROVED and isinstance(again, PermissionResultAllow)
    assert isinstance(twice, PermissionResultDeny)  # a yes covers one call: posting twice is asked again
    [heard] = nudges(r.store.get(task["id"]))
    assert heard == (f"{profile.name()} approved request {pending['id']} on the command line. Run exactly this, once, "
                     f"{THREAD} with exactly these arguments: {json.dumps(INLINE, sort_keys=True)}")


def test_a_yes_covers_only_the_exact_arguments(env, pushes):
    async def main():
        r = runner.Runner()
        task = r.store.create("code_change", "the MR review", "Review it", "other")
        gate = ConnectionGate(task["id"], [lab()], r)
        await gate(THREAD, INLINE, ctx())
        [pending] = r.approvals()
        await announced(r, pending)
        r.answer(pending["id"], "approve", "cli")
        return await gate(THREAD, INLINE | {"body": "Something else entirely."}, ctx())

    assert isinstance(asyncio.run(main()), PermissionResultDeny)


def test_the_users_orders_never_cover_a_call(env, monkeypatch):
    monkeypatch.setattr(approvals, "ordered", lambda said, command: True)  # as if the words ordered anything
    r = runner.Runner()
    task = r.store.create("code_change", "the MR review", "Review it", "other")
    r._order(task["id"], "Push it and merge it.")
    command = {"tool": "host_run", "command": "git push", "folder": "/repo", "why": "pushes"}
    assert r.cleared(task["id"], command) and not r.cleared(task["id"], call_item(lab(), "mark_todo_done", {"id": 7}))


def test_a_subagent_cant_ask_and_unlisted_or_blocked_tools_are_refused(env):
    async def main():
        r = runner.Runner()
        task = r.store.create("code_change", "the MR review", "Review it", "other")
        gate = ConnectionGate(task["id"], [lab()], r)
        helper = await gate(THREAD, INLINE, ctx(agent="a1"))
        blocked = await gate("mcp__gitlab__delete_issue", {"issue_iid": 3}, ctx())
        return r, helper, blocked

    r, helper, blocked = asyncio.run(main())
    assert isinstance(helper, PermissionResultDeny) and "only the task's own agent" in helper.message
    assert isinstance(blocked, PermissionResultDeny) and "isn't available" in blocked.message
    assert r.approvals() == []


class Asked:
    """A reporter whose one yes is spent on first use; records what it was asked."""

    def __init__(self, yes: int = 0) -> None:
        self.yes, self.asked = yes, []

    def cleared(self, task_id, item):
        self.yes -= 1
        return self.yes >= 0

    def request_approval(self, task_id, item):
        self.asked.append(item)
        return {"id": len(self.asked)}


def test_the_task_gate_sends_changes_and_own_inbox_tools_to_the_connection_gate(tmp_path):
    reporter = Asked()
    gate = TaskGate(tmp_path, [], [], connections=ConnectionGate(9, [lab()], reporter))

    async def main():
        return (await gate(TODO, {"id": 7}, ctx()), await gate("mcp__gitlab__delete_issue", {}, ctx()),
                await gate.pre_tool_use({"tool_name": THREAD, "tool_input": INLINE}, "u2", None))

    todo, blocked, hooked = asyncio.run(main())
    assert isinstance(todo, PermissionResultDeny) and isinstance(blocked, PermissionResultDeny)
    assert hooked["hookSpecificOutput"]["permissionDecision"] == "deny"  # a skill's allow rule can't skip it
    assert [i["tool"] for i in reporter.asked] == [TODO, THREAD]  # own-inbox tools ask too; blocked ones never do


def test_a_yes_the_hook_spends_isnt_asked_for_again_by_the_permission_check(tmp_path):
    reporter = Asked(yes=1)
    gate = TaskGate(tmp_path, [], [], connections=ConnectionGate(9, [lab()], reporter))

    async def main():
        hooked = await gate.pre_tool_use({"tool_name": THREAD, "tool_input": INLINE}, "u3", None)
        return hooked, await gate(THREAD, INLINE, ctx(use="u3"))

    hooked, decided = asyncio.run(main())
    assert hooked == {} and isinstance(decided, PermissionResultAllow) and reporter.asked == []


# ── What a task gets ────────────────────────────────────────────

def test_a_task_with_accounts_gets_its_connections_changes_held_and_gitlab_downloads(fresh, monkeypatch):
    use_task_cache(monkeypatch, fresh / "cache")
    seeded()
    opts = worker._options(worker_task(kind="research", project=None), NullReporter())
    gate = opts.can_use_tool
    assert {"mcp__gitlab__create_merge_request_note", "mcp__github__create_pull_request",
            "mcp__github__dismiss_notification"} <= gate.connections.tools
    assert set(repo_files.TOOLS) <= set(opts.allowed_tools) and "repo" in opts.mcp_servers
    assert {"Read", "Glob", "Grep"} <= set(opts.tools) and repo_files.downloads(9) in gate.read_roots
    assert "waits for their yes to that exact call" in opts.system_prompt
    assert "is GitLab's create_merge_request_thread, with a position" in opts.system_prompt
    assert "mcp__repo__download" in opts.system_prompt and "except through their connected tools" in opts.system_prompt
    code = worker._options(worker_task(worktree=str(fresh)), NullReporter())
    assert code.can_use_tool.connections is None and "repo" not in code.mcp_servers  # no accounts, no connections


def test_a_read_only_task_reads_its_connections_but_cant_change_anything_through_them(fresh, monkeypatch):
    use_task_cache(monkeypatch, fresh / "cache")
    seeded()
    opts = worker._options(worker_task(kind="research", project=None, read_only=1, grants=json.dumps(["accounts"])),
                           NullReporter())
    gate = opts.can_use_tool
    changes = {"mcp__gitlab__create_merge_request_note", "mcp__github__create_pull_request",
               "mcp__github__dismiss_notification"}
    assert gate.connections is None and changes <= set(opts.disallowed_tools)  # not even asked about
    assert isinstance(asyncio.run(gate("mcp__gitlab__create_merge_request_note", {}, ctx())), PermissionResultDeny)
    assert "only reading" in opts.system_prompt and "waits for their yes" not in opts.system_prompt
    assert "create_merge_request_thread" not in opts.system_prompt
    assert set(repo_files.TOOLS) <= set(opts.allowed_tools)  # whole files still come: that only reads


# ── Fetching whole files and projects ───────────────────────────

def fake_gitlab(monkeypatch, handler) -> list[httpx.Request]:
    """Send repo_files' requests to `handler`; returns the requests made."""
    made: list[httpx.Request] = []
    real = httpx.AsyncClient

    def record(request: httpx.Request) -> httpx.Response:
        made.append(request)
        return handler(request)

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(record), **kw))
    return made


def tools(task_id: int = 9) -> dict:
    return {t.name: t.handler for t in repo_files._tools(task_id, {"gitlab": gitlab()})}


def said(result: dict) -> str:
    return result["content"][0]["text"]


def test_a_long_file_is_saved_whole_with_the_connections_key_which_the_task_never_sees(fresh, monkeypatch):
    use_task_cache(monkeypatch, fresh / "cache")
    seeded()
    body = "x = 1\n" * 50_000  # far past what a tool result carries
    made = fake_gitlab(monkeypatch, lambda request: httpx.Response(200, content=body.encode()))
    result = asyncio.run(tools()["file"]({"project": "web/player", "ref": "c3", "path": "src/ads.ts"}))
    saved = repo_files.downloads(9) / "web-player@c3" / "src" / "ads.ts"
    assert saved.read_text() == body and str(saved) in said(result) and GL_TOKEN not in json.dumps(result)
    [request] = made
    assert request.url.raw_path.decode() == "/api/v4/projects/web%2Fplayer/repository/files/src%2Fads.ts/raw?ref=c3"
    assert request.url.host == "git.example.com" and request.headers["authorization"] == f"Bearer {GL_TOKEN}"


def test_a_redirect_to_another_host_gets_no_key_and_paths_cant_leave_the_downloads(fresh, monkeypatch):
    use_task_cache(monkeypatch, fresh / "cache")
    seeded()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "git.example.com":
            return httpx.Response(302, headers={"location": "https://files.example.net/blob/1"})
        return httpx.Response(200, content=b"elsewhere")

    made = fake_gitlab(monkeypatch, handler)
    fetched = asyncio.run(tools()["file"]({"project": "web/player", "ref": "main", "path": "README.md"}))
    escaped = asyncio.run(tools()["file"]({"project": "web/player", "ref": "main", "path": "../../outside.txt"}))
    assert "Saved README.md" in said(fetched) and "authorization" not in made[1].headers
    assert escaped.get("is_error") and "outside the downloads folder" in said(escaped) and len(made) == 2


def archive(entries: list[tarfile.TarInfo | tuple[str, bytes]]) -> bytes:
    """A gzipped tar, as GitLab's archive endpoint sends it."""
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w:gz") as tar:
        for entry in entries:
            if isinstance(entry, tarfile.TarInfo):
                tar.addfile(entry)
            else:
                info = tarfile.TarInfo(entry[0])
                info.size = len(entry[1])
                tar.addfile(info, io.BytesIO(entry[1]))
    return out.getvalue()


def test_a_project_unpacks_without_its_top_folder_and_leaves_out_what_leads_outside(fresh, monkeypatch):
    use_task_cache(monkeypatch, fresh / "cache")
    seeded()
    link = tarfile.TarInfo("player-c3/secrets")
    link.type, link.linkname = tarfile.SYMTYPE, "../../../../etc/passwd"
    data = archive([("player-c3/src/ads.ts", b"export {}"), ("player-c3/README.md", b"# Player"),
                    ("player-c3/../../escape.txt", b"no"), link])
    made = fake_gitlab(monkeypatch, lambda request: httpx.Response(200, content=data))
    result = asyncio.run(tools()["download"]({"project": "web/player", "ref": "c3"}))
    folder = repo_files.downloads(9) / "web-player@c3"
    assert (folder / "src" / "ads.ts").read_text() == "export {}" and (folder / "README.md").exists()
    assert not (folder / "secrets").exists() and not list(fresh.rglob("escape.txt"))
    assert said(result).startswith(f"Unpacked 2 files of web/player at c3 into {folder}. Left out 2 entries")
    assert made[0].url.raw_path.decode() == "/api/v4/projects/web%2Fplayer/repository/archive.tar.gz?sha=c3"


@pytest.mark.parametrize("status, message", [(404, "404 Project Not Found"), (401, "401 Unauthorized")])
def test_gitlabs_refusal_is_passed_on(fresh, monkeypatch, status, message):
    use_task_cache(monkeypatch, fresh / "cache")
    seeded()
    fake_gitlab(monkeypatch, lambda request: httpx.Response(status, json={"message": message}))
    result = asyncio.run(tools()["download"]({"project": "web/player", "ref": "c3"}))
    assert result.get("is_error") and said(result) == f"Not downloaded: GitLab said {status}: {message}"


def test_only_gitlab_connections_with_a_stored_key_get_the_download_tools(fresh):
    signed_in = Server("lab2", "Lab", command="npx", args=["@zereight/mcp-gitlab@2.1.69"])
    plain = Server("acme", "Acme", command="acme-mcp", secret_env={"GITLAB_TOKEN": "acme.token"})
    assert repo_files.gitlab([gitlab(), signed_in, plain]) == [gitlab()]


def test_a_plain_http_address_never_gets_the_key(fresh, monkeypatch):
    use_task_cache(monkeypatch, fresh / "cache")
    seeded()
    insecure = gitlab()
    insecure.env = insecure.env | {"GITLAB_API_URL": "http://git.example.com/api/v4"}
    made = fake_gitlab(monkeypatch, lambda request: httpx.Response(200, content=b"x"))
    result = asyncio.run(repo_files._tools(9, {"gitlab": insecure})[0].handler(
        {"project": "web/player", "ref": "c3", "path": "a.ts"}))
    assert result.get("is_error") and "isn't a server address" in said(result) and made == []
