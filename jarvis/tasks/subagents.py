"""Task subagents: helpers on cheaper models, started through the Agent tool.

  code_change    code-searcher (read-only), test-runner
  code_question  code-searcher
  other kinds    web-researcher
Their tool calls pass the same gate and sandbox, which refuses any edit from them. `Subagents` logs starts and ends
and labels their activity.
"""

from dataclasses import replace

from jarvis.llm import roles
from jarvis.llm.roles import Role
from jarvis.llm.sdk import (
    TERMINAL_TASK_STATUSES,
    AgentDefinition,
    TaskStartedMessage,
    TaskUpdatedMessage,
    ToolResultBlock,
    ToolUseBlock,
)
from jarvis.storage import profile
from jarvis.tasks.reporting import Reporter

_AGENT_TOOLS = {"Agent", "Task"}  # the bundled CLI may still call it Task
# {name} is filled in by _agents
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
    model=roles.model(Role.TASK_SEARCH),
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
    model=roles.model(Role.TASK_HELPER),
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
    model=roles.model(Role.TASK_HELPER),
    maxTurns=30,
)


def _agents(kind: str) -> dict[str, AgentDefinition]:
    """Subagents available to a task of `kind`, by name, with the user's name filled in."""
    if kind == "code_change":
        found = {"code-searcher": SEARCHER, "test-runner": TESTER}
    elif kind == "code_question":
        found = {"code-searcher": SEARCHER}  # its Bash is read-only git: no tests
    else:
        found = {"web-researcher": RESEARCHER}
    return {k: replace(a, prompt=a.prompt.replace("{name}", profile.name())) for k, a in found.items()}


_LAUNCHED = "Async agent launched"  # Agent tool result prefix for a backgrounded subagent


def result_text(content) -> str:
    """Tool result content as plain text (text blocks joined)."""
    if isinstance(content, list):
        return " ".join(str(c.get("text", "")) for c in content if isinstance(c, dict))
    return str(content or "")


def _launched(block: ToolResultBlock) -> bool:
    return result_text(block.content).lstrip().startswith(_LAUNCHED)


class Subagents:
    """Track a run's subagents: log starts and ends, label their activity.

    A background subagent outlives its tool result until a terminal task update arrives; `waiting` lists those.
    """

    def __init__(self, task_id: int, reporter: Reporter) -> None:
        self.task_id, self.reporter = task_id, reporter
        self.running: dict[str, str] = {}  # Agent tool_use id -> "code searcher"
        self.background: set[str] = set()  # launched in the background, not yet done
        self.tasks: dict[str, str] = {}  # CLI task id -> Agent tool_use id

    @property
    def waiting(self) -> list[str]:
        """Labels of subagents still running in the background."""
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
        """Prefix `what` with the subagent's label if `parent_id` is one."""
        who = self.running.get(parent_id or "")
        return f"{who}: {what}" if who else what

    def result(self, block: ToolResultBlock) -> None:
        if block.tool_use_id in self.running and not block.is_error and (block.tool_use_id in self.background or _launched(block)):
            self.background.add(block.tool_use_id)
            return  # launched; a task update will report the end
        self.ended(block.tool_use_id, bool(block.is_error))

    def ended(self, tool_use_id: str | None, failed: bool) -> None:
        if (who := self.running.pop(tool_use_id or "", None)) is None:
            return
        self.background.discard(tool_use_id)
        self.reporter.subagent(self.task_id, f"{who} {'failed' if failed else 'finished'}")

