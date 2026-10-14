"""Control panel actions: POST /api/panel/ACTION (jarvis.server checks the passcode first).

Each action takes the JSON body and returns what the page shows. Tasks, approvals and the machines' live state go
through the task runner (tasks.client); everything else (schedules, to-dos, projects, memory, vocabulary, skills,
accounts, devices, voiceprints, history, pairing codes) is changed directly. Machines are paired only here, never by
voice, and only by the owner's login: a code made here, run on the machine, and its fingerprint confirmed here. Keys for connections and JARVIS
itself are entered only here, write-only, so they're never spoken or typed to the brain.

Deliberately not possible here, since anyone with a login can reach the page: showing secrets, adding MCP servers or
changing their command or tool access, changing a project's folder. Those need voice with a yes, or the computer's
command line. Google accounts are added and signed in here (jarvis.integrations.google_signin): a sign-in needs the
Google account's own password and consent, so a login alone gains nothing.
"""

import asyncio
import base64
import hashlib
import logging
import re
import shutil
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, replace
from pathlib import Path

from jarvis import claude_login, setup_local
from jarvis.brain import usage
from jarvis.core import claude_logins, config, credentials, logins, vault
from jarvis.core.database import COMMAND, WAIT_APPROVAL, edits
from jarvis.integrations import accounts, google_signin, mcp_servers
from jarvis.node import keys, machines
from jarvis.notify import apns, fcm, push
from jarvis.safety import remembered
from jarvis.skills import home as skills, review
from jarvis.state import activity, history, memory, settings, todo, vocabulary, voiceprints
from jarvis.tasks import client, data_edits, hold, projects, schedules
from jarvis.voice import speaker

log = logging.getLogger("jarvis.panel")

LOG_LINES = 40  # latest task log lines (runner's maximum)
SERVICE_NAMES = {"livekit": "LiveKit", "agent": "Voice agent", "web": "Web", "tasks": "Task runner"}
GIT_TIMEOUT = 10
HIDDEN = ("instruction", "summary", "nudges", "edits", "session_id", "log")  # omitted from task lists

Action = Callable[[dict], Awaitable[object]]
ACTIONS: dict[str, Action] = {}


def action(fn: Action) -> Action:
    """Register an action under the function's name (trailing _ stripped)."""
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
    """Waiting only on approvals, not on a question."""
    return task["status"] == "waiting" and task.get("wait_kind") == WAIT_APPROVAL


def _task(task: dict, jobs: dict[int, str] | None = None) -> dict:
    """Task for list views: HIDDEN fields dropped, plus approval/edit flags and its job's title (from `jobs`)."""
    kept = {k: v for k, v in task.items() if k not in HIDDEN}
    return kept | {"requests_only": _waits_on_requests(task), "pending_edits": len(data_edits.pending(task)),
                   "worktree": bool(task["worktree"]), "job_title": (jobs or {}).get(task.get("job"))}


async def _job_titles(found: list[dict]) -> dict[int, str]:
    return await asyncio.to_thread(schedules.titles, {t["job"] for t in found if t.get("job") is not None})


@action
async def tasks(_: dict) -> dict:
    """Recent tasks, pending approvals, upcoming jobs and do-not-disturb/usage state."""
    found, waiting = await asyncio.gather(client.tasks(), client.approvals())
    titles = await _job_titles(found)
    return {"tasks": [_task(t, titles) for t in found], "approvals": waiting, "schedules": await _jobs(),
            **await _quiet()}


@action
async def task(body: dict) -> dict:
    """Full task: instruction, summary, latest log lines and suggested edits."""
    found = await client.log(_id(body), LOG_LINES)
    described = [{"n": e["n"], "status": e["status"], "what": data_edits.describe(e), "reason": e.get("reason", ""),
                  "needs_yes": e.get("needs_yes", ""), "result": e.get("result", "")} for e in edits(found)]
    return _task(found, await _job_titles([found])) | {"instruction": found["instruction"], "summary": found["summary"],
                                                       "log": found["log"], "edits": described}


@action
async def reply(body: dict) -> dict:
    """Send a task an answer, nudge or follow-up."""
    if not (message := _text(body, "message")):
        raise ValueError("The message is empty.")
    return _task(await client.reply(_id(body), message))


