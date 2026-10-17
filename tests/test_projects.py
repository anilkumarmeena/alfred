"""Tests for the project list: registering, aliases, changing and removing, folders without git, project_git_init."""

import asyncio
import json
import subprocess
import threading
from pathlib import Path

import pytest

from jarvis import projects, tools
from jarvis.tasks import runner
from jarvis.tasks.worker import READ_ONLY_TOOLS
from tests.test_in_place import env, fake_run, git, listed, make_repo, settle  # noqa: F401  (env is a fixture)


@pytest.fixture
def home(env, monkeypatch):
    """env with tmp as the home folder and JARVIS's folders inside it."""
    tmp = env["tmp"]
    monkeypatch.setenv("HOME", str(tmp))
    monkeypatch.setattr(projects, "ALLOWED_ROOT", tmp)
    monkeypatch.setattr(projects, "PROTECTED", [tmp / "jarvis" / "data", tmp / "jarvis" / "secrets"])
    monkeypatch.setattr(projects, "NEW_PROJECTS_DIR", tmp / "projects")
    return env


def entries() -> dict:
    """Stored rows keyed by project name, in list order."""
    with projects._db() as db:
        rows = db.execute("select * from projects order by id").fetchall()
    return {r["name"]: {"path": r["path"], "in_place": bool(r["in_place"]), "aliases": json.loads(r["aliases"]),
                        "about": r["about"]} for r in rows}


def empty_repo(path: Path) -> Path:
    path.mkdir()
    git(path, "init", "-q", "-b", "main")
    return path


def has_commits(repo: Path) -> bool:
    return subprocess.run(["git", "-C", str(repo), "rev-parse", "--verify", "-q", "HEAD"], capture_output=True).returncode == 0


# ── Registering ─────────────────────────────────────────────────


def test_register_makes_the_folder_and_entry_without_git(home):
    out = projects.register("astro-agent", aliases=["astro agent", "astrologer"], about="WhatsApp astrologer (personal)",
                            in_place=False)
    folder = home["tmp"] / "projects" / "astro-agent"
    assert folder.is_dir() and not (folder / ".git").exists()
    assert out == {"name": "astro-agent", "path": str(folder.resolve()), "created_folder": True, "in_place": True, "git": "none"}
    entry = entries()["astro-agent"]
    assert entry == {"path": "~/projects/astro-agent", "in_place": True, "aliases": ["astro agent", "astrologer"],
                     "about": "WhatsApp astrologer (personal)"}  # in_place forced: no git, so no worktrees
    found = projects.find("Astrologer")
    assert found.name == "astro-agent" and found.path.resolve() == folder.resolve() and found.in_place


def test_register_adds_to_the_end(home):
    projects.register("notes", str(home["tmp"] / "somewhere" / "notes"))
    assert list(entries()) == ["video-agent", "other", "notes"]


def test_names_with_quotes_and_unicode_survive(home):
    name, about = 'Café "Bot"', 'Ünïcode ✨ "quoted" \\ back\\slash\nsecond line'
    projects.register(name, aliases=["déjà vu", "it's \"here\""], about=about)
    p = projects.find(name)
    assert (p.name, p.about, p.aliases) == (name, about, ["déjà vu", "it's \"here\""])
    assert p.path.name == "café-bot" and p.path.is_dir()


def test_an_existing_repo_is_registered_as_it_is(home):
    repo = make_repo(home["tmp"] / "existing")
    head = git(repo, "rev-parse", "HEAD")
    out = projects.register("existing", "~/existing/", in_place=False)
    assert out["in_place"] is False and out["created_folder"] is False and out["git"] == "existing repo, left as is"
    assert entries()["existing"]["in_place"] is False  # spelled out: a missing line means in place
    assert projects.find("existing").in_place is False
    assert git(repo, "rev-parse", "HEAD") == head and git(repo, "status", "--porcelain") == ""


def test_a_repo_with_no_commits_is_forced_in_place(home):
    empty_repo(home["tmp"] / "astro")
    out = projects.register("astro", "~/astro", in_place=False)
    assert out["in_place"] is True and out["git"] == "initialised, no commits yet"


