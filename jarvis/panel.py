"""The web page's control panel: what jarvis.server answers on POST /api/panel/ACTION, once it has checked the passcode.

Each action takes the request's JSON body and returns what the page shows. Tasks and their requests go through the
task runner (jarvis.tasks.client), which owns them; scheduled jobs (only paused, resumed or cancelled here: they're set
by voice), the to-do list, the project list, memory, the speech vocabulary, "about you", the Google accounts, the push
devices, the heartbeat's sources and the user's voiceprint (recorded in the page, made here: jarvis.voice.speaker) are
changed here directly, as the voice agent's tools and the command line do.

Past conversations (jarvis.state.history) are only read here: the days, one day's exchanges, and a search.

Skills (jarvis.skills) are listed, read, switched on and off and deleted here; one with scripts is switched on only
with its review shown and approved here, as a spoken yes would. Installing and writing them is by voice.

Connections (the MCP servers: jarvis.integrations.mcp_servers) are listed with their tools, switched on and off,
signed in to (in JARVIS's browser on the Mac) and removed here, and here alone their keys are entered, so a key is
never spoken or typed to the brain. A key is only ever written: one of the variables a connection already takes, set
up by voice after the user's yes, never shown back.

The logins (jarvis.core.logins: each person's name and passcode) are added, given new passcodes and removed here, and
JARVIS's own keys (jarvis.core.credentials: LiveKit's, the APNs key id, the Google OAuth client) are entered here
alone, write-only like a connection's, so neither a passcode nor a key is ever spoken or typed to the brain.

What the panel can't do, on purpose: show secrets, add MCP servers or change what one runs or how its tools are
sorted, change a project's folder or sign a Google account in. Anyone with a login can reach this page, and those
would let them run commands on the Mac, loosen a gate or read what JARVIS keeps encrypted, so they're done by voice,
with a yes, or on the Mac's command line (make servers, make accounts, the Google sign-in).
"""

import asyncio
import base64
import hashlib
import logging
import re
import shutil
import time
from collections.abc import Awaitable, Callable
from dataclasses import replace
from pathlib import Path

from jarvis import setup_local
from jarvis.brain import usage
from jarvis.core import config, credentials, logins, vault
from jarvis.core.database import COMMAND, WAIT_APPROVAL, edits
from jarvis.integrations import accounts, mcp_servers
from jarvis.notify import apns, push
from jarvis.safety import remembered
from jarvis.skills import home as skills, review
from jarvis.state import activity, history, memory, settings, todo, vocabulary, voiceprints
from jarvis.tasks import client, data_edits, heartbeat, hold, projects, schedules
from jarvis.voice import speaker

log = logging.getLogger("jarvis.panel")

LOG_LINES = 40  # a task's latest log lines, every step included: the runner's most
SERVICE_NAMES = {"livekit": "LiveKit", "agent": "Voice agent", "web": "Web", "tasks": "Task runner"}
GIT_TIMEOUT = 10
HIDDEN = ("instruction", "summary", "nudges", "edits", "session_id", "log")  # left out of a task in the list

Action = Callable[[dict], Awaitable[object]]
ACTIONS: dict[str, Action] = {}


def action(fn: Action) -> Action:
    """An action the page can call, by the function's name."""
    ACTIONS[fn.__name__.rstrip("_")] = fn
    return fn


def _id(body: dict, key: str = "id") -> int:
    try:
        return int(body[key])
    except (KeyError, TypeError, ValueError):
        raise ValueError(f"Say which one: {key} is missing.") from None


def _text(body: dict, key: str, most: int = 4000) -> str:
    value = body.get(key, "")
    if not isinstance(value, str):
        raise ValueError(f"{key} must be text.")
    return value.strip()[:most]


# ── Tasks ───────────────────────────────────────────────────────


def _waits_on_requests(task: dict) -> bool:
    """A task waiting only for the user's yes to its requests, not on a question of its own."""
    return task["status"] == "waiting" and task.get("wait_kind") == WAIT_APPROVAL


def _task(task: dict, jobs: dict[int, str] | None = None) -> dict:
    """A task as the panel shows it: no instruction or summary in lists, its suggested edits in plain words, and the
    title of the scheduled job it ran for, if any (from `jobs`, by id)."""
    kept = {k: v for k, v in task.items() if k not in HIDDEN}
    return kept | {"requests_only": _waits_on_requests(task), "pending_edits": len(data_edits.pending(task)),
                   "worktree": bool(task["worktree"]), "job_title": (jobs or {}).get(task.get("job"))}


async def _job_titles(found: list[dict]) -> dict[int, str]:
    return await asyncio.to_thread(schedules.titles, {t["job"] for t in found if t.get("job") is not None})


