"""Per-stage latency of each voice turn, to find where the user-stops-to-JARVIS-speaks wait goes.

One JSON line per turn in logs/voice-turns.jsonl (plus a readable agent-log line), written when the reply is spoken
or cut off. Stages are seconds since the user stopped speaking (or a typed turn arrived):

  transcript   Whisper text ready          first_text   first reply words
  turn_end     silence ended the turn      first_tool   first tool call began
  brain        reply started (llm_node)    first_audio  JARVIS's voice started
  sent         prompt sent to claude CLI   done         model reply ended

Other fields: speech, words, tts_first, ack (said before first tool), tools, tool_names, spoke, ended
(spoken|interrupted|unspoken|closed), resumed (unspoken only), speaker, logprob, piece (words in first spoken piece),
model (when not the voice brain's own).

mark()/tool()/note() are no-ops outside a timed turn. `python -m jarvis.voice.timing [HOURS]` summarises recent turns.
"""

import json
import logging
import statistics
import sys
import time
from collections import Counter
from collections.abc import Callable
from contextvars import ContextVar
from pathlib import Path

from jarvis.core.config import VOICE_TURNS_LOG

log = logging.getLogger("jarvis.voice.timing")

STAGES = ("transcript", "turn_end", "brain", "sent", "first_text", "first_tool", "first_audio", "done")
# Summary gaps: (name, from, to)
GAPS = (("waiting for the turn to end", None, "turn_end"), ("before the model", "turn_end", "sent"),
        ("model to first words", "sent", "first_text"), ("first words to voice", "first_text", "first_audio"))

CURRENT: ContextVar["Turn | None"] = ContextVar("jarvis_voice_turn", default=None)  # turn being replied to
LATEST: "Turn | None" = None  # same, for code outside the reply's task


class Turn:
    """One user turn's timing. `metrics` is LiveKit's MetricsReport for the message; empty for a typed turn."""

    def __init__(self, n: int, said: str, metrics: dict, clock: Callable[[], float] = time.time) -> None:
        self.clock = clock
        stopped = metrics.get("stopped_speaking_at")
        self.end = stopped or clock()
        self.record: dict = {"time": round(self.end, 1), "turn": n, "words": len(said.split())}
        if not stopped:
            self.record["typed"] = True
        if started := metrics.get("started_speaking_at"):
            self.record["speech"] = round(self.end - started, 3)
        for stage, key in (("transcript", "transcription_delay"), ("turn_end", "end_of_turn_delay")):
            if (value := metrics.get(key)) is not None:
                self.record[stage] = round(value, 3)

    def mark(self, stage: str, at: float | None = None) -> None:
        """Record `stage` at now (or `at`, epoch seconds); first call wins."""
        self.record.setdefault(stage, round((self.clock() if at is None else at) - self.end, 3))

    def tool(self, said: str, name: str = "") -> None:
        """Record a tool call; `said` is reply text so far (the first one becomes `ack`)."""
        if "first_tool" not in self.record and said.strip():
            self.record["ack"] = said.strip()
        self.mark("first_tool")
        self.record["tools"] = self.record.get("tools", 0) + 1
        if name:
            self.record.setdefault("tool_names", []).append(name)


def mark(stage: str) -> None:
    if (turn := CURRENT.get()) is not None:
        turn.mark(stage)


def tool(said: str, name: str = "") -> None:
    if (turn := CURRENT.get()) is not None:
        turn.tool(said, name)


def note(key: str, value) -> None:
    """Set a field on the current turn's record."""
    if (turn := CURRENT.get()) is not None:
        turn.record[key] = value


def piece(text: str) -> None:
    """Record the first piece sent to Kokoro. TTS runs in LiveKit's own task (no CURRENT), so this uses LATEST."""
    if LATEST is not None:
        LATEST.record.setdefault("piece", len(text.split()))