@action
async def retry(body: dict) -> dict:
    """Resume a failed/stopped task with its error as a hint. Command tasks are re-run as a new task instead."""
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
    """Apply or dismiss suggested edits: {"id", "action": "apply" | "dismiss", "edits": [n, ...]}."""
    numbers = body.get("edits")
    if not isinstance(numbers, list) or not numbers:
        raise ValueError("Say which edits, by number.")
    done = await client.edits(_id(body), _text(body, "action"), [int(n) for n in numbers])
    return {"outcome": done["outcome"]}


@action
async def seen(body: dict) -> dict:
    """Mark task updates as seen so JARVIS doesn't announce them."""
    ids = body.get("ids")
    if not isinstance(ids, list):
        raise ValueError("ids must be a list.")
    await client.mark_seen([int(i) for i in ids][:100])
    return {"ok": True}


@action
async def answer(body: dict) -> dict:
    """Answer an approval: {"id", "decision": "approve" | "deny", "remember"?: "once" | "session" | "always",
    "breadth"?: "exact" | "kind"}. The page shows the resulting rule before the user taps."""
    decision = body.get("decision")
    if decision not in ("approve", "deny"):
        raise ValueError("decision must be approve or deny.")
    remember, breadth = body.get("remember") or "once", body.get("breadth") or "exact"
    answered = await client.answer(_id(body), decision, "panel", remember=remember, breadth=breadth)
    return {"state": answered["state"], "outcome": answered["outcome"], "answered": answered.get("answered", False),
            "remembered": answered.get("remembered")}


# ── Remembered approvals ────────────────────────────────────────
# Created by answering requests; here they're only listed and withdrawn (safety.remembered)


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
# Created by voice only; here they're listed, inspected, paused, resumed or cancelled

JOB_FIELDS = ("id", "kind", "title", "text", "status", "next_run", "last_run", "until", "runs", "last_result",
              "task_kind", "project", "only_changes", "folder")
RUNS = 10  # latest runs shown per job


def _job(job: dict) -> dict:
    note = schedules.notepad(job)
    return {k: job[k] for k in JOB_FIELDS} | {"when": schedules.describe(job), "check": schedules.check_text(job),
                                              "does": schedules.work_text(job),
                                              "notepad": note and {"at": note["at"], "text": note["text"]}}


async def _jobs() -> list[dict]:
    """Active and paused jobs, soonest first."""
    return [_job(j) for j in await asyncio.to_thread(schedules.items)]


def _run(task: dict, job: dict) -> dict:
    """One run of a job, result text without the job-title prefix."""
    said = (task.get("spoken") or task.get("error") or task.get("activity") or "").removeprefix(f"{job['title']}: ")
    return {"id": task["id"], "status": task["status"], "created": task["created"], "updated": task["updated"],
            "hidden": bool(task.get("hidden")), "result": said, "fix": bool(task.get("fixes"))}


@action
async def schedule(body: dict) -> dict:
    """Full job plus its latest task runs. Reminders and model-free watches start no tasks; see last_run/last_result."""
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
    """Cancel a job; an in-progress run continues until its task is stopped."""
    await asyncio.to_thread(schedules.cancel, _id(body))
    return await _jobs()


# Do not disturb (tasks.hold) pauses jobs, notices and notifications; scheduled runs also wait on usage limits

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
    """Do-not-disturb state without contacting the runner."""
    return await _quiet()


@action
async def hold_on(body: dict) -> dict:
    """Start do not disturb: {"minutes": n}, {"until": "15:00"}, or neither for indefinitely."""
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


@action
async def todo_restore(body: dict) -> list[dict]:
    """Undo a delete: the item as todo_delete's caller last saw it."""
    todo.restore({"id": _id(body), "text": _text(body, "text", 500), "done": body.get("done") is True,
                  "created": body.get("created"), "done_at": body.get("done_at")})
    return todo.items()


# ── Projects ────────────────────────────────────────────────────