REFUSALS = {
    "outside home": (lambda t: str(t.parent / "outside-home"), "must be inside"),
    "home itself": (lambda t: "~", "whole of"),
    "home with slash": (lambda t: str(t) + "/", "whole of"),
    "library": (lambda t: "~/Library/Stuff", "Library"),
    "library any case": (lambda t: "~/library/stuff", "library"),
    "hidden": (lambda t: "~/.config/thing", "hidden"),
    "hidden deeper": (lambda t: "~/code/.cache/thing", "hidden"),
    "jarvis data": (lambda t: "~/jarvis/data/newproj", "JARVIS's own data"),
    "jarvis secrets": (lambda t: "~/jarvis/secrets", "JARVIS's own secrets"),
    "relative": (lambda t: "projects/thing", "absolute"),
    "registered": (lambda t: str(t / "video-agent"), "project video-agent"),
    "registered with tilde and slash": (lambda t: "~/video-agent/", "project video-agent"),
    "registered via symlink": (lambda t: str(t / "link-to-video"), "project video-agent"),
    "registered via dotdot": (lambda t: "~/projects/../video-agent", "project video-agent"),
    "a file": (lambda t: str(t / "a-file.txt"), "is a file"),
    "inside another repo": (lambda t: "~/other/sub", "inside the git repository"),
}


@pytest.mark.parametrize("case", REFUSALS)
def test_unsafe_or_taken_paths_are_refused_leaving_nothing_behind(home, case):
    tmp = home["tmp"]
    (tmp / "link-to-video").symlink_to(tmp / "video-agent")
    (tmp / "a-file.txt").write_text("hi")
    path, message = REFUSALS[case]
    path = path(tmp)
    before = entries()
    # tmp and its siblings, not inside other tests' folders (they may still be settling)
    def ls():
        return sorted(str(p) for p in (*tmp.rglob("*"), *tmp.parent.iterdir()) if ".git" not in p.parts)

    listing = ls()
    with pytest.raises(ValueError, match=message):
        projects.register("brand-new", path)
    assert entries() == before
    assert ls() == listing  # no folder made


@pytest.mark.parametrize("name, aliases, clash", [
    ("Video Agent", [], "video-agent"),       # the same name, loosely
    ("video_agent!", [], "video-agent"),
    ("new-one", ["OTHER"], "other"),          # an alias that's another project's name
    ("bitwise", [], "video-agent"),           # a name that's another project's alias
    ("new-one", ["bit wise"], "video-agent"),
    ("!!!", [], None),                        # nothing to match on
    ("new-one", ["..."], None),
])
def test_clashing_or_empty_names_are_refused_leaving_nothing_behind(home, name, aliases, clash):
    projects.update("video-agent", aliases=["bitwise"])
    before = entries()
    with pytest.raises(ValueError, match=f"project {clash}" if clash else "letters or digits"):
        projects.register(name, aliases=aliases)
    assert entries() == before
    assert not (home["tmp"] / "projects").exists()


def test_wrong_types_are_refused(home):
    for bad in ({"in_place": "yes"}, {"aliases": "astro"}, {"aliases": [1]}, {"about": 3}):
        with pytest.raises(ValueError):
            projects.register("typed", **bad)
    assert not (home["tmp"] / "projects").exists()


def test_runner_registers_behind_its_lock(home):
    async def main():
        r = runner.Runner()
        return await asyncio.gather(r.register_project("one"), r.register_project("two", "~/elsewhere/two"))

    one, two = asyncio.run(main())
    assert one["created_folder"] and two["created_folder"]
    assert {"one", "two"} <= entries().keys()


# ── Tasks in folders without git ────────────────────────────────


