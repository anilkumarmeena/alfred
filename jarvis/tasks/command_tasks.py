"""The task runner's side (jarvis.tasks.runner) of the tasks that run apart from it, with no task slot: command tasks
(kind "command", jarvis.tasks.command_jobs), shell commands run with no model, and connection tasks (kind "connection",
jarvis.tasks.connection_jobs), calls on a connection carried on by the relay making them. The runner's docstring tells
how each runs and ends.
"""

import asyncio
import json
import logging
import time
from pathlib import Path

from jarvis.brain import usage
from jarvis.core.database import ACTIVE, COMMAND, CONNECTION, WAIT_QUESTION
from jarvis.safety.commands import secret_path
from jarvis.state import activity as bell
from jarvis.tasks import command_jobs, connection_jobs, scheduler, schedules
from jarvis.tasks.approvals import SAID_MOST
from jarvis.tasks.command_jobs import ASK, DONE, FIX, LOST, REPORT, STOPPED, TIMEOUT, last_lines, spec, took
from jarvis.tasks.runner_base import RunnerBase
from jarvis.tasks.scheduler import sentence

log = logging.getLogger("jarvis.tasks.runner")
COMMAND_POLL = 2.0  # seconds between looks at the command tasks running (jarvis.tasks.command_jobs)


class CommandTasks(RunnerBase):
    """Command tasks and connection tasks: started, looked after while they run, and said as they end."""

    def start_command(self, command: str, title: str, folder: str | Path, job: int | None = None,
                      project: str | None = None, timeout_minutes: int | None = None, log_file: str | None = None,
                      summarize: bool = True, auto_fix: bool = True, report: bool = False, attempt: int = 1,
                      after: dict | None = None) -> dict:
        """A new command task: `command` run in `folder` (a full path, ~ allowed) with no model while it works, taking
        no task slot. A scheduled command's runs name their `job`. A run past `timeout_minutes` is stopped; `log_file`
        is a log the command writes itself, read beside its output. As it ends, a success is summed up (`summarize`) or,
        for a job that `report`s, said with its last line; a failure is looked into, and fixed in its project (`project`,
        else the one its folder is in) if `auto_fix` lets it. A rerun after a fix is `attempt` 2, `after` saying what
        failed and what was fixed. Raises ValueError for a command or folder that can't run."""
        self._open()
        command = command.strip()
        if not command:
            raise ValueError("The command is empty.")
        path = Path(folder).expanduser()
        if not path.is_absolute():
            raise ValueError("Give the folder as a full path (~ allowed).")
        if secret_path(path):
            raise ValueError("That folder holds secrets, which stay out of reach.")
        if not path.is_dir():
            raise ValueError(f"{path} isn't a folder.")
        if project is None and auto_fix:
            project = command_jobs.project_of(path)
        title = " ".join(title.split())[:80] or command[:60]
        how = {"folder": str(path), "timeout_minutes": timeout_minutes or None, "log_file": log_file or None,
               "summarize": bool(summarize), "auto_fix": bool(auto_fix), "report": bool(report), "attempt": attempt}
        if after:
            how["after"] = after
        task = self.store.create(COMMAND, title, command, project, job=job, command=how)
        task_id = task["id"]
        try:
            how["started"] = self.commands.start(task_id, command, title, path)["started"]
        except (ValueError, OSError) as e:
            self.store.log(task_id, "failed", f"couldn't start: {e}")
            self.store.set(task_id, status="failed", error=f"Couldn't start: {e}"[:300], spoken=f"Couldn't start: {e}"[:300])
            raise ValueError(f"{e}") from e
        self.store.log(task_id, "start", f"in {path}: {command}")
        if attempt > 1:
            self.store.log(task_id, "rerun", f"running again after task {after['fix']}'s fix" if after else "running again")
        bell.add(f"Started {'scheduled command' if job is not None else 'command'} {task_id}: {title}", "run", task_id)
        return self.store.update(task_id, status="running", activity="running", command=json.dumps(how))

    def rerun(self, task_id: int) -> dict:
        """Run a finished command task's command again, as it ran before, as a command task of its own (the control
        panel's Retry, which the user taps)."""
        task = self._get(task_id)
        if task["kind"] != COMMAND:
            raise ValueError(f"Task {task_id} isn't a command; reply to it to carry on.")
        if task["status"] in ACTIVE:
            raise ValueError(f"Task {task_id} is {task['status']}; it can run again once it has ended.")
        how = spec(task)
        again = self.start_command(task["instruction"], task["title"], how["folder"], task["job"], task["project"],
                                   how.get("timeout_minutes"), how.get("log_file"), how.get("summarize", True),
                                   how.get("auto_fix", True), how.get("report", False))
        self.store.log(task_id, "rerun", f"run again as task {again['id']}")
        if task["job"] is not None and (job := schedules.get(task["job"])) and job["last_task"] == task_id:
            schedules.update(job["id"], last_task=again["id"], run=json.dumps(self._run_of(again, spec(again))))
        return again

    async def stop_command(self, task_id: int) -> dict:
        """Stop a command task's running command: all of it, cleanly first, then forcibly. Raises ValueError if it isn't
        running."""
        task = self._get(task_id)
        if task["kind"] != COMMAND or task["status"] != "running":
            raise ValueError(f"Task {task_id} isn't running a command.")
        if task_id in self.ending:
            raise ValueError(f"Task {task_id}'s command has just ended.")
        self.ending[task_id] = asyncio.current_task()  # so check_commands leaves it be
        try:
            try:
                await self.commands.stop(task_id)
            except ValueError as e:  # it ended just now: check_commands says how
                raise ValueError(f"Task {task_id}'s command had already ended ({e}).") from None
            await self._command_ended(task_id, STOPPED, None, time.time())
        finally:
            self.ending.pop(task_id, None)
        return self.store.get(task_id)

    async def _watch_commands(self) -> None:
        while True:
            try:
                self.check_commands()
                self.check_connections()
            except Exception:
                log.exception("looking after the command tasks failed")
            await asyncio.sleep(COMMAND_POLL)

    def check_commands(self, now: float | None = None) -> None:
        """Look at each command task running: one past its time limit is stopped, and one that has ended is said, in
        the background (summing it up or looking into a failure takes a model call). The one running shows its
        latest line of output as its activity."""
        now = now or time.time()
        for task in self.store.active():
            if task["kind"] != COMMAND or task["status"] != "running" or task["id"] in self.ending:
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
        """Until the command tasks' ends being said are all said (for tests)."""
        while self.ending:
            await asyncio.gather(*self.ending.values(), return_exceptions=True)

    async def _time_up(self, task_id: int) -> None:
        try:
            await self.commands.stop(task_id)
        except ValueError:  # it ended just now, on its own
            record = self.commands.get(task_id)
            if record and (ended := self.commands.how(record)) and ended[0] != STOPPED:
                return await self._command_ended(task_id, *ended, time.time())
        await self._command_ended(task_id, TIMEOUT, None, time.time())

    @staticmethod
    def _job_title(task: dict) -> str:
        """The title of the scheduled job a task is a run of (its pushes go under it), or ""."""
        return (schedules.get(task["job"]) or {}).get("title", "") if task.get("job") is not None else ""

    @staticmethod
    def _run_of(task: dict, how: dict) -> dict:
        """A command task as its scheduled job keeps its latest run (schedules.run_of)."""
        return {"task": task["id"], "started": how.get("started", task["created"]), "attempt": how.get("attempt", 1)} | {
            k: how[k] for k in ("ended", "outcome", "code", "summary", "fix_task") if how.get(k) is not None}

    def _settle_run(self, task: dict, how: dict, last_result: str = "", **fields) -> None:
        """A scheduled command's run has ended: its job keeps how it went, if it's still the job's latest."""
        if task["job"] is None or not (job := schedules.get(task["job"])) or job["last_task"] != task["id"]:
            return
        schedules.update(job["id"], run=json.dumps(self._run_of(task, how)),
                         **({"last_result": last_result[:300]} if last_result else {}), **fields)

    async def _command_ended(self, task_id: int, outcome: str, code: int | None, at: float) -> None:
        """A command task's command has ended: said, summed up, or looked into and fixed."""
        task = self.store.get(task_id)
        if not task or task["status"] != "running":
            return
        how = spec(task) | {"outcome": outcome, "code": code, "ended": at}
        how.setdefault("started", task["created"])
        self.store.set(task_id, command=json.dumps(how))  # first, so it's said once
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
        except Exception as e:  # a model call or the database: the task mustn't stay running
            log.exception("command task %s: its end couldn't be said", task_id)
            if self.store.get(task_id)["status"] == "running":
                self._finish(task_id, "done" if outcome == DONE else "stopped" if outcome == STOPPED else "failed",
                             f"Task {task_id}, {task['title']}, ended ({outcome}), but saying how it went failed: "
                             f"{type(e).__name__}.")

    def _after(self, how: dict) -> str:
        """For a rerun after a fix: what failed before, and what was fixed."""
        after = how.get("after") or {}
        if not after:
            return ""
        return (f" It had failed before (task {after['task']}): {sentence(after.get('failed', ''))} Task {after['fix']} "
                f"fixed it: {sentence(after.get('fixed', ''))}")

    async def _command_done(self, task: dict, how: dict) -> None:
        """Summed up in a sentence or two from a short model call, unless it says not to, or Claude's usage limit is
        out: then with its last lines. A job that reports says its last line, which says what it made."""
        task_id, seconds = task["id"], how["ended"] - how["started"]
        output = self.commands.output(task_id, command_jobs.SUMMARY_CHARS, how.get("log_file"), how["started"])
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
        """A model reads the end of its output and says why it failed; then, as it decides, a task fixes it at once, the
        user is asked first, or it's only said. A run that's had its fix, or may not be fixed, is only said."""
        task_id, seconds = task["id"], how["ended"] - how["started"]
        why, long = command_jobs.ending(how), took(seconds)
        output = self.commands.output(task_id, command_jobs.FIX_CHARS, how.get("log_file"), how["started"])
        job = schedules.get(task["job"]) if task["job"] is not None else None
        can_fix = bool(how.get("auto_fix") and task["project"] and how.get("attempt", 1) <= command_jobs.FIX_ATTEMPTS
                       and (job is None or job["status"] in schedules.LIVE))
        found = None
        if not usage.hold_until():
            found = await command_jobs.triage(task["title"], task["instruction"], how["folder"], why, seconds,
                                              output[-command_jobs.TRIAGE_CHARS:], can_fix)
        if found is None:  # no model to read it: a fix if it may have one, as before there was a choice
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
            except ValueError as e:  # its project has gone, say
                found, said = command_jobs.Triage(REPORT, found.said), f"{said} No task could fix it: {e}"
            else:
                how["fix_task"] = fix["id"]
                self.store.set(task_id, command=json.dumps(how))
                self._settle_run(task, how, sentence(f"Failed after {long}: {why}") + f" Task {fix['id']} is fixing it.",
                                 last_task=fix["id"])
                # Said in a live session; the push waits for the rerun, which says what failed, the fix and how it went
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

    def _start_fix(self, task: dict, how: dict, output: str, job: dict | None, said_yes: str = "") -> dict:
        """A code_change task in the command's project, fixing it. ValueError if it can't start."""
        prompt = command_jobs.fix_prompt(task, how, output, self.commands.log_file(task["id"]),
                                         job["title"] if job else "", said_yes)
        fix = self.start("code_change", f"Fix {task['title']}", prompt, task["project"], job=task["job"], fixes=task["id"])
        self.store.log(task["id"], "fix", f"task {fix['id']} is fixing it")
        return fix

    def _reply_command(self, task: dict, message: str) -> dict:
        """A reply to a command task: to one waiting for the user's yes to fix it, the yes (to say no, stop it); any
        other takes no messages."""
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
        """A task fixing a command task has finished: what to say, and whether quietly, since the command runs again
        (once, when the fix says it should now succeed: command_jobs.wants_rerun) and that run says it all. Not when
        its fix is on a branch of its own (a worktree task), or a scheduled command's job has ended or is running again
        meanwhile."""
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
            rerun = self.start_command(failed["instruction"], failed["title"], how["folder"], failed["job"],
                                       failed["project"], how.get("timeout_minutes"), how.get("log_file"),
                                       how.get("summarize", True), how.get("auto_fix", True), how.get("report", False),
                                       how.get("attempt", 1) + 1, after)
        except ValueError as e:
            return f"{said} The command couldn't run again: {e}", False
        self.store.log(failed["id"], "rerun", f"running again as task {rerun['id']}, after task {task['id']}'s fix")
        if job:
            schedules.update(job["id"], last_task=rerun["id"], run=json.dumps(self._run_of(rerun, spec(rerun))),
                             last_result="Running again after a fix.")
        return f"{said} Running the command again, as task {rerun['id']}.", True

    # ── Connection tasks (jarvis.tasks.connection_jobs) ─────────

    def start_connection(self, title: str, instruction: str, pid: int) -> dict:
        """A call on a connection still running after the voice brain's wait, as a task of its own that the relay
        making it (process `pid`) reports to, with no task slot."""
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
        """What its call last said of how it's going, shown as its activity."""
        if self._connection(task_id)["status"] == "running" and activity.strip():
            self.store.set(task_id, activity=" ".join(activity.split())[:200])
        return self.store.get(task_id)

    async def end_connection(self, task_id: int, outcome: str, result: str) -> dict:
        """Its call has ended ("done" or "failed"): said, summed up by a short model call unless Claude's usage limit
        is out, and what it returned kept as its summary. A task stopped meanwhile was said as it stopped."""
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
        if (task := self.store.get(task_id))["status"] != "running":  # stopped while it was summed up
            return task
        said = f"failed after {took(seconds)}." if failed else f"is done, after {took(seconds)}."
        summary = summary or ("" if failed else "Ask me what it returned.")
        spoken = f"Task {task_id}, {task['title']}, {said}" + (f" {summary}" if summary else "")
        self._finish(task_id, "failed" if failed else "done", spoken, summary=result or None)
        return self.store.get(task_id)

    def check_connections(self) -> None:
        """A connection task whose relay has gone (a crash, or the Mac restarting) ends: its call can't."""
        for task in self.store.active():
            if task["kind"] == CONNECTION and task["status"] == "running" and not connection_jobs.alive(task):
                self._finish(task["id"], "failed", f"Task {task['id']}, {task['title']}, stopped before it finished: "
                                                   "JARVIS lost the call. Make it again if it's still wanted.")
