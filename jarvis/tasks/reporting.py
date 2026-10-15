"""Channel between a task run (worker) and the runner.

  Reporter   protocol the runner implements for run callbacks. On another machine (jarvis.node.work) the answers
             come over the link, so the ones that return something may return an awaitable: `answer` takes either.
  task tools report_progress, ask_user, propose_data_edit, request_access (MCP server "task")
  nudges     user messages to a running task, injected after its next tool call by a PostToolUse hook
"""

from collections.abc import Awaitable
from typing import Protocol

from claude_agent_sdk import HookMatcher, create_sdk_mcp_server, tool as sdk_tool

from jarvis.core import profile
from jarvis.state.memory import FACT_MAX, LIMITS, SECTIONS
from jarvis.tasks import access, data_edits
from jarvis.tasks.host_tools import TaskJobs, answer


class Reporter(Protocol):
    """Runner callbacks a run uses, plus the host job registry its host tools start jobs in."""

    host_jobs: TaskJobs

    def session(self, task_id: int, session_id: str) -> None: ...
    def alive(self, task_id: int, msg: object) -> None: ...  # every session message, for stall detection
    def activity(self, task_id: int, text: str) -> None: ...
    def progress(self, task_id: int, text: str) -> None: ...
    def subagent(self, task_id: int, text: str) -> None: ...
    def ask(self, task_id: int, question: str) -> None: ...
    def take_nudges(self, task_id: int) -> list[str] | Awaitable[list[str]]: ...
    def propose(self, task_id: int, args: dict) -> dict | Awaitable[dict]: ...
    def cleared(self, task_id: int, item: dict) -> bool | Awaitable[bool]: ...
    def commit_ordered(self, task_id: int) -> bool | Awaitable[bool]: ...
    def request_approval(self, task_id: int, item: dict) -> dict | Awaitable[dict]: ...
    def request_access(self, task_id: int, names: list[str], reason: str) -> list[str] | Awaitable[list[str]]: ...
    def flag(self, task_id: int, text: str) -> None: ...


