"""The helpers running beside each voice conversation (its lookups and tool calls carrying on in the background), held
by the task runner so the control panel, the phone and task_list can list them beside tasks.

Each voice session reports its whole list (alfred.voice.session, from alfred.contracts.call.Helper views) whenever it
changes, and every HEARTBEAT seconds while it has any; a session not heard from for STALE seconds is forgotten (its
process ended without saying so). Held in memory only: they live no longer than their conversation. A stop from the
panel goes back to the sessions on the event stream ("helper_stop"); the one running it stops it as background_stop
would, and its next report shows it stopped.
"""

import time

HEARTBEAT = 10  # seconds between a session's reports while it has helpers to show
STALE = 3 * HEARTBEAT
STOPPING = "stopping"


class Helpers:
    def __init__(self) -> None:
        self.sessions: dict[str, tuple[float, list[dict]]] = {}  # session -> (when last heard, its helpers)
        self.stopping: set[str] = set()  # ids asked to stop that haven't ended yet

    def report(self, session: str, helpers: list[dict]) -> bool:
        """Take a session's whole list; True if what's listed changed."""
        before = self.listed()
        if helpers:
            self.sessions[session] = (time.monotonic(), helpers)
        else:
            self.sessions.pop(session, None)
        self.stopping &= {h["id"] for h in self._all() if h["ended"] is None}
        return self.listed() != before

    def _all(self) -> list[dict]:
        now = time.monotonic()
        for gone in [s for s, (heard, _) in self.sessions.items() if now - heard > STALE]:
            del self.sessions[gone]
        return [h for _, helpers in self.sessions.values() for h in helpers]

    def listed(self) -> list[dict]:
        """Every session's helpers, oldest first; one asked to stop shows as stopping until it has."""
        out = [h | {"status": STOPPING} if h["id"] in self.stopping and h["ended"] is None else h for h in self._all()]
        return sorted(out, key=lambda h: h["started"])

    def stop(self, helper: str) -> dict:
        """Mark a running helper as being stopped; ValueError if none is running by that id."""
        found = next((h for h in self._all() if h["id"] == helper), None)
        if found is None or found["ended"] is not None:
            raise ValueError("That lookup isn't running any more.")
        self.stopping.add(helper)
        return found | {"status": STOPPING}