def test_in_place_task_in_a_folder_without_git_reports_changed_files(home, monkeypatch):
    projects.register("scratch")
    folder = home["tmp"] / "projects" / "scratch"
    (folder / "notes.md").write_text("old\n")
    (folder / "gone.txt").write_text("bye\n")
    (folder / "node_modules").mkdir()

    def edit(where: Path, task: dict) -> None:
        (where / "app.py").write_text("print('hi')\n")
        (where / "notes.md").write_text("old\nnew\n")
        (where / "gone.txt").unlink()
        (where / "node_modules" / "dep.js").write_text("x")  # not listed

    seen = fake_run(monkeypatch, edit)

    async def main():
        r = runner.Runner()
        task = r.start("code_change", "first version", "Build it", "scratch")
        await settle(r)
        return r.store.get(task["id"]), r

    task, r = asyncio.run(main())
    assert Path(seen["tasks"][0]["worktree"]).resolve() == folder.resolve()
    assert task["status"] == "done" and task["in_place"] == 1
    assert task["branch"] is None and task["base"] is None
    assert task["changes"] == "not a git repo; created app.py; modified notes.md; deleted gone.txt"
    assert not (folder / ".git").exists()
    assert not r.snapshots  # dropped once the task finished
    assert any("isn't a git repo" in e["text"] for e in r.store.history(task["id"]))


def test_in_place_task_without_git_that_changes_nothing(home, monkeypatch):
    projects.register("quiet")
    fake_run(monkeypatch)

    async def main():
        r = runner.Runner()
        task = r.start("code_change", "look", "Look around", "quiet")
        await settle(r)
        return r.store.get(task["id"])

    assert asyncio.run(main())["changes"] == "not a git repo; no changes"


def test_in_place_task_in_a_repo_with_no_commits(home, monkeypatch):
    astro = empty_repo(home["tmp"] / "astro-agent")
    (astro / "staged.py").write_text("a = 1\n")
    git(astro, "add", "staged.py")
    projects.register("astro-agent", "~/astro-agent")

    def edit(where: Path, task: dict) -> None:
        (where / "staged.py").write_text("a = 1\nb = 2\n")
        (where / "new.py").write_text("x = 1\n")

    fake_run(monkeypatch, edit)

    async def main():
        r = runner.Runner()
        task = r.start("code_change", "first version", "Build it", "astro agent")
        await settle(r)
        return r.store.get(task["id"]), r

    task, r = asyncio.run(main())
    assert task["status"] == "done", task
    assert task["branch"] == "main"
    assert task["base"] == git(astro, "hash-object", "-t", "tree", "/dev/null")  # the empty tree
    assert task["changes"] == "uncommitted: 1 file changed, 2 insertions(+); 1 untracked file in the folder"
    assert not has_commits(astro)
    assert git(astro, "for-each-ref", "refs/jarvis/") == "" and git(astro, "stash", "list") == ""
    assert any("no commits yet" in e["text"] for e in r.store.history(task["id"]))


@pytest.mark.parametrize("make, message", [
    (lambda p: p.mkdir(), "isn't a git repository"),
    (empty_repo, "has no git commits yet"),
])
def test_worktree_task_on_a_folder_without_commits_fails_clearly(home, monkeypatch, make, message):
    make(home["tmp"] / "plain")
    listed("plain", home["tmp"] / "plain", in_place=False)
    seen = fake_run(monkeypatch)

    async def main():
        r = runner.Runner()
        with pytest.raises(ValueError, match=message) as e:
            r.start("code_change", "x", "Do it", "plain")
        return r, str(e.value)

    r, spoken = asyncio.run(main())
    assert "plain" in spoken and "in_place" in spoken and "git" in spoken
    assert r.store.recent() == [] and seen["tasks"] == []
    assert not (home["tmp"] / "worktrees").exists()


def test_worktree_guard_also_holds_when_a_task_is_already_queued(home, monkeypatch):
    (home["tmp"] / "plain").mkdir()
    listed("plain", home["tmp"] / "plain", in_place=False)
    fake_run(monkeypatch)

    async def main():
        r = runner.Runner()
        task = r.store.create("code_change", "x", "Do it", "plain")
        r._launch(task["id"], "Do it")
        await settle(r)
        return r.store.get(task["id"])

    task = asyncio.run(main())
    assert task["status"] == "failed" and "isn't a git repository" in task["error"]


# ── Setting up git later ────────────────────────────────────────


