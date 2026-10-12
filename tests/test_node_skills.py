"""Tests for JARVIS's skills reaching runs on another machine: the ones switched on here, each version fetched once,
checked on arrival, and loaded into the run there as they would be here."""

import asyncio
from pathlib import Path

from test_in_place import env  # noqa: F401  (env is a fixture)
from test_node_offload import World, finished, world  # noqa: F401  (world is a fixture)

from jarvis.node import machines
from jarvis.skills import home as skills
from jarvis.tasks.worker import Outcome


def runs_seeing_skills(tmp_path: Path, seen: list[list[str]]):
    """A stand-in for worker.run that builds the run's skills plugin as the worker does, and notes its skills."""
    async def run(task, prompt, reporter) -> Outcome:
        found = skills.plugin(tmp_path / "plugin", skills.shipped(task["skills"]) if "skills" in task else None)
        seen.append(sorted(s.name for s in found))
        return Outcome("Did it.", "", 0.01)
    return run


def test_a_run_on_a_machine_gets_the_skills_switched_on_here(world, tmp_path, monkeypatch):
    skills.save("release-notes", "Writes release notes", "Write them from the git log.")
    skills.save("standup", "Standup notes", "Summarise yesterday.")
    skills.switch("standup", False)
    seen: list[list[str]] = []
    packed = []

    async def main():
        w = World(tmp_path, runs_seeing_skills(tmp_path, seen))
        pack = w.r.offload._skills_pack

        async def counted(node, body):
            packed.append(sorted(body["names"]))
            return await pack(node, body)

        w.r.hub.handlers["skills.pack"] = counted
        await w.up()
        machines.set_placement("tasks", str(w.node))
        for title in ("first", "second"):
            await finished(w.r, w.r.start("code_question", title, "Look", "video-agent")["id"])
        skills.save("release-notes", "Writes release notes", "Write them from the tags.")  # a new version
        await finished(w.r, w.r.start("code_question", "third", "Look", "video-agent")["id"])
        await w.down()

    asyncio.run(main())
    assert seen == [["release-notes"]] * 3  # the one switched off here stays off there
    assert packed == [["release-notes"], ["release-notes"]]  # fetched once per version
    kept = list((tmp_path / "machine" / "data" / "node" / "skills").glob("release-notes@*"))
    assert len(kept) == 2 and "the tags" in (sorted(kept, key=lambda p: p.stat().st_mtime)[-1] / "SKILL.md").read_text()


def test_a_skill_that_arrives_altered_is_left_out(world, tmp_path):
    skills.save("release-notes", "Writes release notes", "Write them from the git log.")
    seen: list[list[str]] = []

    async def main():
        w = await World(tmp_path, runs_seeing_skills(tmp_path, seen)).up()
        offered = [{"name": "release-notes", "digest": "0" * 64}]  # not the version that will arrive
        folders = await w.work.skills(offered)
        await w.down()
        return folders

    assert asyncio.run(main()) == []


def test_sent_skills_are_checked_again_on_the_machine(tmp_path):
    good, bad = tmp_path / "good@1", tmp_path / "bad@1"
    for folder, meta in ((good, "name: good\ndescription: Does good"), (bad, "name: bad\ndescription: x\nhooks: {}")):
        folder.mkdir()
        (folder / "SKILL.md").write_text(f"---\n{meta}\n---\n\nSteps.\n")
    assert [s.name for s in skills.shipped([str(good), str(bad)])] == ["good"]  # frontmatter hooks aren't allowed
    made = skills.plugin(tmp_path / "plugin", skills.shipped([str(good)]))
    assert (tmp_path / "plugin" / "skills" / "good").resolve() == good.resolve() and made[0].folder == good