class Timings:
    """A session's turn timings, one at a time, each written when it ends."""

    def __init__(self, path: Path | None = VOICE_TURNS_LOG, clock: Callable[[], float] = time.time,
                 written: Callable[[dict], None] | None = None) -> None:
        self.path = path
        self.clock = clock
        self.written = written  # gets each finished record (turn.Pacing.learn)
        self.turn: Turn | None = None
        self.n = 0

    def heard(self, said: str, metrics: dict, how: dict | None = None) -> dict | None:
        """Start timing a user turn; `how` holds speaker fields.

        Returns the previous turn's record if it was still pending (written as "unspoken"), else None."""
        cut = None
        if self.turn is not None:
            if started := metrics.get("started_speaking_at"):
                self.turn.record["resumed"] = round(started - self.turn.end, 3)
            cut = self._write("unspoken")
        self.n += 1
        self.turn = Turn(self.n, said, metrics, self.clock)
        self.turn.record |= how or {}
        return cut

    def replying(self) -> Turn:
        """Mark the reply's start and make its turn CURRENT. A reply with no pending turn (typed) is timed from now."""
        global LATEST
        if self.turn is None or "brain" in self.turn.record:
            self.heard("", {})
        self.turn.mark("brain")
        CURRENT.set(self.turn)
        LATEST = self.turn
        return self.turn

    def spoke(self, metrics: dict, interrupted: bool) -> None:
        """Close the turn on an assistant message. Ignores the greeting and speech that began before the turn ended."""
        turn, started = self.turn, metrics.get("started_speaking_at")
        if turn is None or "brain" not in turn.record or not started or started < turn.end:
            return
        turn.mark("first_audio", started)
        if (ttfb := metrics.get("tts_node_ttfb")) is not None:
            turn.record["tts_first"] = round(ttfb, 3)
        if stopped := metrics.get("stopped_speaking_at"):
            turn.record["spoke"] = round(stopped - started, 3)
        self._write("interrupted" if interrupted else "spoken")

    def close(self) -> None:
        """Write any pending turn as "closed"."""
        if self.turn is not None:
            self._write("closed")

    def _write(self, ended: str) -> dict:
        record = self.turn.record | {"ended": ended}
        self.turn = None
        log.info("%s", describe(record))
        if self.written is not None:
            try:
                self.written(record)
            except Exception:
                log.exception("learning from a turn's timing failed")
        if self.path is None:
            return record
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a") as f:
                f.write(json.dumps(record) + "\n")
        except OSError as e:
            log.warning("couldn't write %s: %s", self.path, e)
        return record


def describe(r: dict) -> str:
    """One-line agent-log form of a record."""
    heard = "typed" if r.get("typed") else f"{r.get('speech', 0):.1f} s of speech"
    stages = ", ".join(f"{s.replace('_', ' ')} {r[s]:.2f}" for s in STAGES if s in r) or "no stages"
    tools = r.get("tools", 0)
    line = (f"voice turn {r['turn']} ({heard}, {r['words']} words): {stages}; "
            f"{tools} tool{'' if tools == 1 else 's'}; {r['ended']}")
    if "resumed" in r:
        line += f", resumed after {r['resumed']:.2f} s"
    if "ack" in r:
        line += f"; ack {r['ack']!r}"
    return line


def _spread(values: list[float]) -> str:
    values = sorted(values)
    p90 = values[min(len(values) - 1, round(0.9 * (len(values) - 1)))]
    return f"median {statistics.median(values):5.2f} s   p90 {p90:5.2f} s   max {values[-1]:5.2f} s   ({len(values)})"


def summary(path: Path = VOICE_TURNS_LOG, since: float = 0.0, slowest: int = 5) -> str:
    """Report on turns since `since`: outcomes, per-stage and per-gap spread, slowest turns."""
    try:
        lines = path.read_text().splitlines()
    except FileNotFoundError:
        return f"No turns yet: {path} doesn't exist."
    records = []
    for line in lines:
        try:
            records.append(json.loads(line))
        except ValueError:  # truncated by a crash
            continue
    records = [r for r in records if r.get("time", 0) >= since]
    if not records:
        return "No turns in that time."
    ended = Counter(r["ended"] for r in records)
    out = [f"{len(records)} turns: " + ", ".join(f"{n} {how}" for how, n in ended.most_common()) + ".",
           "", "Seconds after the user stopped speaking:"]
    for stage in STAGES:
        if values := [r[stage] for r in records if stage in r]:
            out.append(f"  {stage:28} {_spread(values)}")
    out += ["", "Where the wait goes:"]
    for name, start, end in GAPS:
        if values := [r[end] - (r[start] if start else 0) for r in records if end in r and (not start or start in r)]:
            out.append(f"  {name:28} {_spread(values)}")
    for name, key in (("Kokoro's text to first audio", "tts_first"), ("pause before going on", "resumed")):
        if values := [r[key] for r in records if key in r]:
            out.append(f"  {name:28} {_spread(values)}")
    plain = [r for r in records if "first_audio" in r and "first_tool" not in r]
    if plain:
        out += ["", "First audio, turns with no tools:"]
        for name, lighter in (("on the voice brain's model", False), ("on the lighter model", True)):
            if values := [r["first_audio"] for r in plain if bool(r.get("model")) == lighter]:
                out.append(f"  {name:28} {_spread(values)}")
    if acks := [r for r in records if "first_tool" in r]:
        out += ["", f"{sum('ack' in r for r in acks)} of {len(acks)} turns with tools said something first."]
        if work := [r["done"] - r["first_tool"] for r in acks if "done" in r]:
            out.append(f"  {'first tool call to done':28} {_spread(work)}")
        handed = sum("background_lookup" in r.get("tool_names", ()) for r in acks)
        out.append(f"{handed} handed slow reading to a background lookup.")
    if timed := sorted((r for r in records if "first_audio" in r), key=lambda r: r["first_audio"], reverse=True):
        out += ["", "Slowest to first audio:"]
        out += [f"  {time.strftime('%m-%d %H:%M:%S', time.localtime(r['time']))}  {describe(r)}"
                for r in timed[:slowest]]
    return "\n".join(out)


if __name__ == "__main__":  # [HOURS], default 24
    print(summary(since=time.time() - float(sys.argv[1] if sys.argv[1:] else 24) * 3600))
