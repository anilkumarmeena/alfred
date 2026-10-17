"""Runner mixin for slotless tasks: command tasks (see command_jobs) and connection tasks (see connection_jobs)."""

import asyncio
import json
import logging
import time
from pathlib import Path

from jarvis import projects
from jarvis.contracts.remote import COMMANDS, HERE, HERE_WORD, Place, TurnedDown
from jarvis.llm import claude_logins, usage
from jarvis.safety.commands import secret_path
from jarvis.state import activity as bell
from jarvis.tasks import command_jobs, connection_jobs, scheduler, schedules
from jarvis.tasks.approvals import SAID_MOST
from jarvis.tasks.command_jobs import ASK, DONE, FAILED, FIX, LOST, REPORT, STOPPED, TIMEOUT, last_lines, spec, took
from jarvis.tasks.runner_base import RunnerBase
from jarvis.tasks.scheduler import sentence
from jarvis.tasks.store import ACTIVE, COMMAND, CONNECTION, WAIT_QUESTION

log = logging.getLogger("jarvis.tasks.runner")
COMMAND_POLL = 2.0  # seconds between polls of running command and connection tasks


class CommandTasks(RunnerBase):
    """Start, watch and announce command and connection tasks."""

    def start_command(self, command: str, title: str, folder: str | Path, job: int | None = None,
                      project: str | None = None, timeout_minutes: int | None = None, log_file: str | None = None,
                      summarize: bool = True, auto_fix: bool = True, report: bool = False, attempt: int = 1,
                      after: dict | None = None, placeable: bool = False, machine: str = "", approved: str = "",
                      tried: list[str] | None = None) -> dict:
        """Start a command task in absolute `folder` (~ allowed); takes no task slot.

        `job`: owning scheduled job. `log_file`: glob of the command's own log, read with its output. `project`
        defaults to the folder's. A post-fix rerun has `attempt` 2 and `after` = {task, fix, failed, fixed}.
        `placeable`: it may run on another machine (Settings › Machines, or `machine` where the user said), when its
        folder is in a project; `approved` says how a risky command got the user's yes, for that machine to check.
        `tried`: the Claude logins earlier runs of it stopped on (`_fall_over`).
        Raises ValueError for an empty command or an unusable folder."""
        self._open()
        command = command.strip()
        if not command:
            raise ValueError("The command is empty.")
        path = Path(folder).expanduser()
        if not path.is_absolute():
            raise ValueError("Give the folder as a full path (~ allowed).")
        if secret_path(path):
            raise ValueError("That folder holds secrets, which stay out of reach.")
        there = projects.living_at(path)  # a project that lives on another machine: its folder is there
        if not there and not path.is_dir():
            raise ValueError(f"{path} isn't a folder.")
        if project is None and (auto_fix or there):
            project = there.name if there else command_jobs.project_of(path)
        title = " ".join(title.split())[:80] or command[:60]
        how = {"folder": str(path), "timeout_minutes": timeout_minutes or None, "log_file": log_file or None,
               "summarize": bool(summarize), "auto_fix": bool(auto_fix), "report": bool(report), "attempt": attempt}
        if after:
            how["after"] = after
        if tried:
            how["tried"] = tried
        if placeable:
            how |= {"placeable": True} | ({"approved": approved} if approved else {})
        owner = there.name if there else command_jobs.project_of(path) if placeable else None
        if there:
            machine = ""  # it runs where its project lives
        elif machine and machine.strip().lower() != HERE_WORD:
            if not owner:
                raise ValueError("Only a command in one of the projects can run on another machine: its project is "
                                 "copied there.")
            self.machines.find(machine)  # an unknown name is said now, before anything starts
        task = self.store.create(COMMAND, title, command, project, job=job, command=how)
        task_id = task["id"]
        if owner and (there or machine or self.machines.paired()):
            self._said_start(task, attempt, after, job)
            self.placing[task_id] = placing = asyncio.create_task(
                self._place_command(task_id, path, projects.find(owner), how, machine))
            placing.add_done_callback(lambda _: self.placing.pop(task_id, None))
            return self.store.update(task_id, status="running", activity="choosing where it runs",
                                     command=json.dumps(how))
        self._start_here(task_id, command, title, path, how)
        self._said_start(task, attempt, after, job)
        return self.store.update(task_id, status="running", activity="running", command=json.dumps(how))

    def _start_here(self, task_id: int, command: str, title: str, path: Path, how: dict) -> None:
        """Start the command on this computer; ValueError (recorded on the task) if it can't."""
        try:
            record = self.commands.start(task_id, command, title, path)
            how |= {"started": record["started"], "login": record["login"]}
        except (ValueError, OSError) as e:
            self.store.log(task_id, "failed", f"couldn't start: {e}")
            self.store.set(task_id, status="failed", error=f"Couldn't start: {e}"[:300], spoken=f"Couldn't start: {e}"[:300])
            raise ValueError(f"{e}") from e

    def _said_start(self, task: dict, attempt: int, after: dict | None, job: int | None) -> None:
        task_id = task["id"]
        self.store.log(task_id, "start", f"in {command_jobs.spec(task)['folder']}: {task['instruction']}")
        if attempt > 1:
            self.store.log(task_id, "rerun", f"running again after task {after['fix']}'s fix" if after else "running again")
        bell.add(f"Started {'scheduled command' if job is not None else 'command'} {task_id}: {task['title']}", "run",
                 task_id)

    async def _place_command(self, task_id: int, path: Path, owner: projects.Project, how: dict, machine: str) -> None:
        """Choose where a new command runs, then start it there: on its machine (its project copied over), or here
        when none is chosen, or sending it fails (said with the start)."""
        task = self.store.get(task_id)
        job = {"kind": "long command", "title": task["title"], "command": task["instruction"][:500],
               "folder": str(path), "project": owner.name}
        if owner.node:  # it lives there, so it runs there or not at all
            name = self.offload.name(owner.node)
            if self.hub.connected(owner.node) is None:
                self._finish(task_id, "failed", f"Task {task_id}, {task['title']}, didn't start: {name}, where "
                                                f"{owner.name} lives, isn't online.", error=f"{name} isn't online")
                return
            place = Place(owner.node, name, f"It's running on {name}, where {owner.name} lives.")
        else:
            try:
                place = await self.offload.place(COMMANDS, job, machine)
            except Exception as e:
                log.warning("command task %s: choosing its machine failed: %s", task_id, e)
                place = HERE
        if place.node:
            self._placed(task_id, place)
            try:
                await self.offload.start_command(self.store.get(task_id), place.node, owner, path,
                                                 how.get("approved", ""))
            except asyncio.CancelledError:
                raise
            except TurnedDown as e:  # the machine's own check turned it down: it doesn't run anywhere
                await self.offload.forget(place.node, self.offload.key(task_id))
                self.store.set(task_id, node=None)
                self._finish(task_id, "failed", f"Task {task_id}, {task['title']}, didn't start: {place.name} turned "
                                                f"it down, as {e}."[:400], error=str(e)[:300])
                return
            except Exception as e:
                log.warning("command task %s couldn't start on %s: %s", task_id, place.name, e)
                if owner.node:  # its folder is only there
                    self._finish(task_id, "failed", f"Task {task_id}, {task['title']}, couldn't start on "
                                                    f"{place.name}: {e}"[:400], error=str(e)[:300])
                    return
                await self.offload.forget(place.node, self.offload.key(task_id))
                place = Place(said=f"It couldn't be sent to {place.name} ({e}), so it's running on this "
                                             "computer instead."[:400])
                self.store.set(task_id, node=None)
            else:
                how["started"] = time.time()
                self.store.set(task_id, activity="running", command=json.dumps(how))
                return
        self._placed(task_id, place)
        try:
            self._start_here(task_id, task["instruction"], task["title"], path, how)
        except ValueError as e:
            self._finish(task_id, "failed", f"Task {task_id}, {task['title']}, couldn't start: {e}"[:400])
            return
        self.store.set(task_id, activity="running", command=json.dumps(how))

    def rerun(self, task_id: int) -> dict:
        """Rerun a finished command task with the same settings, as a new task (the panel's Retry)."""
        task = self._get(task_id)
        if task["kind"] != COMMAND:
            raise ValueError(f"Task {task_id} isn't a command; reply to it to carry on.")
        if task["status"] in ACTIVE:
            raise ValueError(f"Task {task_id} is {task['status']}; it can run again once it has ended.")
        how = spec(task)
        again = self._again(task, how)
        self.store.log(task_id, "rerun", f"run again as task {again['id']}")
        if task["job"] is not None and (job := schedules.get(task["job"])) and job["last_task"] == task_id:
            schedules.update(job["id"], last_task=again["id"], run=json.dumps(self._run_of(again, spec(again))))
        return again

    def _again(self, task: dict, how: dict, **changes) -> dict:
        """Start command task `task` (spec `how`) again with the same settings, plus `changes` to start_command's."""
        return self.start_command(task["instruction"], task["title"], how["folder"], task["job"], task["project"],
                                  how.get("timeout_minutes"), how.get("log_file"), how.get("summarize", True),
                                  how.get("auto_fix", True), how.get("report", False),
                                  placeable=bool(how.get("placeable")), approved=how.get("approved", ""), **changes)

    async def stop_command(self, task_id: int) -> dict:
        """Stop a running command (gracefully, then forcibly). ValueError if it isn't running."""
        task = self._get(task_id)
        if task["kind"] != COMMAND or task["status"] != "running":
            raise ValueError(f"Task {task_id} isn't running a command.")
        if task_id in self.ending:
            raise ValueError(f"Task {task_id}'s command has just ended.")
        self.ending[task_id] = asyncio.current_task()  # keep check_commands off it
        try:
            if placing := self.placing.get(task_id):  # not started anywhere yet
                placing.cancel()
            else:
                try:
                    await self._stop_run(task)
                except ValueError as e:  # already ended; check_commands handles it
                    raise ValueError(f"Task {task_id}'s command had already ended ({e}).") from None
            await self._command_ended(task_id, STOPPED, None, time.time())
        finally:
            self.ending.pop(task_id, None)
        return self.store.get(task_id)

    async def _stop_run(self, task: dict) -> None:
        """Stop the command, here or on its machine (where its copy then goes too). ValueError if it had ended."""
        if task.get("node"):
            await self.offload.stop_command(task)
            await self.offload.forget(task["node"], self.offload.key(task["id"]))
        else:
            await self.commands.stop(task["id"])

    async def _watch_commands(self) -> None:
        while True:
            try:
                self.check_commands()
                self.check_connections()
            except Exception:
                log.exception("looking after the command tasks failed")
            await asyncio.sleep(COMMAND_POLL)

    def check_commands(self, now: float | None = None) -> None:
        """Poll running command tasks: end finished ones and stop overdue ones in the background; else show the latest
        output line as activity."""
        now = now or time.time()
        for task in self.store.active():
            if task["kind"] != COMMAND or task["status"] != "running" or task["id"] in self.ending \
                    or task["id"] in self.placing:
                continue
            how, record = spec(task), self.commands.get(task["id"])
            ended = self.commands.how(record) if record else (LOST, None)
            if ended is not None:
                self._end_in_background(task["id"], self._command_ended(task["id"], *ended, now))
            elif (limit := how.get("timeout_minutes")) and now - how.get("started", task["created"]) >= limit * 60:
                self._end_in_background(task["id"], self._time_up(task["id"]))
            elif (line := command_jobs.last_line(command_jobs.tail(self.commands.log_file(task["id"]), 400))[:200]) \
                    and line != task["activity"]:
                self.store.set(task["id"], activity=line)

    def _end_in_background(self, task_id: int, coro) -> None:
        self.ending[task_id] = ending = asyncio.create_task(coro)
        ending.add_done_callback(lambda _: self.ending.pop(task_id, None))

    async def drain_commands(self) -> None:
        """Wait until every in-flight command ending has finished (for tests)."""
        while self.ending:
            await asyncio.gather(*self.ending.values(), return_exceptions=True)

    async def _time_up(self, task_id: int) -> None:
        try:
            await self._stop_run(self.store.get(task_id))
        except ValueError:  # it ended on its own just now
            record = self.commands.get(task_id)
            if record and (ended := self.commands.how(record)) and ended[0] != STOPPED:
                return await self._command_ended(task_id, *ended, time.time())
        await self._command_ended(task_id, TIMEOUT, None, time.time())

    @staticmethod
    def _job_title(task: dict) -> str:
        """Title of the task's scheduled job (pushes are grouped under it), or ""."""
        return (schedules.get(task["job"]) or {}).get("title", "") if task.get("job") is not None else ""

    @staticmethod
    def _run_of(task: dict, how: dict) -> dict:
        """The task as stored in its job's `run` field (see schedules.run_of)."""
        return {"task": task["id"], "started": how.get("started", task["created"]), "attempt": how.get("attempt", 1)} | {
            k: how[k] for k in ("ended", "outcome", "code", "summary", "fix_task") if how.get(k) is not None}

    def _settle_run(self, task: dict, how: dict, last_result: str = "", **fields) -> None:
        """Record a finished run on its scheduled job, if it is still the job's latest."""
        if task["job"] is None or not (job := schedules.get(task["job"])) or job["last_task"] != task["id"]:
            return
        schedules.update(job["id"], run=json.dumps(self._run_of(task, how)),
                         **({"last_result": last_result[:300]} if last_result else {}), **fields)

    async def _command_ended(self, task_id: int, outcome: str, code: int | None, at: float) -> None:
        """Handle a command's end: announce a stop, or hand off to `_command_done` / `_command_failed`."""
        task = self.store.get(task_id)
        if not task or task["status"] != "running":
            return
        how = spec(task) | {"outcome": outcome, "code": code, "ended": at}
        how.setdefault("started", task["created"])
        self.store.set(task_id, command=json.dumps(how))  # first, so it's announced once
        log.info("command task %s ended: %s %s", task_id, outcome, code)
        try:
            if outcome == STOPPED:
                said = f"Task {task_id}, {task['title']}, stopped after {took(at - how['started'])}."
                self._settle_run(task, how, sentence(f"Stopped after {took(at - how['started'])}"))
                self._finish(task_id, "stopped", said)
            elif outcome == DONE:
                await self._command_done(task, how)
            else:
                await self._command_failed(task, how)
        except Exception as e:  # never leave the task stuck as running
            log.exception("command task %s: its end couldn't be said", task_id)
            if self.store.get(task_id)["status"] == "running":
                self._finish(task_id, "done" if outcome == DONE else "stopped" if outcome == STOPPED else "failed",
                             f"Task {task_id}, {task['title']}, ended ({outcome}), but saying how it went failed: "
                             f"{type(e).__name__}.")

    def _after(self, how: dict) -> str:
        """For a post-fix rerun: what failed before and what was fixed."""
        after = how.get("after") or {}
        if not after:
            return ""
        return (f" It had failed before (task {after['task']}): {sentence(after.get('failed', ''))} Task {after['fix']} "
                f"fixed it: {sentence(after.get('fixed', ''))}")

    async def _command_done(self, task: dict, how: dict) -> None:
        """Announce success: `report` jobs say their last line; else a model summary, falling back to the last lines.
        An only_changes job's run that printed nothing ends quietly and hidden, with no model call."""
        task_id, seconds = task["id"], how["ended"] - how["started"]
        output = self.commands.output(task_id, command_jobs.SUMMARY_CHARS, how.get("log_file"), how["started"])
        job = schedules.get(task["job"]) if task["job"] is not None else None
        if job and job["only_changes"] and not output.strip() and not how.get("after"):
            how["summary"] = "Nothing to report."
            self.store.set(task_id, command=json.dumps(how))
            self._settle_run(task, how, how["summary"])
            self._finish(task_id, "done", f"{task['title']}: nothing to report.", quiet=True, hidden=1, seen=1,
                         summary=how["summary"])
            return
        summary = ""
        if how.get("report"):
            summary = command_jobs.last_line(output)
        elif how.get("summarize") and not usage.hold_until():
            try:
                summary = await command_jobs.summarize(task["title"], task["instruction"], seconds, output)
            except Exception as e:
                log.warning("command task %s: no summary (%s)", task_id, type(e).__name__)
        long, lines = took(seconds), last_lines(output)
        text = summary or f"Finished in {long}." + (f" Last lines: {lines}" if lines else "")
        if how.get("after"):
            spoken = (f"Task {task_id}, {task['title']}, ran again after a fix, and worked, in {long}."
                      + self._after(how) + (f" This time: {summary}" if summary else ""))
        else:
            spoken = f"Task {task_id}, {task['title']}, is done, in {long}." + (f" {summary}" if summary else "")
        how["summary"] = text
        self.store.set(task_id, command=json.dumps(how))
        self._settle_run(task, how, sentence(f"Succeeded in {long}: {summary}" if summary else f"Succeeded in {long}"))
        self._finish(task_id, "done", spoken, title=self._job_title(task), summary=text)

    async def _command_failed(self, task: dict, how: dict) -> None:
        """Triage a failure, then start a fix (FIX), ask the user (ASK), or just report it (REPORT)."""
        task_id, seconds = task["id"], how["ended"] - how["started"]
        why, long = command_jobs.ending(how), took(seconds)
        output = self.commands.output(task_id, command_jobs.FIX_CHARS, how.get("log_file"), how["started"])
        job = schedules.get(task["job"]) if task["job"] is not None else None
        trouble = self._login_trouble(task, how)
        if trouble and self._fall_over(task, how, job, trouble):
            return
        can_fix = bool(how.get("auto_fix") and task["project"] and how.get("attempt", 1) <= command_jobs.FIX_ATTEMPTS
                       and (job is None or job["status"] in schedules.LIVE))
        found = None
        if trouble:  # nothing a fix could change, and no other login to move to
            found = command_jobs.Triage(REPORT, f"{trouble[0].upper()}{trouble[1:]}, and no other Claude login is free "
                                                "to run it on.")
        elif not usage.hold_until():
            found = await command_jobs.triage(task["title"], task["instruction"], how["folder"], why, seconds,
                                              output[-command_jobs.TRIAGE_CHARS:], can_fix)
        if found is None:  # no triage available: fix if allowed, else report
            lines = last_lines(output)
            found = command_jobs.Triage(FIX if can_fix else REPORT, f"Last lines: {lines}" if lines else "")
        how["said"] = found.said
        self.store.log(task_id, "failed", f"{why}, after {long}. {found.said}".strip() + f" ({found.action})")
        name = (f"Task {task_id}, {task['title']}, ran again after a fix, and failed again ({why}), after {long}."
                + self._after(how) if how.get("after") else f"Task {task_id}, {task['title']}, failed ({why}), after {long}.")
        said = f" {found.said}" if found.said else ""
        if found.action == FIX:
            try:
                fix = self._start_fix(task, how, output, job)
            except ValueError as e:  # e.g. the project is gone
                found, said = command_jobs.Triage(REPORT, found.said), f"{said} No task could fix it: {e}"
            else:
                how["fix_task"] = fix["id"]
                self.store.set(task_id, command=json.dumps(how))
                self._settle_run(task, how, sentence(f"Failed after {long}: {why}") + f" Task {fix['id']} is fixing it.",
                                 last_task=fix["id"])
                # No push now: the rerun's announcement covers the failure, the fix and the result
                self._finish(task_id, "failed", f"{name}{said} Task {fix['id']} is fixing it, and runs it again after.",
                             alert=False, error=why)
                return
        self.store.set(task_id, command=json.dumps(how))
        if found.action == ASK:
            needs = f" Fixing it would mean {found.needs.rstrip('.')}." if found.needs else ""
            question = f"{found.said}{needs} Shall I start a task to fix it?".strip()
            self._settle_run(task, how, sentence(f"Failed after {long}: {why}") + " Waiting for your yes to fix it.")
            self._finish(task_id, "waiting", f"{name}{said}{needs} Shall I start a task to fix it?", question=question,
                         wait_kind=WAIT_QUESTION, title=self._job_title(task), error=why)
            return
        self._settle_run(task, how, sentence(f"Failed after {long}: {why}") + said)
        self._finish(task_id, "failed", f"{name}{said}", title=self._job_title(task), error=why)

    def _login_trouble(self, task: dict, how: dict) -> str:
        """Why the run failed, if its Claude login stopped it (signed out, or used up), which JARVIS then passes over;
        else "". Only runs here know their login."""
        if how.get("login") is None or how["outcome"] != FAILED:
            return ""
        return command_jobs.pass_over(how["login"], command_jobs.tail(self.commands.log_file(task["id"]),
                                                                      command_jobs.LOGIN_CHARS))

    def _fall_over(self, task: dict, how: dict, job: dict | None, trouble: str) -> bool:
        """Run a command its Claude login stopped (`trouble`) again on the next login by priority, as tasks move on;
        False when every login has been tried, or its scheduled job has ended."""
        tried = [*how.get("tried", []), how["login"]]
        if (now_on := claude_logins.active()) in tried or (job and job["status"] not in schedules.LIVE):
            return False
        try:
            rerun = self._again(task, how, attempt=how.get("attempt", 1), after=how.get("after"), tried=tried)
        except ValueError as e:
            log.warning("command task %s couldn't run again on another Claude login: %s", task["id"], e)
            return False
        why, long = command_jobs.ending(how), took(how["ended"] - how["started"])
        moved = f"Running it again on the {now_on or 'default'} one, as task {rerun['id']}."
        self.store.set(task["id"], command=json.dumps(how))
        self.store.log(task["id"], "rerun", f"{trouble}: running again as task {rerun['id']}")
        if job and job["last_task"] == task["id"]:  # the rerun is now the job's latest
            schedules.update(job["id"], last_task=rerun["id"], run=json.dumps(self._run_of(rerun, spec(rerun))),
                             last_result=(sentence(f"Failed after {long}: {trouble}") + f" {moved}")[:300])
        # No push now: the rerun's announcement says how it went
        self._finish(task["id"], "failed", f"Task {task['id']}, {task['title']}, failed ({why}), after {long}: "
                                           f"{trouble}. {moved}", alert=False, error=why)
        return True

    def _start_fix(self, task: dict, how: dict, output: str, job: dict | None, said_yes: str = "") -> dict:
        """Start a code_change fix task in the command's project. ValueError if it can't start."""
        prompt = command_jobs.fix_prompt(task, how, output, self.commands.log_file(task["id"]),
                                         job["title"] if job else "", said_yes)
        fix = self.start("code_change", f"Fix {task['title']}", prompt, task["project"], job=task["job"], fixes=task["id"])
        self.store.log(task["id"], "fix", f"task {fix['id']} is fixing it")
        return fix

    def _reply_command(self, task: dict, message: str) -> dict:
        """A reply to a command task waiting on ASK is the user's yes to fix it (stopping it means no).

        Any other command task rejects messages with ValueError."""
        task_id = task["id"]
        if task["status"] != "waiting":
            raise ValueError(f"Task {task_id} runs a shell command, which takes no messages. "
                             + ("Stop it with task_stop if need be." if task["status"] in ACTIVE else
                                "To run it again, start it again."))
        how = spec(task)
        said = " ".join(message.split())[:SAID_MOST]
        self.store.log(task_id, "reply", said)
        job = schedules.get(task["job"]) if task["job"] is not None else None
        output = self.commands.output(task_id, command_jobs.FIX_CHARS, how.get("log_file"), how.get("started", 0))
        fix = self._start_fix(task, how, output, job, said)
        how["fix_task"] = fix["id"]
        self._settle_run(task, how, f"Task {fix['id']} is fixing it.", last_task=fix["id"])
        self._finish(task_id, "failed", f"Task {task_id}, {task['title']}: task {fix['id']} is fixing it, and runs it "
                                        "again after.", seen=1, question=None, wait_kind=None,
                     command=json.dumps(how))
        return self.store.get(task_id)

    def _fixed(self, task: dict, summary: str, again: bool) -> tuple[str, bool]:
        """A fix task finished: returns (what to say, quiet). Quiet means a rerun was started and will announce instead.

        No rerun if `again` is false, the fix is on its own worktree branch, or the scheduled job ended or moved on."""
        said = f"Task {task['id']}, {task['title']}, is done. {scheduler.headline(summary)}"
        if not (failed := self.store.get(task["fixes"])) or failed["kind"] != COMMAND:
            return said, False
        if not again:
            return f"{said} It doesn't run the command again: it would fail the same way.", False
        if task["worktree"] and not task["in_place"]:
            return f"{said} The fix is on its own branch: once that's merged, the command can run again.", False
        job = schedules.get(failed["job"]) if failed["job"] is not None else None
        if job and (job["status"] not in schedules.LIVE or job["last_task"] not in (None, task["id"])):
            return said, False
        how = spec(failed)
        after = {"task": failed["id"], "fix": task["id"], "failed": how.get("said") or command_jobs.ending(how),
                 "fixed": scheduler.headline(summary)}
        try:
            rerun = self._again(failed, how, attempt=how.get("attempt", 1) + 1, after=after)
        except ValueError as e:
            return f"{said} The command couldn't run again: {e}", False
        self.store.log(failed["id"], "rerun", f"running again as task {rerun['id']}, after task {task['id']}'s fix")
        if job:
            schedules.update(job["id"], last_task=rerun["id"], run=json.dumps(self._run_of(rerun, spec(rerun))),
                             last_result="Running again after a fix.")
        return f"{said} Running the command again, as task {rerun['id']}.", True

    # ── Connection tasks (jarvis.tasks.connection_jobs) ─────────

    def start_connection(self, title: str, instruction: str, pid: int) -> dict:
        """Start a slotless connection task that relay process `pid` reports to."""
        task = self.store.create(CONNECTION, " ".join(title.split())[:80] or "a connection's call", instruction, None,
                                 command={"pid": int(pid)})
        self.store.log(task["id"], "start", instruction[:300])
        return self.store.update(task["id"], status="running", activity="running")

    def _connection(self, task_id: int) -> dict:
        task = self._get(task_id)
        if task["kind"] != CONNECTION:
            raise ValueError(f"Task {task_id} isn't a call on a connection.")
        return task

    def connection_update(self, task_id: int, activity: str) -> dict:
        """Show the call's latest progress as the task's activity."""
        if self._connection(task_id)["status"] == "running" and activity.strip():
            self.store.set(task_id, activity=" ".join(activity.split())[:200])
        return self.store.get(task_id)

    async def end_connection(self, task_id: int, outcome: str, result: str) -> dict:
        """End a connection task ("done"/"failed"): announce a model summary and keep the full result as its summary.

        A task stopped meanwhile was already announced and is returned as is."""
        task = self._connection(task_id)
        if task["status"] != "running":
            return task
        failed, seconds = outcome != "done", time.time() - task["created"]
        summary = ""
        if not usage.hold_until():
            try:
                summary = await connection_jobs.summarize(task["title"], task["instruction"], failed, seconds, result)
            except Exception as e:
                log.warning("connection task %s: no summary (%s)", task_id, type(e).__name__)
        if (task := self.store.get(task_id))["status"] != "running":  # stopped during summarising
            return task
        said = f"failed after {took(seconds)}." if failed else f"is done, after {took(seconds)}."
        summary = summary or ("" if failed else "Ask me what it returned.")
        spoken = f"Task {task_id}, {task['title']}, {said}" + (f" {summary}" if summary else "")
        self._finish(task_id, "failed" if failed else "done", spoken, summary=result or None)
        return self.store.get(task_id)

    def check_connections(self) -> None:
        """Fail connection tasks whose relay process has died."""
        for task in self.store.active():
            if task["kind"] == CONNECTION and task["status"] == "running" and not connection_jobs.alive(task):
                self._finish(task["id"], "failed", f"Task {task['id']}, {task['title']}, stopped before it finished: "
                                                   "JARVIS lost the call. Make it again if it's still wanted.")
