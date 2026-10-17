"""Tests for projects that live on another machine: registered there, worked on in their own folder there (in place
or on a branch), merged and discarded there, and never run here."""

import asyncio
from pathlib import Path

import pytest
from test_in_place import env, git, make_repo  # noqa: F401  (env is a fixture)
from test_node_offload import World, finished, world  # noqa: F401  (world is a fixture)

from jarvis import projects
from jarvis.node import machines, offload, work
from jarvis.node.link import Refused
from jarvis.tasks import command_jobs
from jarvis.tasks.worker import Outcome
from jarvis.tools import tasks as task_tools


@pytest.fixture
def there(world, tmp_path, monkeypatch):  # noqa: F811
    """A git repo standing for a folder on the machine, inside the home both computers' checks allow."""
    monkeypatch.setattr(projects, "ALLOWED_ROOT", tmp_path)
    (tmp_path / "studio").mkdir()
    return make_repo(tmp_path / "studio" / "film")


async def registered(w: World, folder: Path, in_place: bool = True) -> dict:
    return await w.r.register_project("film", str(folder), ["the film"], "A film", in_place, machine="Studio")


def test_a_project_is_registered_on_its_machine_which_checks_and_makes_its_folder(there, tmp_path):
    async def main():
        w = await World(tmp_path).up()
        made = await registered(w, there)
        new = await w.r.register_project("notes", str(tmp_path / "studio" / "notes"), [], "", True, machine="Studio")
        with pytest.raises(ValueError, match="hidden folders"):
            await w.r.register_project("x", str(tmp_path / "studio" / ".hidden"), [], "", True, machine="Studio")
        await w.down()
        return made, new

    made, new = asyncio.run(main())
    assert made["machine"] == "Studio" and made["git"] == "existing repo, left as is"
    assert (tmp_path / "studio" / "notes").is_dir() and new["git"] == "none"
    film = projects.find("the film")
    assert film.node is not None and film.path == there.resolve()
    assert next(p for p in task_tools.projects_list() if p["name"] == "film")["lives_on"] == "Studio"


def test_a_task_in_the_projects_own_folder_there_commits_once_the_user_ordered_it(there, tmp_path):
    heard = []

    async def run(task, prompt, reporter) -> Outcome:
        heard.append(await reporter.commit_ordered(task["id"]))
        return Outcome("Done.", "", 0.01)

    async def main():
        w = await World(tmp_path, run).up()
        await registered(w, there)
        for order in ("Fix the typo and commit it.", None):
            await finished(w.r, w.r.start("code_change", "fix typo", "Fix the typo", "film", order=order)["id"])
        await w.down()

    asyncio.run(main())
    assert heard == [True, False]


def test_an_in_place_task_works_in_the_projects_own_folder_there(there, tmp_path):
    (there / "app.py").write_text("print('mine, uncommitted')\n")
    seen = {}

    async def run(task, prompt, reporter) -> Outcome:
        seen["folder"] = task["worktree"]
        (Path(task["worktree"]) / "scene.py").write_text("shot = 1\n")
        return Outcome("Added a scene.", "", 0.01)

    async def main():
        w = await World(tmp_path, run).up()
        await registered(w, there)
        machines.set_placement("tasks", machines.HERE)  # the setting doesn't matter: it lives there
        task = w.r.start("code_change", "add a scene", "Add scene.py", "film")
        shown = await w.r.where(task["id"])
        done = await finished(w.r, task["id"])
        await w.down()
        return shown, done, w.r

    shown, done, r = asyncio.run(main())
    assert shown["where"] == "It's running on Studio, where film lives."
    assert seen["folder"] == str(there.resolve()) and (there / "scene.py").exists()
    assert done["status"] == "done" and done["changes"].startswith("uncommitted:") and "untracked" in done["changes"]
    assert git(there, "for-each-ref", "refs/jarvis/")  # the user's uncommitted work was saved there first
    assert not list((tmp_path / "machine" / "worktrees").glob("node-test-t*"))  # no copy was made
    assert "saved" in [e["kind"] for e in r.store.history(done["id"], 50)]


def test_a_worktree_task_gets_its_branch_there_and_is_merged_there(there, tmp_path):
    async def run(task, prompt, reporter) -> Outcome:
        (Path(task["worktree"]) / "edit.py").write_text("cut = 2\n")
        return Outcome("Edited.", "", 0.01)

    async def main():
        w = await World(tmp_path, run).up()
        await registered(w, there, in_place=False)
        task = w.r.start("code_change", "edit the cut", "Add edit.py", "film")
        done = await finished(w.r, task["id"])
        merged = await w.r.merge(task["id"])
        await w.down()
        return done, merged

    done, merged = asyncio.run(main())
    assert done["status"] == "done" and done["branch"].startswith(f"jarvis/{done['id']}-") and "1 file changed" in done["changes"]
    assert "merged into main" in merged["spoken"] and (there / "edit.py").read_text() == "cut = 2\n"
    assert done["branch"] not in git(there, "branch")


def test_discarding_removes_the_branch_there(there, tmp_path):
    async def run(task, prompt, reporter) -> Outcome:
        (Path(task["worktree"]) / "draft.py").write_text("x = 1\n")
        return Outcome("Drafted.", "", 0.01)

    async def main():
        w = await World(tmp_path, run).up()
        await registered(w, there, in_place=False)
        task = w.r.start("code_change", "draft", "Draft it", "film")
        done = await finished(w.r, task["id"])
        gone = await w.r.discard(task["id"])
        await w.down()
        return done, gone

    done, gone = asyncio.run(main())
    assert "Worktree removed and branch deleted" in gone["spoken"]
    assert done["branch"] not in git(there, "branch") and not Path(done["worktree"]).exists()


def test_a_task_on_a_project_there_waits_for_its_machine_and_never_runs_here(there, tmp_path, monkeypatch):
    monkeypatch.setattr(offload, "NODE_WAIT_MINUTES", 0.002)
    ran = []

    async def main():
        w = await World(tmp_path, lambda *a: ran.append(a)).up()
        await registered(w, there)
        await w.down()
        task = w.r.start("code_question", "ask", "What's in it?", "film")
        return await finished(w.r, task["id"])

    done = asyncio.run(main())
    assert done["status"] == "failed" and "unreachable" in done["error"] and not ran and done["node"] is not None


def test_a_command_on_a_project_there_runs_in_its_folder_there(there, tmp_path):
    async def main():
        w = await World(tmp_path).up()
        await registered(w, there)
        task = w.r.start_command("echo rendered > out.txt", "the render", str(there.resolve()), placeable=True)
        shown = await w.r.where(task["id"])
        done = await finished(w.r, task["id"])
        await w.down()
        return shown, done

    shown, done = asyncio.run(main())
    assert shown["where"] == "It's running on Studio, where film lives." and done["status"] == "done"
    assert (there / "out.txt").read_text() == "rendered\n"
    assert command_jobs.project_of(there.resolve()) is None  # never mistaken for a folder here


def test_the_machine_refuses_folders_outside_its_home_or_holding_secrets(there, tmp_path):
    async def main():
        w = World(tmp_path)
        for bad in ("/etc", str(tmp_path / "studio" / "secrets"), str(work.ROOT)):
            with pytest.raises(Refused):
                await w.work.folder_check({"path": bad, "name": "x"})
        with pytest.raises(Refused):
            await w.work._folder_step("changes")({"folder": "/etc", "base": "", "snapshot": {}})

    asyncio.run(main())