@action
async def tasks(_: dict) -> dict:
    """Recent tasks (active ones, and any that changed in the last day), the requests waiting for an answer, and the
    scheduled jobs still to run."""
    found, waiting = await asyncio.gather(client.tasks(), client.approvals())
    titles = await _job_titles(found)
    return {"tasks": [_task(t, titles) for t in found], "approvals": waiting, "schedules": await _jobs(),
            **await _quiet()}


@action
async def task(body: dict) -> dict:
    """One task in full: its brief, its summary, its latest log lines and its suggested edits."""
    found = await client.log(_id(body), LOG_LINES)
    described = [{"n": e["n"], "status": e["status"], "what": data_edits.describe(e), "reason": e.get("reason", ""),
                  "needs_yes": e.get("needs_yes", ""), "result": e.get("result", "")} for e in edits(found)]
    return _task(found, await _job_titles([found])) | {"instruction": found["instruction"], "summary": found["summary"],
                                                       "log": found["log"], "edits": described}


@action
async def reply(body: dict) -> dict:
    """An answer to a task's question, a nudge while it runs, or a follow-up once it's finished."""
    if not (message := _text(body, "message")):
        raise ValueError("The message is empty.")
    return _task(await client.reply(_id(body), message))


@action
async def retry(body: dict) -> dict:
    """A failed or stopped task, run again where it left off, with what went wrong as the hint. A command task (a shell
    job) that has ended runs its command again instead, as a task of its own."""
    found = await client.task(_id(body))
    if found.get("kind") == COMMAND:
        return _task(await client.rerun(found["id"]))
    if found["status"] not in ("failed", "stopped"):
        raise ValueError(f"Task {found['id']} is {found['status']}, not failed or stopped.")
    why = f" Last time it ended with: {found['error']}" if found.get("error") else ""
    return _task(await client.reply(found["id"], f"Try again, picking up where you left off.{why}"))


@action
async def stop(body: dict) -> dict:
    return _task(await client.stop(_id(body)))


@action
async def merge(body: dict) -> dict:
    return _task(await client.merge(_id(body)))


@action
async def discard(body: dict) -> dict:
    return _task(await client.discard(_id(body)))


@action
async def edits_(body: dict) -> dict:
    """Apply or dismiss some of a task's suggested edits: {"id", "action": "apply" | "dismiss", "edits": [n, ...]}."""
    numbers = body.get("edits")
    if not isinstance(numbers, list) or not numbers:
        raise ValueError("Say which edits, by number.")
    done = await client.edits(_id(body), _text(body, "action"), [int(n) for n in numbers])
    return {"outcome": done["outcome"]}


@action
async def seen(body: dict) -> dict:
    """The user has looked at these tasks' latest updates in the panel, so JARVIS needn't bring them up."""
    ids = body.get("ids")
    if not isinstance(ids, list):
        raise ValueError("ids must be a list.")
    await client.mark_seen([int(i) for i in ids][:100])
    return {"ok": True}


@action
async def answer(body: dict) -> dict:
    """The user's yes or no to a task's request, from the panel: {"id", "decision": "approve" | "deny"}, and for a
    yes, how far it goes: "remember": "once" | "session" | "always", with "breadth": "exact" | "kind" for an always,
    whose rule the page shows beside its button before it's tapped (the request's `remember`)."""
    decision = body.get("decision")
    if decision not in ("approve", "deny"):
        raise ValueError("decision must be approve or deny.")
    remember, breadth = body.get("remember") or "once", body.get("breadth") or "exact"
    answered = await client.answer(_id(body), decision, "panel", remember=remember, breadth=breadth)
    return {"state": answered["state"], "outcome": answered["outcome"], "answered": answered.get("answered", False),
            "remembered": answered.get("remembered")}


# ── Remembered approvals ────────────────────────────────────────
# Made only by answering a request (or by voice); here they're listed and withdrawn (jarvis.safety.remembered).


def _rules() -> list[dict]:
    return [{k: r[k] for k in ("id", "scope", "session", "covers", "who", "example", "via", "created", "uses", "last_used")}
            for r in remembered.listed()]


@action
async def approval_rules(_: dict) -> list[dict]:
    return await asyncio.to_thread(_rules)


@action
async def approval_rule_forget(body: dict) -> list[dict]:
    gone = await asyncio.to_thread(remembered.forget, _id(body))
    activity.add(f"Withdrew remembered approval {gone['id']}: {gone['covers']}", "warn")
    return await asyncio.to_thread(_rules)


# ── Scheduled jobs ──────────────────────────────────────────────
# Set by voice only (jarvis.tools.schedules): the panel lists them, shows their runs, and pauses, resumes or cancels.

JOB_FIELDS = ("id", "kind", "title", "text", "status", "next_run", "last_run", "until", "runs", "last_result",
              "task_kind", "project", "only_changes", "folder")
RUNS = 10  # a job's latest runs, in its view