def test_git_init_commits_the_current_files_with_a_default_gitignore(home):
    projects.register("scratch")
    folder = home["tmp"] / "projects" / "scratch"
    (folder / "app.py").write_text("print('hi')\n")
    (folder / ".env").write_text("SECRET=1\n")
    (folder / "node_modules").mkdir()
    (folder / "node_modules" / "dep.js").write_text("x")
    (folder / "venv").mkdir()
    (folder / "venv" / "python").write_text("x")

    out = projects.init_git("scratch")
    assert out["added_default_gitignore"] is True and out["files_committed"] == 2
    assert has_commits(folder)
    assert git(folder, "log", "--format=%s") == "Initial commit"
    assert git(folder, "ls-files").splitlines() == [".gitignore", "app.py"]
    assert "node_modules/" in (folder / ".gitignore").read_text()
    assert git(folder, "status", "--porcelain") == ""


def test_git_init_respects_an_existing_gitignore_and_a_repo_with_no_commits(home):
    astro = empty_repo(home["tmp"] / "astro-agent")
    (astro / ".gitignore").write_text("secret.txt\n")
    (astro / "secret.txt").write_text("x")
    (astro / "main.py").write_text("x = 1\n")
    projects.register("astro-agent", "~/astro-agent")

    out = projects.init_git("astro agent")
    assert out["added_default_gitignore"] is False and out["branch"] == "main"
    assert (astro / ".gitignore").read_text() == "secret.txt\n"
    assert git(astro, "ls-files").splitlines() == [".gitignore", "main.py"]


def test_git_init_refuses_a_repo_with_commits(home):
    with pytest.raises(ValueError, match="already has git"):
        projects.init_git("other")


def test_git_init_waits_for_the_projects_tasks(home, monkeypatch):
    projects.register("scratch")
    gate = asyncio.Event()
    fake_run(monkeypatch, gate=gate)

    async def main():
        r = runner.Runner()
        task = r.start("code_change", "x", "Do it", "scratch")
        await asyncio.sleep(0.1)
        with pytest.raises(ValueError, match=f"Task {task['id']} is working on scratch"):
            await r.init_git("scratch")
        gate.set()
        await settle(r)
        return await r.init_git("scratch")

    assert asyncio.run(main())["files_committed"] == 1  # just the .gitignore


def test_an_in_place_task_started_during_git_init_waits_for_it(home, monkeypatch):
    projects.register("scratch")
    seen = fake_run(monkeypatch)
    started, release = threading.Event(), threading.Event()
    real_init = projects.init_git

    def slow_init(name: str) -> dict:
        started.set()
        release.wait(5)
        return real_init(name)

    monkeypatch.setattr(projects, "init_git", slow_init)

    async def main():
        r = runner.Runner()
        init = asyncio.create_task(r.init_git("scratch"))
        await asyncio.to_thread(started.wait, 5)
        task = r.start("code_change", "x", "Do it", "scratch")
        await asyncio.sleep(0.2)
        assert seen["tasks"] == []  # held back while git is being set up in its folder
        release.set()
        await init
        await settle(r)
        return r.store.get(task["id"])

    task = asyncio.run(main())
    assert task["status"] == "done" and task["base"]  # it ran on the repo git init made


# ── Tools ───────────────────────────────────────────────────────


def test_project_tools_are_registered_and_not_open_to_task_sessions():
    names = {t.name for t in tools.all_tools()}
    assert {"project_register", "project_git_init"} <= names
    assert "project_register" not in READ_ONLY_TOOLS and "project_git_init" not in READ_ONLY_TOOLS
    assert "task_merge" in names and "task_merge" not in READ_ONLY_TOOLS


# ── Defaults and JARVIS itself ──────────────────────────────────


def test_an_existing_repo_is_registered_in_place_by_default(home):
    make_repo(home["tmp"] / "existing")
    out = projects.register("existing", "~/existing")
    assert out["in_place"] is True and entries()["existing"]["in_place"] is True