async def _changed(folder: Path) -> int | None:
    """Number of uncommitted changes, or None if git fails or times out."""
    try:
        proc = await asyncio.create_subprocess_exec("git", "-C", str(folder), "status", "--porcelain",
                                                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await asyncio.wait_for(proc.communicate(), GIT_TIMEOUT)
    except (OSError, TimeoutError):
        return None
    return len(out.splitlines()) if proc.returncode == 0 else None


async def _project(p: projects.Project) -> dict:
    if p.node:  # its folder is on that machine
        return {"name": p.name, "path": str(p.path), "aliases": p.aliases, "about": p.about, "in_place": p.in_place,
                "jarvis": False, "exists": True, "git": None, "changed": None, "machine": machines.where(str(p.node))}
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
    """Update aliases, about and mode. The folder can't be changed here."""
    in_place = body.get("in_place")
    await asyncio.to_thread(projects.update, _text(body, "name"), None, _aliases(body),
                            _text(body, "about", 300) if "about" in body else None,
                            None if in_place is None else bool(in_place))
    return await projects_({})


@action
async def project_add(body: dict) -> list[dict]:
    """Register a project at an existing folder, or a new one under the projects folder if no path."""
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

SKILL_TEXT = 60_000  # max SKILL.md chars shown


def _skill(s: skills.Skill) -> dict:
    return s.brief() | {"enabled": s.enabled, "needs_approval": s.needs_approval, "kind": s.source,
                        "created": s.created, "updated": s.updated}


@action
async def skills_(_: dict) -> list[dict]:
    """All skills with their summary fields."""
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
    """Full skill: SKILL.md, files (scripts marked) and latest review."""
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
    """Enable/disable. Enabling unapproved scripts needs approve: true, sent after the page shows the current review."""
    return await asyncio.to_thread(_switch, _text(body, "name", 100), bool(body.get("on")), bool(body.get("approve")))


@action
async def skill_review(body: dict) -> dict:
    """Run a script review (one model call) for the page to show before approval."""
    name = _text(body, "name", 100)
    await review.run(name)
    return await asyncio.to_thread(_skill_detail, name)


@action
async def skill_delete(body: dict) -> list[dict]:
    """Delete a skill and its folder (the page confirms first)."""
    await asyncio.to_thread(skills.delete, _text(body, "name", 100))
    return await skills_({})


# ── Memory and "about you" ──────────────────────────────────────


def _memory(change: Callable[[], object] = lambda: None) -> dict:
    """Apply `change`, then return facts by section with usage vs limits. memory.Full becomes a ValueError."""
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
    """Replace one memory line (exact text as listed); an empty fact forgets it."""
    return _memory(lambda: memory.replace_fact(str(body.get("line", "")), _text(body, "fact", 1000)))


# ── Speech vocabulary ───────────────────────────────────────────


def _heard(body: dict) -> list[str]:
    """Mishearings from a list or comma-separated text."""
    value = body.get("heard", [])
    if isinstance(value, str):
        value = value.split(",")
    if not (isinstance(value, list) and all(isinstance(h, str) for h in value)):
        raise ValueError("heard must be text.")
    return [h.strip() for h in value if h.strip()][:vocabulary.MAX_HEARD]


def _vocabulary(change: Callable[[], object] = lambda: None) -> dict:
    """Apply `change`, then return entries, extra project terms, the cap and the prompt setting."""
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
    """Replace an entry (by its current word) with the given word and mishearings."""
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
    """Set one preference {"key", "value"}; empty resets to default."""
    settings.set_preference(_text(body, "key", 40), _text(body, "value", 100))
    return await asyncio.to_thread(settings.preferences)


# ── Voiceprints (speaker check) ─────────────────────────────────

MAX_ENROLL_SECONDS = 90  # max recording length accepted


def _voiceprints() -> dict:
    """Owner voiceprint (or None), model presence, and the enrollment passage and duration."""
    owner = next((p.described() for p in voiceprints.every() if p.name == voiceprints.OWNER), None)
    return {"owner": owner, "model": config.SPEAKER_MODEL.is_file(), "passage": speaker.PASSAGE,
            "seconds": speaker.RECORD_SECONDS}


@action
async def voiceprint(_: dict) -> dict:
    return await asyncio.to_thread(_voiceprints)


@action
async def voiceprint_enroll(body: dict) -> dict:
    """Enroll from {"audio": base64 16 kHz mono s16 PCM} (~30 s of reading). Replaces the voiceprint; raises
    ValueError if one can't be made."""
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
    """Forget a voiceprint {"name"}; defaults to the owner's."""
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


@action
async def account_add(body: dict) -> list[dict]:
    """Add an account to sign in next: {"label", "note", "chat" (Workspace with Google Chat)}."""
    account = accounts.Account(_text(body, "label", 40), note=_text(body, "note", 300), chat=body.get("chat") is True)

    def add() -> list[dict]:
        with accounts.change() as found:
            found.add(account)
        return _accounts()

    return await asyncio.to_thread(add)


@action
async def account_remove(body: dict) -> list[dict]:
    """Remove an account and delete its sign-in: {"label"}."""
    label = _text(body, "label")

    def remove() -> list[dict]:
        with accounts.change() as found:
            found.remove(label)
            accounts.remove_files(label)  # inside the transaction: a failed delete keeps the account
        return _accounts()

    return await asyncio.to_thread(remove)


@action
async def google_setup(body: dict) -> dict:
    """The OAuth client's state for the Google accounts page: {"origin"} is the panel's address."""
    return await asyncio.to_thread(google_signin.signins.setup, _text(body, "origin", 300))


@action
async def google_sign_in(body: dict) -> dict:
    """Start signing an account in: {"label", "step" (Chat accounts' scope step, 0 first), "origin"}. Returns Google's
    URL to open and the state to follow."""
    return await asyncio.to_thread(google_signin.start, _text(body, "label", 40), _id(body, "step"),
                                   _text(body, "origin", 300))


@action
async def google_sign_in_status(body: dict) -> dict:
    """How a started sign-in went, waiting a while for it to come back: {"state"}; with the accounts once it's over."""
    found = await google_signin.signins.wait(_text(body, "state", 300))
    return found | ({"accounts": await asyncio.to_thread(_accounts)} if found["state"] != "waiting" else {})


@action
async def google_check(body: dict) -> dict:
    """Check a signed-in account's Google APIs again (after turning one on in Google Cloud): {"label"}."""
    label = _text(body, "label", 40)
    return await asyncio.to_thread(lambda: asdict(google_signin.recheck(label)) | {"accounts": _accounts()})


@action
async def google_sign_in_paste(body: dict) -> dict:
    """Finish a sign-in from the address Google's redirect ended on (a phone with a Desktop app client): {"address"}.
    How it went reaches the panel through google_sign_in_status, which is waiting on it."""
    await google_signin.signins.finish_pasted(_text(body, "address"))
    return {}


# ── Notification devices ────────────────────────────────────────


def _device_id(secret: str) -> str:
    """Non-reversible id for a push endpoint or app token."""
    return hashlib.sha256(secret.encode()).hexdigest()[:16]


def _devices(endpoint: str = "") -> list[dict]:
    web = [{"id": _device_id(s["endpoint"]), "kind": "web", "device": s.get("device") or "browser",
            "added": s.get("added"), "this": s["endpoint"] == endpoint} for s in push.Subscriptions().all()]
    app = [{"id": _device_id(d["token"]), "kind": "app", "device": d.get("device") or "iPhone", "added": d.get("added"),
            "this": False} for d in apns.Devices().all()]
    android = [{"id": _device_id(d["token"]), "kind": "app", "device": d.get("device") or "android",
                "added": d.get("added"), "this": False} for d in fcm.Devices().all()]
    return web + app + android


@action
async def devices(body: dict) -> list[dict]:
    """Devices receiving notifications; the caller's own endpoint is marked `this`."""
    return _devices(str(body.get("endpoint", "")))


@action
async def device_remove(body: dict) -> list[dict]:
    gone = _text(body, "id", 32)
    subs = push.Subscriptions()
    subs.remove([s["endpoint"] for s in subs.all() if _device_id(s["endpoint"]) == gone])
    for store in (apns.Devices(), fcm.Devices()):
        store.remove({d["token"] for d in store.all() if _device_id(d["token"]) == gone})
    return _devices(str(body.get("endpoint", "")))


# ── Machines (jarvis.node): pairing, confirming, where jobs run ─
# Pairing itself is finished on the machine (make node-pair), with a code made here. Only the owner's login pairs,
# confirms or forgets a machine, since a machine runs the owner's work.

PAIRING = "pair, confirm or forget machines"


async def _machines(body: dict) -> dict:
    """The machines as the page shows them, and whether this login may pair them."""
    return await client.machines() | {"owner": await asyncio.to_thread(logins.owner),
                                      "can_pair": await asyncio.to_thread(logins.is_owner, str(body.get("login", "")))}


def _owner_only(body: dict) -> None:
    logins.owner_only(str(body.get("login", "")), PAIRING)


@action
async def machines_(body: dict) -> dict:
    """Paired machines, each checked online just now, and where each kind of job runs."""
    return await _machines(body)


@action
async def machine_code(body: dict) -> dict:
    """A one-time pairing code, with this computer's fingerprint for the machine's screen to match."""
    await asyncio.to_thread(_owner_only, body)
    made = await asyncio.to_thread(machines.new_code)
    return made | {"main_fingerprint": keys.fingerprint(await asyncio.to_thread(machines.main_public))}


@action
async def machine_confirm(body: dict) -> dict:
    """The user checked a pending machine's fingerprint: it may connect."""
    await asyncio.to_thread(_owner_only, body)
    found = await asyncio.to_thread(machines.confirm, _id(body))
    await asyncio.to_thread(activity.add, f"Paired {found.name} in the control panel.", "ok")
    return await _machines(body)


@action
async def machine_save(body: dict) -> dict:
    """Rename a machine: {"id", "name", "aliases"}."""
    await asyncio.to_thread(machines.rename, _id(body), _text(body, "name", machines.NAME_MOST), _aliases(body))
    return await _machines(body)


@action
async def machine_remove(body: dict) -> dict:
    """Forget a machine (the page confirms first); it must pair again to come back."""
    await asyncio.to_thread(_owner_only, body)
    gone = await client.machine_remove(_id(body))
    await asyncio.to_thread(activity.add, f"Removed {gone['removed']} in the control panel.", "warn")
    return await _machines(body)


@action
async def placement_save(body: dict) -> dict:
    """Where a kind of job runs: {"kind": "commands" | "tasks", "value": "here" | "auto" | machine id}."""
    await asyncio.to_thread(machines.set_placement, _text(body, "kind", 20), _text(body, "value", 20))
    return await _machines(body)


# ── Bell ───────────────────────────────────────────────────────


@action
async def activity_(_: dict) -> list[dict]:
    """Recent activity, newest first."""
    return await asyncio.to_thread(activity.recent)


# ── Conversation history (read-only) ────────────────────────────


@action
async def conversations(body: dict) -> dict:
    """Days with conversations, newest first, paged by {"before": "YYYY-MM-DD"}."""
    return await asyncio.to_thread(history.days, _text(body, "before", 10))


@action
async def conversation(body: dict) -> dict:
    """One day's exchanges, newest first: {"date": "YYYY-MM-DD"}."""
    return await asyncio.to_thread(history.day, _text(body, "date", 10))


@action
async def conversation_search(body: dict) -> dict:
    """Exchanges containing every word of {"text"}, newest first."""
    return await asyncio.to_thread(history.search, _text(body, "text", 200))


# ── Logins and JARVIS's own keys ────────────────────────────────
# Passcodes (core.logins) and keys (core.credentials) are set only here: never read back, never by voice


def _access(login: str) -> dict:
    """Logins (caller marked) and key status/source; never key values."""
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
    """Add a login {"name", "passcode_new"}; passcodes must be unique."""
    name = _text(body, "name", 40)
    await asyncio.to_thread(logins.add, name, _passcode(body))
    await asyncio.to_thread(activity.add, f"Added the login {name} in the control panel.")
    return await access(body)


@action
async def login_passcode(body: dict) -> dict:
    """Change a login's passcode: {"name", "passcode_new"}."""
    await asyncio.to_thread(logins.set_passcode, _text(body, "name", 40), _passcode(body), str(body.get("login", "")))
    return await access(body)


@action
async def login_remove(body: dict) -> dict:
    """Remove a login (never the last): {"name"}. Its devices must log in again."""
    name = _text(body, "name", 40)
    await asyncio.to_thread(logins.remove, name, str(body.get("login", "")))
    await asyncio.to_thread(activity.add, f"Removed the login {name} in the control panel.")
    return await access(body)


def _set_key(body: dict) -> None:
    key = credentials.need(_text(body, "name", 40))
    credentials.put(key, str(body.get("value", "")))
    if key in (credentials.LIVEKIT_KEY, credentials.LIVEKIT_SECRET):
        setup_local.write_livekit_config()  # picked up when LiveKit next starts


@action
async def key_set(body: dict) -> dict:
    """Store a JARVIS key in the vault: {"name", "value"}. Write-only; used after services restart."""
    await asyncio.to_thread(_set_key, body)
    return await access(body)


@action
async def key_clear(body: dict) -> dict:
    """Clear an optional key: {"name"}. Falls back to .env if set there."""
    await asyncio.to_thread(credentials.clear, credentials.need(_text(body, "name", 40)))
    return await access(body)


# ── Claude logins (core.claude_logins, jarvis.claude_login) ─────
# Signed in here through the web service, in the desktop session, so each lands in the keychain the services read.
# JARVIS runs on the first usable one in the priority order set here. Only the owner changes them: a login is a Claude
# account and its usage. "" names the default login.

CLAUDE_LOGINS = "change JARVIS's Claude logins"


def _claude_login(body: dict) -> str:
    return claude_logins.known(_text(body, "name", 40))


def _claude_row(name: str, active: str, checks: dict) -> dict:
    said = claude_login.status(name)
    found = claude_logins.account(name)
    return {"name": name, "active": name == active, "skipped": claude_logins.skipped(name),
            "signed_in": bool(said.get("loggedIn")),
            "email": said.get("email") or found.get("emailAddress"), "org": said.get("orgName"),
            "plan": said.get("subscriptionType"), "check": checks.get(name),
            "usage": usage.said(kept=usage.of(name)) if usage.of(name) else ""}


async def _claude_logins(body: dict) -> dict:
    active, checks = await asyncio.to_thread(claude_logins.active), await asyncio.to_thread(claude_logins.checks)
    names = await asyncio.to_thread(claude_logins.order)
    rows = await asyncio.gather(*(asyncio.to_thread(_claude_row, name, active, checks) for name in names))
    return {"logins": list(rows), "can_change": await asyncio.to_thread(logins.is_owner, str(body.get("login", "")))}


def _claude_owner_only(body: dict) -> None:
    logins.owner_only(str(body.get("login", "")), CLAUDE_LOGINS)


@action
async def claude_logins_(body: dict) -> dict:
    """The Claude logins JARVIS can run on, by priority: each one's account, last check and usage, why it's passed
    over if it is, and which one JARVIS runs on now."""
    return await _claude_logins(body)


@action
async def claude_login_start(body: dict) -> dict:
    """Start signing a login in: {"name" ("" = the default; new = add it), "email" (optional, prefilled)}. Returns
    Claude's sign-in link and the state to finish with."""
    await asyncio.to_thread(_claude_owner_only, body)
    raw = _text(body, "name", 40)
    name = claude_logins.valid(raw) if raw else claude_logins.DEFAULT
    return await asyncio.to_thread(claude_login.signins.start, name, _text(body, "email", 200))


@action
async def claude_login_finish(body: dict) -> dict:
    """Finish a sign-in with the code Claude's page showed: {"state", "code"}. Checks the login, then lists them."""
    await asyncio.to_thread(_claude_owner_only, body)
    ok, said = await asyncio.to_thread(claude_login.signins.finish, _text(body, "state", 100), _text(body, "code", 2000))
    if ok:
        await asyncio.to_thread(activity.add, "Signed a Claude login in from the control panel.")
    return {"ok": ok, "said": said} | await _claude_logins(body)


@action
async def claude_login_cancel(body: dict) -> dict:
    await asyncio.to_thread(claude_login.signins.cancel, _text(body, "state", 100))
    return {}


@action
async def claude_login_check(body: dict) -> dict:
    """Check JARVIS's services can use a login: {"name"}. One tiny model call."""
    await asyncio.to_thread(_claude_owner_only, body)
    ok, said = await asyncio.to_thread(claude_login.check, _claude_login(body))
    return {"ok": ok, "said": said} | await _claude_logins(body)


@action
async def claude_login_order(body: dict) -> dict:
    """Set the priority order: {"names": every login, first = most wanted}. Applies from JARVIS's next call; tasks
    already started stay on theirs while it's usable."""
    await asyncio.to_thread(_claude_owner_only, body)
    names = body.get("names")
    if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
        raise ValueError("Send the logins' names, in order.")
    await asyncio.to_thread(claude_logins.set_order, names)
    await asyncio.to_thread(activity.add, "Changed the Claude logins' order in the control panel: "
                                          + " > ".join(n or "default" for n in names) + ".")
    return await _claude_logins(body)


@action
async def claude_login_remove(body: dict) -> dict:
    """Sign one of JARVIS's own logins out and forget it: {"name"}. In use: JARVIS goes back to the default."""
    await asyncio.to_thread(_claude_owner_only, body)
    name = _claude_login(body)
    await asyncio.to_thread(claude_login.sign_out, name)
    await asyncio.to_thread(activity.add, f"Removed the Claude login {name} in the control panel.")
    return await _claude_logins(body)


# ── Connections and system ──────────────────────────────────────


SECRET_MOST = 8000  # max key length


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
    """MCP servers: state, provenance, tools by access, and which secrets are set (never values)."""
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
    """Enable/disable a connection: {"name", "on"}. Unreviewed ones can't be enabled."""
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
    log.warning("the panel set %s's %s", server.name, var)  # name only, never the value
    return _servers()


@action
async def server_secret(body: dict) -> list[dict]:
    """Store a connection secret: {"name", "var", "value"}. Write-only."""
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
    """Clear a connection secret: {"name", "var"}."""
    return await asyncio.to_thread(_clear_secret, body)


@action
async def server_sign_in(body: dict) -> dict:
    """OAuth sign-in for a connection in JARVIS's browser on the computer; waits up to SIGN_IN_MINUTES: {"name"}."""
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
    """What removing a connection would affect, shown before confirming: {"name"}."""
    from jarvis.tools.connections import impact

    return {"impact": await asyncio.to_thread(lambda: impact(_connection(body)))}


@action
async def server_remove(body: dict) -> list[dict]:
    """Remove a connection with its keys and sign-in: {"name"}."""
    server = await asyncio.to_thread(_connection, body)
    await asyncio.to_thread(mcp_servers.remove, server.name)
    await asyncio.to_thread(activity.add, f"Removed {server.label} in the control panel.")
    return await asyncio.to_thread(_servers)


async def _launchd(label: str) -> dict:
    """Service state from `launchctl list`: running (with start time), stopped (exit code) or not loaded."""
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
    """Process start time from ps etime ([[dd-]hh:]mm:ss)."""
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


async def _systemd(label: str) -> dict:
    """Service state from `systemctl --user show`: running (with start time), stopped (exit code) or not loaded."""
    out = ""
    try:
        proc = await asyncio.create_subprocess_exec(
            "systemctl", "--user", "show", "-p", "LoadState,ActiveState,ExecMainStatus,ExecMainStartTimestampMonotonic",
            f"{label}.service", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        raw, _ = await asyncio.wait_for(proc.communicate(), 5)
        out = raw.decode(errors="replace") if proc.returncode == 0 else ""
    except (OSError, TimeoutError):
        pass
    name = SERVICE_NAMES.get(label.rsplit(".", 1)[-1], label)
    props = setup_local.properties(out)
    if props.get("LoadState", "not-found") == "not-found":
        return {"name": name, "state": "not loaded"}
    if props.get("ActiveState") not in ("active", "reloading"):
        status = props.get("ExecMainStatus", "")
        return {"name": name, "state": "stopped", "exit": int(status) if status.lstrip("-").isdigit() else None}
    # Monotonic microseconds: the same clock as time.monotonic() on Linux, and no date string to parse
    started = props.get("ExecMainStartTimestampMonotonic", "")
    since = time.time() - (time.monotonic() - int(started) / 1e6) if started.isdigit() and int(started) else None
    return {"name": name, "state": "running", "since": since}


def _vault() -> dict:
    try:
        vault.verify()
    except vault.VaultError as e:
        return {"state": "problem", "problem": str(e)}
    return {"state": "ok", "secrets": len(vault.names())}


@action
async def system(_: dict) -> dict:
    systemd = setup_local.MANAGER == "systemd"
    found, probe = (shutil.which("systemctl"), _systemd) if systemd else (shutil.which("launchctl"), _launchd)
    labels = [config.service_label(name) for name in setup_local.SERVICES]
    services = await asyncio.gather(*(probe(label) for label in labels)) if found else []
    return {"services": list(services), "vault": await asyncio.to_thread(_vault)}


async def call(name: str, body: dict) -> object:
    """Run action `name`. Raises KeyError if unknown; ValueError or runner client errors with a user-facing message."""
    return await ACTIONS[name](body)
