"""Voice brain on the local `claude` CLI via the Claude Agent SDK.

The user's own Claude login (or JARVIS's, see CLAUDE_OWN_CONFIG_DIR); one CLI process per voice session. Claude Code's own abilities (file edits,
settings, its skills, other MCP servers) are off. Available: JARVIS tools, web search/fetch, sandboxed shell and jobs
(jarvis.safety.shell), the browser when on, enabled MCP servers (writes gated by jarvis.safety.mcp_gate), side jobs
for slow reading and file lookups (jarvis.brain.side), and enabled skills as a per-session plugin. The brain has no
file reads or subagents of its own: a file lookup holds a turn at most LOOKUP_WAIT seconds in all, then carries on in
the background.

Photos sent from the page ride along with the next turn as image blocks. Talking over JARVIS stops the reply,
except while a shell command runs: then the reply finishes unheard and the next turn waits for it.

A turn does only a little work itself (jarvis.brain.budget): past a few calls or seconds, further calls are refused
so the brain hands the rest to a lookup or a task. Slow JARVIS tool calls carry on outside the turn
(jarvis.brain.kept); their results come back as follow-up turns the brain starts itself, spoken through the floor. The user's own turn always comes first: it stops a follow-up under way.
"""

import asyncio
import json
import logging
import warnings
from collections.abc import AsyncIterator
from contextlib import aclosing