def test_jarvis_itself_is_registered_with_worktrees_even_when_asked_for_in_place(home, monkeypatch):
    jarvis = make_repo(home["tmp"] / "jarvis")
    monkeypatch.setattr(projects, "JARVIS_REPO", jarvis)
    out = projects.register("jarvis", "~/jarvis/", in_place=True)
    assert out["in_place"] is False and entries()["jarvis"]["in_place"] is False
    assert projects.find("jarvis").in_place is False


# ── Changing and removing ───────────────────────────────────────


def test_update_changes_only_what_it_is_given(home):
    out = projects.update("video agent", aliases=["vid", " "], about="  YouTube pipeline (personal) ")
    assert out == {"name": "video-agent", "path": str(home["video"]), "aliases": ["vid"],
                   "about": "YouTube pipeline (personal)", "in_place": True}
    assert projects.find("vid").name == "video-agent"
    assert projects.update("vid", in_place=False) == out | {"in_place": False}
    assert entries()["video-agent"]["path"] == str(home["video"])  # as it was given


def test_update_moves_a_project_to_a_folder_without_git_in_place(home):
    (home["tmp"] / "moved").mkdir()
    out = projects.update("other", path="~/moved/")
    assert out["path"] == str(home["tmp"] / "moved") and out["in_place"] is True  # worktrees need git history
    assert entries()["other"]["path"] == "~/moved"


@pytest.mark.parametrize("change, message", [
    ({}, "Say what to change"),
    ({"path": "~/nowhere"}, "doesn't exist"),
    ({"path": "~/video-agent/"}, "already registered, as the project video-agent"),
    ({"path": "~/.hidden"}, "hidden"),
    ({"aliases": ["Video Agent"]}, "clash with the project video-agent"),
    ({"aliases": ["..."]}, "letters or digits"),
    ({"aliases": "another"}, "list of text"),
    ({"in_place": "no"}, "true or false"),
    ({"about": 3}, "must be text"),
])
def test_update_refuses_what_it_cant_do_and_changes_nothing(home, change, message):
    before = entries()
    with pytest.raises(ValueError, match=message):
        projects.update("other", **change)
    assert entries() == before


def test_update_refuses_worktrees_without_git_history_and_in_place_for_jarvis(home, monkeypatch):
    projects.register("scratch")
    with pytest.raises(ValueError, match="scratch isn't a git repository"):
        projects.update("scratch", in_place=False)
    assert entries()["scratch"]["in_place"] is True
    monkeypatch.setattr(projects, "JARVIS_REPO", make_repo(home["tmp"] / "jarvis"))
    projects.register("jarvis", "~/jarvis")
    with pytest.raises(ValueError, match="never in place"):
        projects.update("jarvis", in_place=True)
    assert projects.update("jarvis", about="Me")["in_place"] is False
    with pytest.raises(ValueError, match="Unknown project 'nope'"):
        projects.update("nope", about="x")


def test_remove_takes_a_project_off_the_list_and_leaves_its_folder(home):
    assert projects.remove("Other") == f"Removed other from the project list; its folder {home['other']} is as it was."
    assert list(entries()) == ["video-agent"] and home["other"].is_dir()
    with pytest.raises(ValueError, match="Unknown project 'other'"):
        projects.remove("other")


def test_the_command_line_lists_adds_changes_and_removes(home, capsys):
    assert projects.main([]) == 0
    assert capsys.readouterr().out.splitlines() == [f"video-agent: {projects.shown(home['video'])}, in place",
                                                    f"other: {projects.shown(home['other'])}, a worktree and branch per task"]
    projects.main(["add", "notes", "--aliases", "my notes, jottings", "--about", "Notes (personal)"])
    assert capsys.readouterr().out.startswith("Added notes at ")
    assert entries()["notes"]["aliases"] == ["my notes", "jottings"]
    projects.main(["set", "jottings", "--about", "Notes", "--aliases", ""])
    out = capsys.readouterr().out
    assert out.startswith("notes: ") and out.endswith(", in place\n  about: Notes\n")
    with pytest.raises(SystemExit, match="notes isn't a git repository"):
        projects.main(["set", "notes", "--worktrees"])
    projects.main(["remove", "notes"])
    assert capsys.readouterr().out.startswith("Removed notes from the project list")
    assert list(entries()) == ["video-agent", "other"]
