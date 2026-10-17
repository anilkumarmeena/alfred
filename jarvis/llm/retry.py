"""Retry and error policy for model calls. Failures are classified from SDK/CLI signals, not error text:

  LIMIT    usage limit (rejected rate-limit event, 429, CLI rate_limit): passes when the window resets
  PASSING  network, overload, 5xx/408, CLI server_error, CLI died or timed out: passes on its own
  ERROR    anything else: retrying won't help

`retry` runs a call under a Policy; self-rescheduling loops call `Policy.wait` directly.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TypeVar

from claude_agent_sdk import CLIConnectionError, CLIJSONDecodeError, CLINotFoundError, ProcessError, ResultError

log = logging.getLogger("jarvis.llm.retry")
T = TypeVar("T")

LIMIT, PASSING, ERROR = "limit", "passing", "error"
KINDS = frozenset({LIMIT, PASSING, ERROR})
PASSING_STATUS = {408, 500, 502, 503, 504, 529}  # 529: overloaded


class CallFailed(RuntimeError):
    """A model call that ended in an error result."""

    def __init__(self, message: str, kind: str) -> None:
        super().__init__(message)
        self.kind = kind


def result_kind(status: int | None = None, reason: str | None = None, rejected: bool = False,
                error: str | None = None) -> str:
    """Kind of a failed result. `rejected`: a rate-limit event said the limit is used up; `error`:
    AssistantMessage.error."""
    if rejected or status == 429 or error == "rate_limit":
        return LIMIT
    if error == "server_error" or status in PASSING_STATUS or (status is None and reason == "api_error"):
        return PASSING
    return ERROR


def classify(e: BaseException, passing: tuple[type[BaseException], ...] = ()) -> str:
    """Kind of an exception, by type. `passing`: extra caller types to treat as PASSING."""
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
    """`waits`: seconds before each retry (empty: one try). `retries`: kinds retried. `forever`: repeat the last
    wait indefinitely. `passing`: extra exception types treated as PASSING."""
    waits: tuple[float, ...] = ()
    retries: frozenset[str] = frozenset({LIMIT, PASSING})
    forever: bool = False
    passing: tuple[type[BaseException], ...] = ()

    def wait(self, failed: int, kind: str) -> float | None:
        """Seconds before the next try after `failed` failures (latest of `kind`); None to give up."""
        if kind not in self.retries or not self.waits or failed < 1:
            return None
        if failed <= len(self.waits):
            return self.waits[failed - 1]
        return self.waits[-1] if self.forever else None

    def kind(self, e: BaseException) -> str:
        return classify(e, self.passing)


ONCE = Policy()  # one try; caller has its own fallback


async def retry(policy: Policy, call: Callable[[], Awaitable[T]], label: str,
                sleep: Callable[[float], Awaitable[object]] = asyncio.sleep) -> T:
    """Run `call()` under `policy`, raising the last failure. Logs only error type and kind (no call data)."""
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
