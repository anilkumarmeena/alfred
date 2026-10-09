"""How long each voice turn takes, stage by stage, so the wait between the user stopping and JARVIS speaking can be
measured and its slow part found.

Each turn that reaches JARVIS is one line in logs/voice-turns.jsonl, and a readable one in the agent log, once its
reply has been spoken, or once the user's next turn or the session's end has cut it off. Each stage is the seconds
since the user stopped speaking (LiveKit's VAD end of speech; for a typed turn, since it arrived):

  transcript   Whisper's text was ready (LiveKit's transcription_delay)
  turn_end     the turn detector decided they'd finished (LiveKit's end_of_turn_delay)
  brain        the reply started (JarvisAgent.llm_node)
  sent         the words went to the claude CLI: after any yes-or-no check, a command still running, photos
  first_text   the reply's first words
  first_tool   the model began its first tool call, if it made one
  first_audio  JARVIS's voice started
  done         the model's reply ended

With them: `speech` (seconds the user spoke), `words` (in their turn), `tts_first` (Kokoro's time from its first text
to its first audio, LiveKit's tts_node_ttfb), `ack` (what was said before the first tool call: the acknowledgement),
`tools` (how many calls), `spoke` (seconds JARVIS spoke), and how it `ended`: spoken, interrupted (talked over while
speaking), unspoken (the user went on before any of it was heard) or closed (the session ended first). An unspoken
turn also has `resumed`, the seconds from the user stopping to their speaking again: a pause mid-thought.

How the turn's end was decided (jarvis.voice.turn): `eot`, the audio turn detector's probability at the last pause;
`judge`, the fast model's verdict when it was asked (yes, no, unsure, or none when it failed or was slow) and
`judge_s`, how long it took; `wait`, whether the short or the long delay ended the turn. And who spoke and how well
they were heard: `speaker` (the voiceprint matched, or the owner) and `logprob`, Whisper's average log-probability.
`piece` is how many words the reply's first spoken piece had, and `model` the model that answered when it wasn't the
voice brain's own (jarvis.brain.route).

The brain marks its own stages through mark() and tool(), which do nothing outside a timed turn.

python -m jarvis.voice.timing [HOURS] (make voice-timing) summarises the last day's turns.
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
# The waits between stages that the summary breaks a turn into: (name, from, to)
GAPS = (("waiting for the turn to end", None, "turn_end"), ("before the model", "turn_end", "sent"),
        ("model to first words", "sent", "first_text"), ("first words to voice", "first_text", "first_audio"))

CURRENT: ContextVar["Turn | None"] = ContextVar("jarvis_voice_turn", default=None)  # the turn being replied to
LATEST: "Turn | None" = None  # the same, for what runs outside the reply's own task


class Turn:
    """One user turn's timing. `metrics` are LiveKit's for the user's message (llm.MetricsReport); none for a typed
    turn."""

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
        """`stage` was reached now (or at `at`, a time.time()); only the first time counts."""
        self.record.setdefault(stage, round((self.clock() if at is None else at) - self.end, 3))

    def tool(self, said: str, name: str = "") -> None:
        """The model began a call to tool `name`, having said `said` so far in this reply."""
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
    """Something about the turn being replied to, such as the model that answered it."""
    if (turn := CURRENT.get()) is not None:
        turn.record[key] = value


def piece(text: str) -> None:
    """The reply's first piece went to Kokoro (jarvis.voice.clauses). Speech runs in a task of LiveKit's own, which
    doesn't see CURRENT, so this goes to the turn replied to last."""
    if LATEST is not None:
        LATEST.record.setdefault("piece", len(text.split()))


class Timings:
    """A voice session's turns, timed one at a time, each written when it's over."""

    def __init__(self, path: Path | None = VOICE_TURNS_LOG, clock: Callable[[], float] = time.time,
                 written: Callable[[dict], None] | None = None) -> None:
        self.path = path
        self.clock = clock
        self.written = written  # told each record once its turn is over (jarvis.voice.turn.Pacing)
        self.turn: Turn | None = None
        self.n = 0

    def heard(self, said: str, metrics: dict, how: dict | None = None) -> dict | None:
        """A user turn ended (Agent.on_user_turn_completed); `how` is how its end was decided and who said it. One
        before it not yet heard was cut off by it: that one's record is returned."""
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
        """The reply to the latest turn starts (JarvisAgent.llm_node): the turn the brain's marks go to. A turn that
        came with no user turn ended, a typed one, is timed from now."""
        global LATEST
        if self.turn is None or "brain" in self.turn.record:
            self.heard("", {})
        self.turn.mark("brain")
        CURRENT.set(self.turn)
        LATEST = self.turn
        return self.turn

    def spoke(self, metrics: dict, interrupted: bool) -> None:
        """JARVIS finished saying something (conversation_item_added, an assistant message); its metrics are LiveKit's.
        Only a reply to the turn under way ends it: not the greeting, nor a reply cut off before the user ended this
        turn, which started speaking before they stopped."""
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
        """The session ended."""
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
    """One turn's record as a line for the agent log."""
    heard = "typed" if r.get("typed") else f"{r.get('speech', 0):.1f} s of speech"
    stages = ", ".join(f"{s.replace('_', ' ')} {r[s]:.2f}" for s in STAGES if s in r) or "no stages"
    tools = r.get("tools", 0)
    line = (f"voice turn {r['turn']} ({heard}, {r['words']} words): {stages}; "
            f"{tools} tool{'' if tools == 1 else 's'}; {r['ended']}")
    if "resumed" in r:
        line += f", resumed after {r['resumed']:.2f} s"
    if "eot" in r:
        line += f"; end: detector {r['eot']:.2f}"
        if "judge" in r:
            line += f", model {r['judge']} in {r.get('judge_s', 0):.2f} s"
        line += f", {r.get('wait', 'short')} wait"
    if "ack" in r:
        line += f"; ack {r['ack']!r}"
    return line


def _spread(values: list[float]) -> str:
    values = sorted(values)
    p90 = values[min(len(values) - 1, round(0.9 * (len(values) - 1)))]
    return f"median {statistics.median(values):5.2f} s   p90 {p90:5.2f} s   max {values[-1]:5.2f} s   ({len(values)})"


def summary(path: Path = VOICE_TURNS_LOG, since: float = 0.0, slowest: int = 5) -> str:
    """The turns in `path` since `since`: how they ended, each stage's and each wait's spread, and the slowest turns."""
    try:
        lines = path.read_text().splitlines()
    except FileNotFoundError:
        return f"No turns yet: {path} doesn't exist."
    records = []
    for line in lines:
        try:
            records.append(json.loads(line))
        except ValueError:  # a line cut short by a crash
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
    if judged := [r for r in records if "judge" in r]:
        verdicts = Counter(r["judge"] for r in judged)
        out += ["", f"The model judged {len(judged)} turn ends: "
                + ", ".join(f"{n} {verdict}" for verdict, n in verdicts.most_common()) + ".",
                f"  {'its time':28} {_spread([r.get('judge_s', 0.0) for r in judged])}"]
    for wait in ("short", "long"):
        if waited := [r for r in records if r.get("wait") == wait]:
            went_on = sum(r["ended"] == "unspoken" for r in waited)
            out.append(f"After the {wait} wait the user went on {went_on} of {len(waited)} times.")
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


if __name__ == "__main__":  # python -m jarvis.voice.timing [HOURS]: the last day's turns by default
    print(summary(since=time.time() - float(sys.argv[1] if sys.argv[1:] else 24) * 3600))