def _task_server(task_id: int, reporter: Reporter):
    @sdk_tool(
        "report_progress",
        f"Tell {profile.name()} about a real milestone, in one plain sentence.",
        {"type": "object", "properties": {"message": {"type": "string"}}, "required": ["message"]},
    )
    async def report_progress(args: dict) -> dict:
        reporter.progress(task_id, args["message"])
        return {"content": [{"type": "text", "text": "Noted."}]}

    @sdk_tool(
        "ask_user",
        f"Ask {profile.name()} a question you can't sensibly decide yourself. The task pauses until they answer.",
        {"type": "object", "properties": {"question": {"type": "string", "description": "One short spoken question."}}, "required": ["question"]},
    )
    async def ask_user(args: dict) -> dict:
        reporter.ask(task_id, args["question"])
        return {"content": [{"type": "text", "text": "Sent. End your turn now with one short line; the answer will arrive as the next message."}]}

    @sdk_tool(
        "propose_data_edit",
        "Suggest one change to JARVIS's own data files, which you can't edit yourself; JARVIS applies it, asking "
        f"{profile.name()} first when it's a bigger one. Actions: project_register (name, and optionally path, aliases, about, "
        "in_place) adds a project to the project list; project_update (name, and the path, aliases, about or in_place "
        "to change) changes a listed project's settings; todo_add (item) and todo_done (text) add or tick off a to-do, "
        "and todo_reword (text, item) rewords one; "
        "remember (fact, section) and forget (text) save or delete long-term memory lines; edit (file, old, new) "
        "replaces one exact piece of text in memory.md, for anything else there. memory.md's sections have size "
        f"limits ({', '.join(f'{s} {n:,}' for s, n in LIMITS.items())} characters, any one line "
        f"{FACT_MAX}, date included): prefer rewriting a fact with edit to adding a near-duplicate.",
        {"type": "object", "properties": {
            "action": {"type": "string", "enum": list(data_edits.ACTIONS)},
            "reason": {"type": "string", "description": "One line: why, in terms of the work you did."},
            "name": {"type": "string", "description": "project_register: the project's spoken name; project_update: the listed project's name."},
            "path": {"type": "string", "description": "project_register, project_update: its absolute or ~ folder; leave out for the default, or to keep it."},
            "aliases": {"type": "array", "items": {"type": "string"}, "description": "project_register, project_update: names speech recognition might hear (for project_update, the whole new list)."},
            "about": {"type": "string", "description": "project_register, project_update: what it is, and whether it's work or personal."},
            "in_place": {"type": "boolean", "description": "project_register, project_update: false for a branch per task (default true)."},
            "item": {"type": "string", "description": "todo_add, todo_reword: the item, as one self-contained sentence."},
            "fact": {"type": "string", "description": "remember: one self-contained sentence."},
            "section": {"type": "string", "enum": list(SECTIONS),
                        "description": f"remember: user for {profile.name()} themself (preferences, habits, goals, how they "
                                       "like to be spoken to); environment for their machine, setup, tools, projects "
                                       "and accounts."},
            "text": {"type": "string", "description": "todo_done, todo_reword, forget: text found only in the line(s) meant."},
            "file": {"type": "string", "enum": list(data_edits.FILES), "description": "edit: which data file."},
            "old": {"type": "string", "description": "edit: text that appears exactly once in the file now, quoted exactly."},
            "new": {"type": "string", "description": "edit: what replaces it (empty to delete it)."},
        }, "required": ["action", "reason"]},
    )
    async def propose_data_edit(args: dict) -> dict:
        try:
            edit = await answer(reporter.propose(task_id, args))
        except ValueError as e:
            return {"content": [{"type": "text", "text": f"Not suggested: {e}"}], "is_error": True}
        return {"content": [{"type": "text", "text": f"Suggested as edit {edit['n']}: {data_edits.describe(edit)}. "
                             "JARVIS applies it after you finish; mention it in your summary, not as a hand edit."}]}

    @sdk_tool(
        "request_access",
        f"Ask for access this task doesn't have, when what {profile.name()} asked for needs it: "
        f"{access.listing(access.GRANTS)}. The task pauses until it's answered; once granted, you carry on in this "
        "same session with the added tools.",
        {"type": "object", "properties": {
            "access": {"type": "array", "items": {"type": "string", "enum": list(access.GRANTS)}},
            "reason": {"type": "string", "description": f"One plain sentence, said to {profile.name()}: what you need it "
                                                        "for, in terms of what you were asked."},
        }, "required": ["access", "reason"]},
    )
    async def request_access(args: dict) -> dict:
        try:
            asked = await answer(reporter.request_access(task_id, args.get("access") or [], str(args.get("reason") or "")))
        except ValueError as e:
            return {"content": [{"type": "text", "text": f"Not asked: {e}"}], "is_error": True}
        return {"content": [{"type": "text", "text": f"Asked for {', '.join(asked)}. End your turn now with one short "
                             "line; once it's granted you'll get a message and carry on here, with it."}]}

    return create_sdk_mcp_server("task", tools=[report_progress, ask_user, propose_data_edit, request_access])


REQUEST_ACCESS = "mcp__task__request_access"
PAUSES = {"mcp__task__ask_user", REQUEST_ACCESS}  # tools after which the task waits for an answer


def nudge_text(messages: list[str], finished: bool = False) -> str:
    """Prompt text delivering user messages to a running session."""
    said = "\n".join(f"- {m}" for m in messages)
    if finished:
        return f"{profile.name()} sent this as you were finishing:\n{said}\nDo what it asks, then end with a summary of the whole task as before."
    return (f"New message{'s' if len(messages) > 1 else ''} from {profile.name()}, sent while you were working, oldest first:\n{said}\n"
            "Adjust as asked and carry on with everything else you were doing. It's already noted in your progress log.")


def with_nudges(prompt: str, messages: list[str], started: bool) -> str:
    """Append messages queued before the run began to its prompt."""
    if not messages:
        return prompt
    lead = f"Also from {profile.name()}, sent while you were busy:" if started else f"Added by {profile.name()} before you started:"
    return f"{prompt}\n\n{lead}\n" + "\n".join(f"- {m}" for m in messages)


def _nudge_hook(task_id: int, reporter: Reporter) -> list[HookMatcher]:
    """PostToolUse hook delivering pending nudges after each tool call."""

    async def deliver(data: dict, _tool_use_id, _ctx) -> dict:
        if data.get("agent_id"):
            return {}  # subagent call: deliver to the main session only
        if data["tool_name"] in PAUSES:
            return {}  # pausing for an answer: messages go with the reply
        if not (messages := await answer(reporter.take_nudges(task_id))):
            return {}
        return {"hookSpecificOutput": {"hookEventName": data["hook_event_name"], "additionalContext": nudge_text(messages)}}

    return [HookMatcher(hooks=[deliver])]