def _job(job: dict) -> dict:
    note = schedules.notepad(job)
    return {k: job[k] for k in JOB_FIELDS} | {"when": schedules.describe(job), "check": schedules.check_text(job),
                                              "notepad": note and {"at": note["at"], "text": note["text"]}}


async def _jobs() -> list[dict]:
    """The jobs still to run, active or paused, soonest first."""
    return [_job(j) for j in await asyncio.to_thread(schedules.items)]


def _run(task: dict, job: dict) -> dict:
    """One of a job's runs: when, how it ended, and what it said, without the job's title in front."""
    said = (task.get("spoken") or task.get("error") or task.get("activity") or "").removeprefix(f"{job['title']}: ")
    return {"id": task["id"], "status": task["status"], "created": task["created"], "updated": task["updated"],
            "hidden": bool(task.get("hidden")), "result": said, "fix": bool(task.get("fixes"))}


@action
async def schedule(body: dict) -> dict:
    """One job in full, with its latest runs: tasks, or a command's command tasks and the tasks fixing them. Reminders,
    and watches checked without a model, start no tasks: their latest run is in last_run and last_result."""
    job = await asyncio.to_thread(schedules.find, _id(body))
    runs, problem = [], ""
    if job["task_kind"] or job["kind"] == "command":
        try:
            runs = [_run(t, job) for t in (await client.runs(job["id"]))[:RUNS]]
        except client.RunnerUnavailable as e:
            problem = str(e)
    return _job(job) | {"recent": runs, "runs_problem": problem}


@action
async def schedule_pause(body: dict) -> list[dict]:
    await asyncio.to_thread(schedules.change, _id(body), paused=True)
    return await _jobs()


@action
async def schedule_resume(body: dict) -> list[dict]:
    await asyncio.to_thread(schedules.change, _id(body), paused=False)
    return await _jobs()


@action
async def schedule_cancel(body: dict) -> list[dict]:
    """Cancel a job; a run already under way carries on (Stop on its task stops it)."""
    await asyncio.to_thread(schedules.cancel, _id(body))
    return await _jobs()


# Do not disturb (jarvis.tasks.hold), which holds every job, the heartbeat and task notifications; and what's known of
# Claude's usage limits (jarvis.brain.usage), which scheduled runs wait on.

def _quiet_now() -> dict:
    h = hold.status()
    wait = usage.hold_until()
    return {"hold": h and h | {"said": hold.describe(h)},
            "usage": {"said": usage.said(), "waiting": f"Scheduled tasks wait for its reset {usage.when(wait)}."
                      if wait else ""}}


async def _quiet() -> dict:
    return await asyncio.to_thread(_quiet_now)


@action
async def hold_status(_: dict) -> dict:
    """Whether do not disturb is on, for Settings → Preferences, without asking the task runner as `tasks` does."""
    return await _quiet()


@action
async def hold_on(body: dict) -> dict:
    """Do not disturb: {"minutes": n} or {"until": "15:00"}, or neither for until it's turned off."""
    minutes = body.get("minutes") or 0
    if not isinstance(minutes, int) or isinstance(minutes, bool):
        raise ValueError("minutes must be a whole number.")
    await asyncio.to_thread(hold.start, _text(body, "until", 40), minutes)
    return await _quiet()


@action
async def hold_off(_: dict) -> dict:
    await asyncio.to_thread(hold.end)
    return await _quiet()


# ── To-dos ──────────────────────────────────────────────────────


@action
async def todos(_: dict) -> list[dict]:
    return todo.items()


@action
async def todo_add(body: dict) -> list[dict]:
    todo.add(_text(body, "text", 500))
    return todo.items()


@action
async def todo_check(body: dict) -> list[dict]:
    todo.check(_id(body), bool(body.get("done", True)))
    return todo.items()


@action
async def todo_edit(body: dict) -> list[dict]:
    todo.edit(_id(body), _text(body, "text", 500))
    return todo.items()


@action
async def todo_delete(body: dict) -> list[dict]:
    todo.delete(_id(body))
    return todo.items()


# ── Projects ────────────────────────────────────────────────────


