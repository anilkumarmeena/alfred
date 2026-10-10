"""One retry and error policy for JARVIS's model calls and the loops around them.

A failure is one of three kinds, read from what the SDK and the CLI report rather than from an error's words:

  LIMIT    Claude's usage limit (a rejected rate-limit event, an API 429, the CLI's rate_limit error): it passes when
           its window resets (jarvis.brain.usage says when)
  PASSING  the network, Claude overloaded or briefly unreachable (an API 5xx, 408, the CLI's server_error), the CLI
           dying mid-call or not answering in time: it passes on its own
  ERROR    anything else: trying again won't help

A Policy says which kinds are tried again, and how long to wait before each further try. `retry` runs a call under
one, waiting between tries; a loop that comes back on its own later (the scheduler, the learning review) asks
`Policy.wait` when its next try is due instead.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TypeVar

from claude_agent_sdk import CLIConnectionError, CLIJSONDecodeError, CLINotFoundError, ProcessError, ResultError

log = logging.getLogger("jarvis.brain.retry")
T = TypeVar("T")

LIMIT, PASSING, ERROR = "limit", "passing", "error"
KINDS = frozenset({LIMIT, PASSING, ERROR})
PASSING_STATUS = {408, 500, 502, 503, 504, 529}  # 529: overloaded


class CallFailed(RuntimeError):
    """A model call that ended in an error result, with its kind."""

    def __init__(self, message: str, kind: str) -> None:
        super().__init__(message)
        self.kind = kind


def result_kind(status: int | None = None, reason: str | None = None, rejected: bool = False,
                error: str | None = None) -> str:
    """The kind of a failed result, from its API status and terminal reason, whether a rate-limit event said the limit
    was used up (`rejected`), and the CLI's error on the turn's assistant message (AssistantMessage.error)."""
    if rejected or status == 429 or error == "rate_limit":
        return LIMIT
    if error == "server_error" or status in PASSING_STATUS or (status is None and reason == "api_error"):
        return PASSING
    return ERROR


def classify(e: BaseException, passing: tuple[type[BaseException], ...] = ()) -> str:
    """The kind of an exception, by its type (and for the SDK's ResultError, what the CLI said of the result).
    `passing` adds the caller's own types that pass on their own."""
    if isinstance(e, CallFailed):
        return e.kind
    if isinstance(e, ResultError):
        return result_kind(e.api_error_status, e.terminal_reason)
    if isinstance(e, CLINotFoundError):
        return ERROR
    if isinstance(e, (CLIConnectionError, ProcessError, CLIJSONDecodeError, TimeoutError, ConnectionError, *passing)):
        return PASSING
    return ERROR


@dataclass(frozen=True)
class Policy:
    """`waits`: seconds before each further try, in turn (none: one try). `retries`: the kinds tried again. `forever`:
    once the waits run out, keep trying at the last one. `passing`: exception types of the caller's that pass."""
    waits: tuple[float, ...] = ()
    retries: frozenset[str] = frozenset({LIMIT, PASSING})
    forever: bool = False
    passing: tuple[type[BaseException], ...] = ()

    def wait(self, failed: int, kind: str) -> float | None:
        """Seconds before the next try, once `failed` tries have failed (the latest of `kind`); None to give up."""
        if kind not in self.retries or not self.waits or failed < 1:
            return None
        if failed <= len(self.waits):
            return self.waits[failed - 1]
        return self.waits[-1] if self.forever else None

    def kind(self, e: BaseException) -> str:
        return classify(e, self.passing)


ONCE = Policy()  # one try: the caller has its own fallback


async def retry(policy: Policy, call: Callable[[], Awaitable[T]], label: str,
                sleep: Callable[[float], Awaitable[object]] = asyncio.sleep) -> T:
    """`call()`'s result, tried again as `policy` says; the last failure is raised. Retries are logged by the error's
    type and kind alone (nothing of the call's data), with the traceback for an ERROR a policy retries anyway."""
    failed = 0
    while True:
        try:
            return await call()
        except Exception as e:
            failed += 1
            kind = policy.kind(e)
            if (wait := policy.wait(failed, kind)) is None:
                raise
            log.log(logging.INFO if kind == PASSING else logging.WARNING, "%s failed (%s: %s); trying again in %gs",
                    label, kind, type(e).__name__, wait, exc_info=kind == ERROR)
        await sleep(wait)