from claude_agent_sdk import (
    AssistantMessage,
    CanUseToolShadowedWarning,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ClaudeSDKError,
    HookMatcher,
    RateLimitEvent,
    ResultMessage,
    StreamEvent,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from jarvis import prompts, tools
from jarvis.brain import usage
from jarvis.brain.budget import Budget
from jarvis.brain.kept import Kept
from jarvis.brain.persona import Notes, system_prompt
from jarvis.brain.sdk import BUILTIN_AGENTS_DENIED, LimitWatch, jarvis_server, sdk_options
from jarvis.core import profile
from jarvis.core.config import (
    CLI_MODEL,
    CONNECTION_WAIT,
    EFFORT,
    INJECTION_VOICE_SECONDS,
    MAX_TOOL_ROUNDS,
    browser_on,
)
from jarvis.integrations import accounts, mcp_servers
from jarvis.safety import consent, mcp_gate, remembered, shell
from jarvis.skills import home as skills
from jarvis.state import activity, conversation, memory, pictures
from jarvis.tools import browser, connections, google, screen, skills as skill_tools
from jarvis.voice import timing

log = logging.getLogger("jarvis.brain.cli")
JARVIS_TOOLS = "mcp__jarvis__"  # CLI prefix for JARVIS's own tools
# Allowed outright: web, JARVIS tools, job_status/stop_job, MCP reads. The gate sees unsandboxed Bash, start_job,
# Google sends/deletes and MCP writes.
warnings.filterwarnings("ignore", category=CanUseToolShadowedWarning)

ALLOWED_BUILTINS = ["WebSearch", "WebFetch"]
INTERRUPT_SECONDS = 15  # wait for an interrupted reply to end, then restart the CLI

LOOKUP_PROMPT = "\n\n" + prompts.load("voice_lookups")


def _result_text(response) -> str:
    """A tool result as text; MCP results are a list of content blocks or a dict holding one."""
    if isinstance(response, dict):
        response = response.get("content", response.get("text", ""))
    if isinstance(response, list):
        return "\n".join(b.get("text", "") for b in response if isinstance(b, dict))
    return response if isinstance(response, str) else ""


def _bell_hook(gate: mcp_gate.Gate) -> list[HookMatcher]:
    """PostToolUse hook: note changes (tools with `changes`, MCP writes) for the panel's bell. Held writes never
    run, so aren't noted."""

    async def noted(data: dict, _tool_use_id, _ctx) -> dict:
        name, args, response = data["tool_name"], data.get("tool_input") or {}, data.get("tool_response")
        if isinstance(response, dict) and (response.get("is_error") or response.get("isError")):
            return {}
        if said := gate.change(name, args):
            await asyncio.to_thread(activity.add, said)
        elif name.startswith(JARVIS_TOOLS) and (t := tools.get(name.removeprefix(JARVIS_TOOLS))) and t.changes:
            await asyncio.to_thread(activity.tool_ran, t.name, args, _result_text(response), t.said)
        return {}

    return [HookMatcher(hooks=[noted])]


def _options(gate: mcp_gate.Gate, jobs: shell.Jobs, kept: Kept | None = None,
             budget: Budget | None = None) -> ClaudeAgentOptions:
    server, allowed = jarvis_server(kept=kept)
    # Read per session; a slow server goes to the background after CONNECTION_WAIT seconds
    mcp = mcp_servers.session(mcp_servers.for_voice(), wait=CONNECTION_WAIT)
    gate.use(mcp.servers)
    shell.scratch_dir().mkdir(parents=True, exist_ok=True)
    # Enabled skills as a per-session plugin
    plugin = skills.voice_plugin()
    found = skills.plugin(plugin)
    return sdk_options(
        env=shell.BASH_ENV | mcp_servers.session_env(),
        cwd=str(shell.scratch_dir()),
        system_prompt=system_prompt(shell.prompt() + LOOKUP_PROMPT + screen.prompt()
                                    + (browser.prompt() if browser_on() else "")
                                    + mcp_servers.prompt(mcp.servers) + connections.prompt()
                                    + skill_tools.prompt(found)),
        model=usage.available(CLI_MODEL),  # lighter model while Opus's weekly limit is used up
        effort=EFFORT,
        tools=ALLOWED_BUILTINS + ["Bash"] + (["Skill"] if found else []),
        skills=[skills.qualified(s) for s in found],  # only these, as Skill(name)
        plugins=[{"type": "local", "path": str(plugin)}] if found else [],
        mcp_servers={"jarvis": server, "shell": shell.job_server(jobs)} | mcp.configs,
        # sandboxed Bash is allowed by the sandbox
        allowed_tools=ALLOWED_BUILTINS + allowed + [shell.JOB_STATUS, shell.STOP_JOB] + mcp.allowed,
        disallowed_tools=BUILTIN_AGENTS_DENIED + mcp.blocked,
        permission_mode="default",
        # Everything else: unsandboxed Bash and start_job (risky ones need a yes), Google sends/deletes and MCP
        # writes (always need a yes)
        can_use_tool=gate,
        hooks={"PreToolUse": (budget or Budget()).hook, "PostToolUse": _bell_hook(gate)},
        screen_seconds=INJECTION_VOICE_SECONDS,  # the reply waits on each tool-result injection check
        sandbox=shell.SANDBOX,
        settings=json.dumps(json.loads(shell.SETTINGS) | skills.FLAGS),  # no skill's hooks or !`commands` run
        max_turns=MAX_TOOL_ROUNDS,
        include_partial_messages=True,
        stderr=lambda line: log.debug("claude: %s", line),
    )


def _photos(n: int) -> str:
    return "a photo" if n == 1 else f"{n} photos"


def _listed(names: list[str]) -> str:
    """'a', 'a and b', 'a, b and c'."""
    names = list(dict.fromkeys(names))
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


class CliBrain(Notes):
    def __init__(self) -> None:
        super().__init__()
        self.client: ClaudeSDKClient | None = None
        self._lock = asyncio.Lock()
        self._turns = asyncio.Lock()  # one turn at a time: the user's, or a follow-up
        self._drain: asyncio.Task | None = None  # drains a talked-over reply
        self._holding = False  # that reply is running a command, left to finish
        self._running: dict[str, str] = {}  # tool_use id -> tool name, current reply
        self._unheard: list[str] = []  # text of a talked-over reply
        self._cut: str = ""  # request whose work was cut off, for the next turn
        self._carried: list[str] = []  # JARVIS tools that carried on when a reply was talked over, for the next turn
        self._following = self._preempted = False  # a follow-up is under way; the user's turn stopped it
        self.limits = LimitWatch("voice")
        self.shell = mcp_gate.Gate()
        self.jobs = shell.Jobs()  # command tasks; announced on end like any task update
        self.kept = Kept()  # slow JARVIS tool calls, carried on outside the turn
        self.budget = Budget()  # how much work a turn does itself before handing the rest off
        self.next_model: str | None = None  # lighter model for the next turn only (jarvis.brain.route)
        self._main = self._model = ""  # session model, current model
        self._accounts = accounts.load()  # Google accounts the CLI started with

    async def start(self) -> None:
        """Start the CLI, or restart it when the Google accounts changed since it started: its prompt and tool list
        are fixed then, so an account signed in mid-conversation is only taken by a new one."""
        async with self._lock:
            google.fresh()
            if self.client is not None and google.ACCOUNTS != self._accounts:
                log.info("Google accounts changed; restarting the CLI to take them")
                await self.close()
            if self.client is None:
                self._accounts = google.ACCOUNTS
                options = self.limits.runs_on(_options(self.shell, self.jobs, self.kept, self.budget))
                client = ClaudeSDKClient(options)
                await client.connect()
                self.client = client
                self._main = self._model = options.model

    async def _use_model(self) -> None:
        """Switch to next_model for this turn if set, else the session model; same session keeps history."""
        wanted, self.next_model = (usage.available(self.next_model) if self.next_model else self._main), None
        await self._set_model(wanted)
        if wanted != self._main:
            timing.note("model", wanted)

    async def _set_model(self, wanted: str) -> None:
        if wanted != self._model:
            await self.client.set_model(wanted)
            self._model = wanted

    async def close(self) -> None:
        if self.client is not None:
            client, self.client = self.client, None
            await client.disconnect()

    async def end(self) -> None:
        """Stop background tool calls, the CLI and browser (logins kept) and drop "for this session" approvals."""
        await self.kept.close()
        await self.close()
        await browser.browser_close()
        await consent.close()  # a yes/no-check session that was never used
        await asyncio.to_thread(remembered.end_session, self.shell.session)

    def question_open(self) -> bool:
        return self.shell.question_open()

    def on_background(self, finished) -> None:
        self.kept.finished = finished

    async def _interrupt(self) -> None:
        """Stop the in-flight reply and read up to its result so the next turn starts clean."""
        try:
            await self.client.interrupt()
            async with asyncio.timeout(INTERRUPT_SECONDS):
                async for _ in self.client.receive_response():
                    pass
        except Exception:
            log.exception("interrupt failed; restarting the CLI")
            await self.close()

    async def _finish(self) -> None:
        """Drain a talked-over reply without stopping its command; keep its text for the next turn."""
        try:
            async for msg in self.client.receive_response():
                self._track(msg)
                if isinstance(msg, StreamEvent) and msg.parent_tool_use_id is None:
                    delta = msg.event.get("delta", {}) if msg.event.get("type") == "content_block_delta" else {}
                    if delta.get("type") == "text_delta":
                        self._unheard.append(delta["text"])
        except Exception:
            log.exception("finishing the last reply failed; restarting the CLI")
            await self.close()
        finally:
            self._running.clear()

    def _track(self, msg) -> None:
        """Track in-flight tool calls."""
        if isinstance(msg, AssistantMessage) and msg.parent_tool_use_id is None:
            self._running |= {b.id: b.name for b in msg.content if isinstance(b, ToolUseBlock)}
        elif isinstance(msg, UserMessage) and isinstance(msg.content, list):
            for b in msg.content:
                if isinstance(b, ToolResultBlock):
                    self._running.pop(b.tool_use_id, None)

    async def _drained(self) -> None:
        """Wait for a talked-over reply to end. Shielded: a talked-over turn must not stop its command."""
        if self._drain is not None:
            await asyncio.shield(self._drain)
            self._drain = None
        self._running.clear()

    def _talked_over(self, request: str) -> None:
        """A reply stopped before its end. A running command finishes unheard; else the reply is interrupted. JARVIS
        tools still running carry on in the background; other work cut off is offered to the next turn."""
        self._holding = "Bash" in self._running.values()
        if self._holding:
            log.info("spoken over while a command runs; letting it finish")
        else:
            carried = self.kept.keep_all()
            self._carried += carried
            if request and [n for n in self._running.values() if n.removeprefix(JARVIS_TOOLS) not in carried]:
                self._cut = " ".join(request.split())[:300]  # stopped mid-work; next turn may background it
        self._drain = asyncio.create_task(self._finish() if self._holding else self._interrupt())

    def _chunk(self, msg, spoken: list[str], timed: bool = True) -> str:
        """Spoken text in `msg`, also added to `spoken`, or ''. Tracks tool calls and usage limits on the way."""
        self._track(msg)
        if isinstance(msg, StreamEvent) and msg.parent_tool_use_id is None:
            event = msg.event
            if event.get("type") == "content_block_delta" and event["delta"].get("type") == "text_delta":
                spoken.append(event["delta"]["text"])
                return spoken[-1]
            if event.get("type") == "message_start" and spoken and not spoken[-1].endswith((" ", "\n")):
                spoken.append(" ")  # new model turn after a tool call
                return " "
            if timed and event.get("type") == "content_block_start" and event["content_block"].get("type") == "tool_use":
                timing.tool("".join(spoken), event["content_block"].get("name", ""))
        elif isinstance(msg, RateLimitEvent):
            self.limits.saw(msg)
        return ""

    def _prompt(self, text: str, agreed: list[str], photos: int = 0) -> str:
        prompt = self.turn(text)
        if photos:
            prompt += f"\n[{profile.name()} sent {_photos(photos)} with this, attached.]"
        if unheard := "".join(self._unheard).strip():
            prompt += (f"\n[{profile.name()} spoke while your last command was running, so they didn't hear the rest of that "
                       f"reply: \"{unheard}\"]")
        self._unheard.clear()
        if self._cut:
            prompt += (f"\n[They spoke while you were still working on \"{self._cut}\", so that work stopped. If it's "
                       f"still wanted and will take more than a moment, hand it to computer_lookup (if it's about this "
                       f"computer) or background_lookup now, so you can answer them meanwhile.]")
            self._cut = ""
        if self._carried:
            prompt += (f"\n[They spoke while {_listed(self._carried)} was still running, so it carries on in the "
                       f"background: its result comes back to you in a turn of your own. Don't start it again.]")
            self._carried = []
        if agreed:
            prompt += "\n" + self.shell.agreed_hint(agreed)
        return prompt

    @staticmethod
    def _message(prompt: str, photos: list):
        """The prompt, or with photos a single message of image blocks followed by the text."""
        if not photos:
            return prompt
        content = [pictures.block(p) for p in photos] + [{"type": "text", "text": prompt}]

        async def message():
            yield {"type": "user", "message": {"role": "user", "content": content}, "parent_tool_use_id": None}

        return message()

    async def respond(self, text: str) -> AsyncIterator[str]:
        """Stream the spoken reply to one user utterance. A follow-up under way is stopped first."""
        if self._following and self.client is not None:
            self._preempted = True
            log.info("the user spoke during a follow-up; stopping it")
            try:
                await self.client.interrupt()
            except Exception:
                log.exception("stopping the follow-up failed; restarting the CLI")
                await self.close()
        async with self._turns, aclosing(self._respond(text)) as reply:
            async for chunk in reply:
                yield chunk

    async def _respond(self, text: str) -> AsyncIterator[str]:
        if self._drain is not None and self._holding and not self._drain.done():
            yield shell.HOLD + " "
        await self._drained()
        if lookups := conversation.current().lookups:
            lookups.new_turn()  # a fresh LOOKUP_WAIT for this turn
        spoken: list[str] = []
        finished = interrupted = False
        # Answer to an open question: word lists first, else a short model call
        agreed = self.shell.new_turn(text, await self.shell.judge(text))
        approved = self.shell.described(agreed)  # before the agreed draft goes away
        photos: list = []
        try:
            try:
                photos = await asyncio.to_thread(pictures.take)  # sent since the last turn
            except OSError:
                log.exception("couldn't take the photos sent")
            await self.start()
            await self._use_model()
            self.budget.new_turn()
            await self.client.query(self._message(self._prompt(text, agreed, len(photos)), photos))
            timing.mark("sent")
            async for msg in self.client.receive_response():
                if chunk := self._chunk(msg, spoken):
                    yield chunk
                elif isinstance(msg, ResultMessage):
                    finished = True
                    timing.mark("done")
                    if question := self.shell.question("".join(spoken)):
                        spoken.append(question)
                        yield question
                    if msg.is_error:
                        log.error("turn failed: %s %s", msg.subtype, msg.result)
                        if not spoken:
                            limit = self.limits.describe(msg.result)
                            yield "Sorry, that didn't work. " + (f"I've hit {limit}." if limit else "Something went wrong on my end.")
        except (asyncio.CancelledError, GeneratorExit):
            interrupted = True  # logged as cut off, not blank
            if not finished and self.client is not None:
                self._talked_over(text)
            raise
        except ClaudeSDKError:
            log.exception("Claude CLI failed")
            await self.close()
            yield " I can't reach Claude right now."
        finally:
            conversation.current().heard.replied("".join(spoken))
            said = f"{text} [sent {_photos(len(photos))}]" if photos else text
            memory.log_exchange(said, "".join(spoken), approved, interrupted=interrupted)

    async def follow_up(self, prompt: str, words: str = "") -> AsyncIterator[str]:
        """A turn the brain starts itself on a kept call's result (`prompt`), spoken unprompted through the floor.

        Nobody spoke, so nothing is recorded as the user's words. If they haven't spoken since `words` (the turn that
        made the call), it carries that turn on: an order in it ("merge and push") counts as it would have there.
        Otherwise their latest words are spent, so nothing here acts on them. The user's next turn stops it.
        """
        async with self._turns:
            await self._drained()
            self._following, self._preempted = True, False
            spoken: list[str] = []
            said: list[str] = []  # spoken aloud: nothing once the user's turn stops it
            finished = False
            heard = conversation.current().heard
            self.shell.new_turn("")
            if words and heard.said == words.strip():
                self.shell.said = heard.said
            else:
                heard.spent = True
            try:
                await self.start()
                await self._set_model(self._main)  # next_model is for the user's next turn
                self.budget.new_turn()
                await self.client.query("\n".join([self.head(), prompt]))
                async for msg in self.client.receive_response():
                    chunk = self._chunk(msg, spoken, timed=False)
                    if chunk and not self._preempted:
                        said.append(chunk)
                        yield chunk
                    elif isinstance(msg, ResultMessage):
                        finished = True
                        if msg.is_error:
                            log.warning("follow-up ended: %s %s", msg.subtype, msg.result)
                        elif not self._preempted and (question := self.shell.question("".join(said))):
                            said.append(question)
                            yield question
            except (asyncio.CancelledError, GeneratorExit):
                if not finished and self.client is not None:
                    self._talked_over("")
                raise
            except ClaudeSDKError:
                log.exception("Claude CLI failed during a follow-up")
                await self.close()
            finally:
                self._following = False
                if text := "".join(said).strip():
                    heard.replied(text)  # a "yes" next answers it
                    memory.log_announcement(text)