async def _changed(folder: Path) -> int | None:
    """How many files differ from the last commit in a git folder, or None if git can't say in time."""
    try:
        proc = await asyncio.create_subprocess_exec("git", "-C", str(folder), "status", "--porcelain",
                                                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await asyncio.wait_for(proc.communicate(), GIT_TIMEOUT)
    except (OSError, TimeoutError):
        return None
    return len(out.splitlines()) if proc.returncode == 0 else None


async def _project(p: projects.Project) -> dict:
    state = await asyncio.to_thread(projects.git_state, p.path)
    return {"name": p.name, "path": projects.shown(p.path), "aliases": p.aliases, "about": p.about,
            "in_place": p.in_place, "jarvis": projects.is_jarvis(p.path), "exists": p.path.is_dir(), "git": state,
            "changed": await _changed(p.path) if state == projects.GIT else None}


@action
async def projects_(_: dict) -> list[dict]:
    return list(await asyncio.gather(*(_project(p) for p in await asyncio.to_thread(projects.load))))


def _aliases(body: dict) -> list[str] | None:
    value = body.get("aliases")
    if value is None:
        return None
    if isinstance(value, str):
        value = value.split(",")
    if not isinstance(value, list):
        raise ValueError("aliases must be a list.")
    return [str(a) for a in value]


@action
async def project_save(body: dict) -> list[dict]:
    """A project's aliases, about and mode. Not its folder: that stays on the Mac's command line."""
    in_place = body.get("in_place")
    await asyncio.to_thread(projects.update, _text(body, "name"), None, _aliases(body),
                            _text(body, "about", 300) if "about" in body else None,
                            None if in_place is None else bool(in_place))
    return await projects_({})


@action
async def project_add(body: dict) -> list[dict]:
    """A new project: a folder that exists, or a new one made under the projects folder when none is given."""
    await client.register_project(_text(body, "name", 60), _text(body, "path", 300) or None, _aliases(body) or [],
                                  _text(body, "about", 300), bool(body.get("in_place", projects.IN_PLACE_DEFAULT)))
    return await projects_({})


@action
async def project_remove(body: dict) -> list[dict]:
    await asyncio.to_thread(projects.remove, _text(body, "name"))
    return await projects_({})


@action
async def project_git(body: dict) -> list[dict]:
    await client.git_init(_text(body, "name"))
    return await projects_({})


# ── Skills ──────────────────────────────────────────────────────

SKILL_TEXT = 60_000  # characters of a SKILL.md the page shows


def _skill(s: skills.Skill) -> dict:
    return s.brief() | {"enabled": s.enabled, "needs_approval": s.needs_approval, "kind": s.source,
                        "created": s.created, "updated": s.updated}


@action
async def skills_(_: dict) -> list[dict]:
    """Every skill: name, description, where it came from, whether it's on, when it was added."""
    return [_skill(s) for s in await asyncio.to_thread(skills.all_skills)]


def _skill_detail(name: str) -> dict:
    s = skills.get(name)
    text = skills.skill_md(s.folder)
    files = [{"path": f, "size": (s.folder / f).lstat().st_size, "script": f in s.scripts} for f in skills.files(s.folder)]
    found = None
    if s.review:
        found = {k: s.review.get(k) for k in ("summary", "risks", "flags", "verdict", "partial", "at", "model")}
        found["current"] = review.current(s) is not None
    return _skill(s) | {"skill_md": text[:SKILL_TEXT], "cut": len(text) > SKILL_TEXT, "files": files, "review": found}


@action
async def skill(body: dict) -> dict:
    """One skill in full: its SKILL.md, its files (which are scripts), and the latest review of its scripts."""
    return await asyncio.to_thread(_skill_detail, _text(body, "name", 100))


def _switch(name: str, on: bool, approve: bool) -> dict:
    s = skills.get(name)
    if on and s.needs_approval:
        if not (approve and (found := review.current(s))):
            raise ValueError(f"{s.name} has scripts that need their review read and approved first.")
        skills.approve(s.name, found["digest"])
    else:
        skills.switch(s.name, on)
    return _skill_detail(s.name)


@action
async def skill_switch(body: dict) -> dict:
    """Switch a skill on or off. On, for one with scripts not yet approved as they are, takes approve: true, sent once
    the page has shown the review of those very files."""
    return await asyncio.to_thread(_switch, _text(body, "name", 100), bool(body.get("on")), bool(body.get("approve")))


@action
async def skill_review(body: dict) -> dict:
    """Review a skill's scripts (a short model call), for the page to show before they're approved."""
    name = _text(body, "name", 100)
    await review.run(name)
    return await asyncio.to_thread(_skill_detail, name)


@action
async def skill_delete(body: dict) -> list[dict]:
    """Delete a skill, folder and all; the page has asked first."""
    await asyncio.to_thread(skills.delete, _text(body, "name", 100))
    return await skills_({})


# ── Memory and "about you" ──────────────────────────────────────


def _memory(change: Callable[[], object] = lambda: None) -> dict:
    """Memory's lines by section, and how much of each section's limit they take, after `change`. A section too
    full for it says so in a sentence, as the page already lists its facts."""
    try:
        change()
    except memory.Full as e:
        raise ValueError(f"{e.short} Shorten or forget some of its facts first.") from None
    found = memory.facts()
    return found | {"limits": {s: {"used": memory.usage(found[s]), "max": memory.LIMITS[s]} for s in memory.SECTIONS}
                    | {"fact": memory.FACT_MAX}}


@action
async def memory_(_: dict) -> dict:
    return _memory()


@action
async def remember(body: dict) -> dict:
    if not (fact := _text(body, "fact", 1000)):
        raise ValueError("The memory is empty.")
    return _memory(lambda: memory.remember(fact, _text(body, "section")))


@action
async def memory_edit(body: dict) -> dict:
    """Rewrite one line of memory, named exactly as listed; an empty fact forgets it."""
    return _memory(lambda: memory.replace_fact(str(body.get("line", "")), _text(body, "fact", 1000)))


# ── The speech vocabulary ───────────────────────────────────────


def _heard(body: dict) -> list[str]:
    """The mishearings given: a list, or one text separated by commas."""
    value = body.get("heard", [])
    if isinstance(value, str):
        value = value.split(",")
    if not (isinstance(value, list) and all(isinstance(h, str) for h in value)):
        raise ValueError("heard must be text.")
    return [h.strip() for h in value if h.strip()][:vocabulary.MAX_HEARD]


def _vocabulary(change: Callable[[], object] = lambda: None) -> dict:
    """The vocabulary's entries after `change`, the project names speech recognition is also given, and the cap."""
    change()
    found = vocabulary.entries()
    return {"entries": [{"word": e.word, "heard": e.heard, "date": e.date, "learned": e.learned} for e in found],
            "projects": [p for p in vocabulary.project_terms() if not vocabulary.find(found, p)],
            "max": vocabulary.MAX_ENTRIES, "prompt": config.STT_PROMPT}


@action
async def vocabulary_(_: dict) -> dict:
    return await asyncio.to_thread(_vocabulary)


@action
async def vocabulary_add(body: dict) -> dict:
    word, heard = _text(body, "word", 200), _heard(body)
    return await asyncio.to_thread(_vocabulary, lambda: vocabulary.add(word, heard))


@action
async def vocabulary_save(body: dict) -> dict:
    """Rewrite one entry, named by its word as listed, with exactly the mishearings given."""
    old, word, heard = _text(body, "old", 200), _text(body, "word", 200), _heard(body)
    return await asyncio.to_thread(_vocabulary, lambda: vocabulary.save(old, word, heard))


@action
async def vocabulary_remove(body: dict) -> dict:
    word = _text(body, "word", 200)
    return await asyncio.to_thread(_vocabulary, lambda: vocabulary.remove(word))


@action
async def about(_: dict) -> dict:
    return {"text": settings.about()}


@action
async def about_save(body: dict) -> dict:
    return {"text": settings.set_about(_text(body, "text", 4000))}


# ── Preferences: name, time zone, voice, task limit ─────────────


@action
async def preferences(_: dict) -> dict:
    return await asyncio.to_thread(settings.preferences)


@action
async def preference_save(body: dict) -> dict:
    """{"key", "value"}: one preference; an empty value goes back to the default."""
    settings.set_preference(_text(body, "key", 40), _text(body, "value", 100))
    return await asyncio.to_thread(settings.preferences)


# ── The heartbeat's sources (jarvis.tasks.heartbeat), read afresh at every beat ──


def _settings(body: dict) -> dict:
    found = body.get("settings") or {}
    if not isinstance(found, dict):
        raise ValueError("settings must be an object.")
    return {str(k)[:40]: _text(found, k, 300) for k in found}


def _heartbeat(change: Callable[[], object] = lambda: None) -> dict:
    change()
    return heartbeat.described()


@action
async def heartbeat_sources(_: dict) -> dict:
    return await asyncio.to_thread(_heartbeat)


@action
async def heartbeat_source_save(body: dict) -> dict:
    """{"id", "on"?, "settings"?}: switch a source, or change its settings; an empty value goes back to the default."""
    id_, on, values = _text(body, "id", 80), body.get("on"), _settings(body)
    if on is not None and not isinstance(on, bool):
        raise ValueError("on must be true or false.")
    return await asyncio.to_thread(_heartbeat, lambda: heartbeat.change_source(id_, on, values))


@action
async def heartbeat_source_add(body: dict) -> dict:
    """{"kind", "title", "settings"}: another source of a kind that allows more."""
    kind, title, values = _text(body, "kind", 40), _text(body, "title", 80), _settings(body)
    return await asyncio.to_thread(_heartbeat, lambda: heartbeat.add_source(kind, title, values, "in the panel"))


@action
async def heartbeat_source_remove(body: dict) -> dict:
    id_ = _text(body, "id", 80)
    return await asyncio.to_thread(_heartbeat, lambda: heartbeat.remove_source(id_))


# ── Voiceprints: the speaker check's (jarvis.voice.speaker) ─────

MAX_ENROLL_SECONDS = 90  # of recording the page may send


def _voiceprints() -> dict:
    """The user's voiceprint (None before they enroll), whether the model is there, and what to read for how long."""
    owner = next((p.described() for p in voiceprints.every() if p.name == voiceprints.OWNER), None)
    return {"owner": owner, "model": config.SPEAKER_MODEL.is_file(), "passage": speaker.PASSAGE,
            "seconds": speaker.RECORD_SECONDS}


@action
async def voiceprint(_: dict) -> dict:
    return await asyncio.to_thread(_voiceprints)


@action
async def voiceprint_enroll(body: dict) -> dict:
    """{"audio": base64 of 16 kHz mono 16-bit PCM}: the user reading for about 30 seconds, recorded in the page.
    Replaces their voiceprint; raises ValueError, saying what to do, when it can't make one."""
    try:
        pcm = base64.b64decode(str(body.get("audio", "")), validate=True)
    except ValueError:
        raise ValueError("The recording didn't arrive whole. Try again.") from None
    if len(pcm) > MAX_ENROLL_SECONDS * speaker.RATE * 2:
        raise ValueError(f"That recording is too long: keep it under {MAX_ENROLL_SECONDS} seconds.")
    await asyncio.to_thread(speaker.enroll, speaker.to_float(pcm), via="panel")
    return await asyncio.to_thread(_voiceprints)


@action
async def voiceprint_remove(body: dict) -> dict:
    """{"name"}: forget a voiceprint, the user's by default."""
    await asyncio.to_thread(voiceprints.remove, _text(body, "name", 40) or voiceprints.OWNER)
    return await asyncio.to_thread(_voiceprints)


# ── Google accounts ─────────────────────────────────────────────


def _accounts() -> list[dict]:
    found = accounts.load()
    return [{"label": a.label, "email": a.email, "note": a.note, "chat": a.chat, "default": a.label == found.default,
             "signed_in": accounts.token_path(a.label).exists()} for a in found.accounts]


@action
async def accounts_(_: dict) -> list[dict]:
    return _accounts()


@action
async def account_note(body: dict) -> list[dict]:
    with accounts.change() as found:
        found.update(_text(body, "label"), note=_text(body, "note", 300))
    return _accounts()


@action
async def account_default(body: dict) -> list[dict]:
    with accounts.change() as found:
        found.set_default(_text(body, "label"))
    return _accounts()


# ── Devices that get notifications ──────────────────────────────


def _device_id(secret: str) -> str:
    """An id for a push endpoint or app token that doesn't give it away."""
    return hashlib.sha256(secret.encode()).hexdigest()[:16]


def _devices(endpoint: str = "") -> list[dict]:
    web = [{"id": _device_id(s["endpoint"]), "kind": "web", "device": s.get("device") or "browser",
            "added": s.get("added"), "this": s["endpoint"] == endpoint} for s in push.Subscriptions().all()]
    app = [{"id": _device_id(d["token"]), "kind": "app", "device": d.get("device") or "iPhone", "added": d.get("added"),
            "this": False} for d in apns.Devices().all()]
    return web + app


@action
async def devices(body: dict) -> list[dict]:
    """The devices that get JARVIS's notifications; the one the page sends its push endpoint for is marked."""
    return _devices(str(body.get("endpoint", "")))


@action
async def device_remove(body: dict) -> list[dict]:
    gone = _text(body, "id", 32)
    subs = push.Subscriptions()
    subs.remove([s["endpoint"] for s in subs.all() if _device_id(s["endpoint"]) == gone])
    apns.Devices().remove({d["token"] for d in apns.Devices().all() if _device_id(d["token"]) == gone})
    return _devices(str(body.get("endpoint", "")))


# ── The bell ───────────────────────────────────────────────────


@action
async def activity_(_: dict) -> list[dict]:
    """What JARVIS did lately (jarvis.state.activity), newest first: the bell's list."""
    return await asyncio.to_thread(activity.recent)


# ── History: past conversations (read only) ─────────────────────
# The full log in data/conversations (jarvis.state.history), newest first: the days, one day's exchanges, a search.


@action
async def conversations(body: dict) -> dict:
    """The days with conversations, newest first, a page at a time: {"before": "YYYY-MM-DD"} for the next page."""
    return await asyncio.to_thread(history.days, _text(body, "before", 10))


@action
async def conversation(body: dict) -> dict:
    """One day's exchanges, newest first: {"date": "YYYY-MM-DD"}."""
    return await asyncio.to_thread(history.day, _text(body, "date", 10))


@action
async def conversation_search(body: dict) -> dict:
    """The exchanges that mention every word of {"text"}, newest first."""
    return await asyncio.to_thread(history.search, _text(body, "text", 200))


# ── Logins and JARVIS's own keys ────────────────────────────────
# Who may connect, each with a passcode of their own (jarvis.core.logins), and the keys JARVIS itself uses
# (jarvis.core.credentials): written here, never read back, and only here, never by voice.


def _access(login: str) -> dict:
    """The logins, the caller's marked, and JARVIS's keys: whether each is set and where from, never what it is."""
    return {"logins": [found | {"this": found["name"] == login} for found in logins.listed()],
            "keys": [{"name": c.name, "label": c.label, "hint": c.hint, "required": c.required,
                      "source": credentials.source(c)} for c in credentials.ALL.values()]}


def _passcode(body: dict) -> str:
    value = body.get("passcode_new", "")
    if not isinstance(value, str):
        raise ValueError("The passcode must be text.")
    return value


@action
async def access(body: dict) -> dict:
    return await asyncio.to_thread(_access, body.get("login", ""))


@action
async def login_add(body: dict) -> dict:
    """A new login: {"name", "passcode_new"}. The passcode is theirs alone: another login's is refused."""
    name = _text(body, "name", 40)
    await asyncio.to_thread(logins.add, name, _passcode(body))
    await asyncio.to_thread(activity.add, f"Added the login {name} in the control panel.")
    return await access(body)


@action
async def login_passcode(body: dict) -> dict:
    """A new passcode for a login: {"name", "passcode_new"}."""
    await asyncio.to_thread(logins.set_passcode, _text(body, "name", 40), _passcode(body))
    return await access(body)


@action
async def login_remove(body: dict) -> dict:
    """Removes a login, never the last: {"name"}. Its devices are asked for a passcode again."""
    name = _text(body, "name", 40)
    await asyncio.to_thread(logins.remove, name)
    await asyncio.to_thread(activity.add, f"Removed the login {name} in the control panel.")
    return await access(body)


def _set_key(body: dict) -> None:
    key = credentials.need(_text(body, "name", 40))
    credentials.put(key, str(body.get("value", "")))
    if key in (credentials.LIVEKIT_KEY, credentials.LIVEKIT_SECRET):
        setup_local.write_livekit_config()  # for the LiveKit server, as it next starts


@action
async def key_set(body: dict) -> dict:
    """Keeps one of JARVIS's keys in the vault: {"name", "value"}. Only written, never read back. JARVIS's services
    use it once they restart."""
    await asyncio.to_thread(_set_key, body)
    return await access(body)


@action
async def key_clear(body: dict) -> dict:
    """Forgets one of JARVIS's keys that it can do without: {"name"}. .env's, if it has one, is used again."""
    await asyncio.to_thread(credentials.clear, credentials.need(_text(body, "name", 40)))
    return await access(body)


# ── Connections (read only) and the system ──────────────────────


SECRET_MOST = 8000  # characters of a key entered here


def _server(s: mcp_servers.Server) -> dict:
    by_access: dict[str, int] = {}
    for spec in s.tools.values():
        by_access[spec.access] = by_access.get(spec.access, 0) + 1
    return {"name": s.name, "label": s.label, "enabled": s.enabled, "reviewed": s.reviewed,
            "command": s.url or " ".join([s.command, *s.args]), "tools": by_access,
            "tool_list": [{"name": t, "access": spec.access} for t, spec in sorted(s.tools.items())],
            "secrets": [{"var": var, "set": vault.has(name)} for var, name in s.secret_env.items()],
            "sign_in": s.sign_in, "signed_in": s.sign_in and vault.has(s.oauth_secret),
            "source": s.source, "verified": s.verified, "version": s.version, "research": s.for_research,
            "problem": mcp_servers.missing(s, reviewed=False) if s.enabled or not s.reviewed else ""}


def _servers() -> list[dict]:
    return [_server(s) for s in mcp_servers.load()]


def _connection(body: dict) -> mcp_servers.Server:
    return mcp_servers.need(_text(body, "name", 40))


@action
async def servers(_: dict) -> list[dict]:
    """The MCP servers: whether each is on and can start, where it came from, its tools by access, and which of its
    secrets are set, never what they are."""
    return await asyncio.to_thread(_servers)


def _switch_server(body: dict) -> list[dict]:
    server = _connection(body)
    on = bool(body.get("on"))
    if on and not server.reviewed:
        raise ValueError(f"{server.label}'s tools haven't been sorted yet: ask JARVIS to review them, and your yes "
                         "switches it on.")
    mcp_servers.save(replace(server, enabled=on))
    return _servers()


@action
async def server_switch(body: dict) -> list[dict]:
    """Switches a connection on or off: {"name", "on"}. One whose tools aren't sorted yet stays off."""
    return await asyncio.to_thread(_switch_server, body)


def _set_secret(body: dict) -> list[dict]:
    server = _connection(body)
    var = _text(body, "var", 64)
    if var not in server.secret_env:
        raise ValueError(f"{server.label} takes no {var}.")
    value = str(body.get("value", "")).strip()
    if not value or len(value) > SECRET_MOST or "\n" in value:
        raise ValueError("Paste the key on one line.")
    try:
        vault.ensure()
        vault.put(server.secret_env[var], value)
    except vault.VaultError as e:
        raise ValueError(str(e)) from None
    log.warning("the panel set %s's %s", server.name, var)  # which, never what
    return _servers()


@action
async def server_secret(body: dict) -> list[dict]:
    """Keeps a key a connection takes, encrypted: {"name", "var", "value"}. Only written, never read back."""
    return await asyncio.to_thread(_set_secret, body)


def _clear_secret(body: dict) -> list[dict]:
    server = _connection(body)
    var = _text(body, "var", 64)
    if var not in server.secret_env:
        raise ValueError(f"{server.label} takes no {var}.")
    vault.remove(server.secret_env[var])
    return _servers()


@action
async def server_secret_clear(body: dict) -> list[dict]:
    """Forgets a connection's key: {"name", "var"}."""
    return await asyncio.to_thread(_clear_secret, body)


@action
async def server_sign_in(body: dict) -> dict:
    """Opens a connection's sign-in page in JARVIS's browser on the Mac, and waits up to ten minutes for the user to
    sign in there: {"name"}."""
    from jarvis.integrations import mcp_oauth
    from jarvis.integrations.browser import BROWSER, Unavailable

    server = await asyncio.to_thread(_connection, body)
    if not server.sign_in:
        raise ValueError(f"{server.label} doesn't sign in.")
    try:
        said = await mcp_oauth.sign_in(server, lambda url, back: BROWSER.sign_in(url, back, mcp_oauth.SIGN_IN_MINUTES))
    except TimeoutError:
        raise ValueError("The sign-in wasn't finished in time.") from None
    except (mcp_oauth.SignInError, Unavailable) as e:
        raise ValueError(str(e)) from None
    finally:
        await BROWSER.close()
    await asyncio.to_thread(activity.add, said)
    return {"said": said, "servers": await asyncio.to_thread(_servers)}


@action
async def server_impact(body: dict) -> dict:
    """What removing a connection takes with it, to show before the user agrees: {"name"}."""
    from jarvis.tools.connections import impact

    return {"impact": await asyncio.to_thread(lambda: impact(_connection(body)))}


@action
async def server_remove(body: dict) -> list[dict]:
    """Removes a connection, with its keys and sign-in, once the user has read what goes with it: {"name"}."""
    server = await asyncio.to_thread(_connection, body)
    await asyncio.to_thread(mcp_servers.remove, server.name)
    await asyncio.to_thread(activity.add, f"Removed {server.label} in the control panel.")
    return await asyncio.to_thread(_servers)


async def _launchd(label: str) -> dict:
    """A launchd service's state, from launchctl: its pid while running, and how long it has been up."""
    out = ""
    try:
        proc = await asyncio.create_subprocess_exec("launchctl", "list", label, stdout=asyncio.subprocess.PIPE,
                                                    stderr=asyncio.subprocess.DEVNULL)
        raw, _ = await asyncio.wait_for(proc.communicate(), 5)
        out = raw.decode(errors="replace") if proc.returncode == 0 else ""
    except (OSError, TimeoutError):
        pass
    name = SERVICE_NAMES.get(label.rsplit(".", 1)[-1], label)
    if not out:
        return {"name": name, "state": "not loaded"}
    pid = re.search(r'"PID" = (\d+);', out)
    if not pid:
        status = re.search(r'"LastExitStatus" = (-?\d+);', out)
        return {"name": name, "state": "stopped", "exit": int(status.group(1)) if status else None}
    return {"name": name, "state": "running", "since": await _started(int(pid.group(1)))}


async def _started(pid: int) -> float | None:
    """When a process started, from ps's elapsed time ([[dd-]hh:]mm:ss)."""
    try:
        proc = await asyncio.create_subprocess_exec("ps", "-o", "etime=", "-p", str(pid),
                                                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        raw, _ = await asyncio.wait_for(proc.communicate(), 5)
    except (OSError, TimeoutError):
        return None
    days, _, clock = raw.decode().strip().rpartition("-")
    parts = [int(p) for p in clock.split(":") if p.isdigit()]
    if not parts:
        return None
    seconds = sum(v * m for v, m in zip(reversed(parts), (1, 60, 3600))) + int(days or 0) * 86400
    return time.time() - seconds


def _vault() -> dict:
    try:
        vault.verify()
    except vault.VaultError as e:
        return {"state": "problem", "problem": str(e)}
    return {"state": "ok", "secrets": len(vault.names())}


@action
async def system(_: dict) -> dict:
    found = shutil.which("launchctl")
    services = await asyncio.gather(*(_launchd(label) for label in setup_local.SERVICES)) if found else []
    return {"services": list(services), "vault": await asyncio.to_thread(_vault)}


async def call(name: str, body: dict) -> object:
    """Run the action `name` on the request's body. Raises KeyError for an unknown action, ValueError (and the
    runner client's errors) for a request it can't do, with what to say."""
    return await ACTIONS[name](body)
