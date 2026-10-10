"""A task's subagents (jarvis.tasks.worker): side jobs a session hands to helpers on cheaper models, through the Agent
tool. A read-only code searcher for code tasks, a test runner for code_change, and a web researcher for research. Their
tool calls meet the same gate and sandbox, and the gate refuses any edit from them. `Subagents` follows a run's: their
starts and ends go in the task's log, and what they're doing shows as its activity.
"""

from dataclasses import replace

from claude_agent_sdk import (
    TERMINAL_TASK_STATUSES,
    AgentDefinition,
    TaskStartedMessage,
    TaskUpdatedMessage,
    ToolResultBlock,
    ToolUseBlock,
)

from jarvis.core import profile
from jarvis.core.config import TASK_HELPER_MODEL, TASK_SEARCH_MODEL
from jarvis.tasks.reporting import Reporter

_AGENT_TOOLS = {"Agent", "Task"}  # the subagent tool, which the bundled CLI still calls by its old name, Task
# {name} is filled in as a task starts (_agents)
_HELPER_RULES = """You're a subagent of a JARVIS background task, working for the agent that briefed you; {name} doesn't see what you write.
- Stick to what you were asked. Don't edit, create or delete the project's files: only the task's own agent changes things.
- Reply with a short, factual report: what you found, with file_path:line references where they help, and anything that surprised you."""

SEARCHER = AgentDefinition(
    description="Read-only code searcher. Give it a question about the code (where something is defined or used, how a feature works, "
                "what depends on what) and it finds the relevant files and summarises them with paths and line numbers. "
                "Use it to explore without filling your own context.",
    prompt=f"""You find and summarise code. Search with Glob and Grep, read enough to be sure, and answer the question you were given.
{_HELPER_RULES}
- Quote only the lines that matter, and say when something you were asked about doesn't exist.""",
    tools=["Read", "Glob", "Grep"],
    model=TASK_SEARCH_MODEL,
    maxTurns=30,
)

TESTER = AgentDefinition(
    description="Test runner. Runs the project's tests (all of them, or the ones you name), then reports what passed and "
                "what failed, with each failure's error and where it happened. It doesn't fix anything.",
    prompt=f"""You run tests and report the results. Find how the project runs its tests (README, CLAUDE.md, Makefile, package.json,
pyproject.toml), run what you were asked to, and read failing tests and tracebacks closely enough to say why they fail.
{_HELPER_RULES}
- Shell commands run in a sandbox that can reach the network. You may install dependencies the tests need, but never
  change source or test files to make them pass.
- Report the exact command you ran, the pass and fail counts, and for each failure the test, the error and the likely cause.""",
    tools=["Read", "Glob", "Grep", "Bash"],
    model=TASK_HELPER_MODEL,
    maxTurns=40,
)

RESEARCHER = AgentDefinition(
    description="Web researcher. Give it one focused question and it searches the web, reads the best sources and "
                "reports the answer with the sources' names and URLs. Brief several at once for separate questions.",
    prompt=f"""You research one question on the web. Search, read the most reliable sources, cross-check what matters,
and prefer recent, primary sources.
{_HELPER_RULES}
- Say how sure you are, where sources disagree, and give the URLs you relied on.""",
    tools=["WebSearch", "WebFetch"],
    model=TASK_HELPER_MODEL,
    maxTurns=30,
)


def _agents(kind: str) -> dict[str, AgentDefinition]:
    """The subagents a task of this kind may start, by name, told whom they work for."""
    if kind == "code_change":
        found = {"code-searcher": SEARCHER, "test-runner": TESTER}
    elif kind == "code_question":
        found = {"code-searcher": SEARCHER}  # its Bash is read-only git, so it has no tests to run
    else:
        found = {"web-researcher": RESEARCHER}
    return {k: replace(a, prompt=a.prompt.replace("{name}", profile.name())) for k, a in found.items()}


_LAUNCHED = "Async agent launched"  # how the Agent tool's result starts when it sent the subagent to the background


def result_text(content) -> str:
    """A tool result's content as plain text: a list of blocks has its text parts joined."""
    if isinstance(content, list):
        return " ".join(str(c.get("text", "")) for c in content if isinstance(c, dict))
    return str(content or "")


def _launched(block: ToolResultBlock) -> bool:
    return result_text(block.content).lstrip().startswith(_LAUNCHED)


class Subagents:
    """Follows a run's subagents: logs each one's start and end, and labels what it's doing. A subagent in the
    background (asked for, or the CLI's default) is still running after its tool result, until a task notification
    or a terminal task update says it's done: `waiting` lists those."""

    def __init__(self, task_id: int, reporter: Reporter) -> None:
        self.task_id, self.reporter = task_id, reporter
        self.running: dict[str, str] = {}  # Agent tool_use id -> "code searcher"
        self.background: set[str] = set()  # ids of those whose tool result only says they launched
        self.tasks: dict[str, str] = {}  # the CLI's task id -> the Agent tool_use id, for task updates

    @property
    def waiting(self) -> list[str]:
        """Who's still running in the background."""
        return [self.running[i] for i in self.background if i in self.running]

    def task_started(self, msg: TaskStartedMessage) -> None:
        if msg.tool_use_id in self.running:
            self.tasks[msg.task_id] = msg.tool_use_id

    def task_updated(self, msg: TaskUpdatedMessage) -> None:
        status = msg.status or msg.patch.get("status")
        if status in TERMINAL_TASK_STATUSES and (tool_use_id := self.tasks.get(msg.task_id)):
            self.ended(tool_use_id, status != "completed")

    def started(self, block: ToolUseBlock) -> None:
        args = block.input
        who = (args.get("subagent_type") or "general-purpose").replace("-", " ")
        self.running[block.id] = who
        if args.get("run_in_background"):
            self.background.add(block.id)
        how = " in the background" if block.id in self.background else ""
        self.reporter.subagent(self.task_id, f"{who} started{how}: {args.get('description') or 'no description'}")

    def label(self, parent_id: str | None, what: str) -> str:
        """`what`, marked as a subagent's if one is doing it."""
        who = self.running.get(parent_id or "")
        return f"{who}: {what}" if who else what

    def result(self, block: ToolResultBlock) -> None:
        if block.tool_use_id in self.running and not block.is_error and (block.tool_use_id in self.background or _launched(block)):
            self.background.add(block.tool_use_id)
            return  # it launched; a task notification says when it's done
        self.ended(block.tool_use_id, bool(block.is_error))

    def ended(self, tool_use_id: str | None, failed: bool) -> None:
        if (who := self.running.pop(tool_use_id or "", None)) is None:
            return
        self.background.discard(tool_use_id)
        self.reporter.subagent(self.task_id, f"{who} {'failed' if failed else 'finished'}")

