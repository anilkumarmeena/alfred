"""Token usage of task runs: input, output, cache read and cache write tokens per model and per subagent.

  Tally      counts one run as its session streams (alfred.tasks.worker), giving rows when it ends, stopped included
  summary    a task's rows (alfred.tasks.store, task_tokens table) totalled for task details

The result message's per-model usage covers every call of the run, a background subagent's or the CLI's own side calls
included, but arrives only as a turn ends; assistant messages carry each call's usage as it happens, tagged with the
subagent that made it. A run's per-model totals are the larger of the two, and the task's own session gets what its
subagents' streamed calls didn't use (its own calls and any never streamed), so a run's rows add up to its total once.
"""

from alfred.llm.sdk import AssistantMessage, ResultMessage

FIELDS = ("input", "output", "cache_read", "cache_write")
_CALL = dict(zip(FIELDS, ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")))
_MODEL = dict(zip(FIELDS, ("inputTokens", "outputTokens", "cacheReadInputTokens", "cacheCreationInputTokens")))
FINISHED, FAILED, STOPPED = ENDINGS = ("finished", "failed", "stopped")  # how a run ended
MAIN = ""  # agent of the task's own session; subagents go by their label ("code searcher")
MAIN_LABEL = "main session"

Counts = tuple[int, int, int, int]  # in FIELDS order
_NONE: Counts = (0, 0, 0, 0)


def _counts(usage: dict | None, keys: dict[str, str]) -> Counts:
    usage = usage or {}
    return tuple(max(int(usage.get(keys[f]) or 0), 0) for f in FIELDS)  # type: ignore[return-value]


def _add(a: Counts, b: Counts) -> Counts:
    return tuple(x + y for x, y in zip(a, b))  # type: ignore[return-value]


def _less(a: Counts, b: Counts) -> Counts:
    return tuple(max(x - y, 0) for x, y in zip(a, b))  # type: ignore[return-value]


def _most(a: Counts, b: Counts) -> Counts:
    return tuple(max(x, y) for x, y in zip(a, b))  # type: ignore[return-value]


def _model(name: str) -> str:
    """The model without the CLI's context suffix ("claude-x[1m]"), so per-model usage matches each call's model."""
    return name.split("[", 1)[0] or "unknown"


class Tally:
    """One run's token usage, counted from its session's messages."""

    def __init__(self) -> None:
        self.calls: dict[str, tuple[str, str, Counts]] = {}  # API message id -> (agent, model, counts)
        self.reported: dict[str, Counts] = {}  # model -> the latest result's usage, the whole run so far
        self.then: dict[str, Counts] = {}  # model -> calls counted when that result came

    def saw(self, msg: AssistantMessage, agent: str = MAIN) -> None:
        """One assistant message; the CLI splits a call's reply into a message per block, all with the same usage."""
        if msg.usage:
            self.calls[msg.message_id or msg.uuid or f"#{len(self.calls)}"] = (
                agent, _model(msg.model), _counts(msg.usage, _CALL))

    def result(self, msg: ResultMessage) -> None:
        if msg.model_usage:
            self.reported, self.then = {}, self._per_model()
            for name, used in msg.model_usage.items():
                self.reported[_model(name)] = _add(self.reported.get(_model(name), _NONE), _counts(dict(used), _MODEL))

    def _per_model(self) -> dict[str, Counts]:
        found: dict[str, Counts] = {}
        for _, model, counts in self.calls.values():
            found[model] = _add(found.get(model, _NONE), counts)
        return found

    def rows(self) -> list[dict]:
        """{agent, model, input, output, cache_read, cache_write} per agent and model that used any."""
        by: dict[tuple[str, str], Counts] = {}
        for agent, model, counts in self.calls.values():
            by[agent, model] = _add(by.get((agent, model), _NONE), counts)
        counted = self._per_model()
        for model in {*counted, *self.reported}:
            # The latest result's usage plus the calls since, unless the calls counted come to more
            since = _less(counted.get(model, _NONE), self.then.get(model, _NONE))
            total = _most(_add(self.reported.get(model, _NONE), since), counted.get(model, _NONE))
            helpers = _less(counted.get(model, _NONE), by.get((MAIN, model), _NONE))
            by[MAIN, model] = _less(total, helpers)
        return [{"agent": agent, "model": model} | dict(zip(FIELDS, counts))
                for (agent, model), counts in sorted(by.items()) if any(counts)]


def checked(rows: object) -> list[dict]:
    """Rows as another machine sent them, kept only where well formed."""
    kept = []
    for row in rows if isinstance(rows, list) else []:
        if isinstance(row, dict) and isinstance(row.get("model"), str) and isinstance(row.get("agent", MAIN), str):
            try:
                counts = {f: max(int(row.get(f) or 0), 0) for f in FIELDS}
            except (TypeError, ValueError):
                continue
            kept.append({"agent": row.get("agent", MAIN)[:80], "model": row["model"][:120]} | counts)
    return kept


def _total(counts: dict) -> dict:
    return counts | {"total": sum(counts[f] for f in FIELDS)}


def summary(rows: list[dict]) -> dict | None:
    """A task's token use over all its runs: totals, then by model and by agent (MAIN_LABEL for its own session);
    None if none was recorded."""
    if not rows:
        return None
    zero = dict.fromkeys(FIELDS, 0)
    whole, models, agents = dict(zero), {}, {}
    for row in rows:
        for into in (whole, models.setdefault(row["model"], dict(zero)),
                     agents.setdefault(row["agent"] or MAIN_LABEL, dict(zero))):
            for f in FIELDS:
                into[f] += row[f]
    runs = len({row["at"] for row in rows})

    def biggest(found: dict) -> dict:
        return {k: _total(v) for k, v in sorted(found.items(), key=lambda kv: -sum(kv[1].values()))}

    return _total(whole) | {"runs": runs, "by_model": biggest(models), "by_agent": biggest(agents)}
