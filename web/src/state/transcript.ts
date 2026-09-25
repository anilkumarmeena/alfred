// The conversation as captioned by LiveKit: a line per segment, streamed in as its text arrives. Each
// line's text is its own signal, so a streamed chunk updates one text node rather than the list.
import { signal, type Signal } from "@preact/signals";
import type { Line } from "../types";

const MAX_LINES = 80;

export const lines = signal<Line[]>([]);
const bySegment = new Map<string, Signal<string>>();  // so a re-sent segment replaces its line
let nextId = 0, spoke = false;

function push(line: Line) {
  const next = [...lines.value, line];
  while (next.length > MAX_LINES) {
    const old = next.shift()!;
    if (old.kind === "msg" && old.segment) bySegment.delete(old.segment);
  }
  lines.value = next;
}

// A new line to stream text into, or the old one emptied if LiveKit sends the segment again. said: what it says
// so far, for a line that arrives whole (the iOS app's captions), so it isn't emptied and refilled.
export function addLine(who: "you" | "jarvis", segment?: string, said = ""): Signal<string> {
  const old = segment && bySegment.get(segment);
  if (old) { old.value = said; return old; }
  const text = signal(said);
  if (segment) bySegment.set(segment, text);
  push({ kind: "msg", id: nextId++, who, text, segment });
  spoke = true;
  return text;
}

export function addDivider(text: string): void {
  push({ kind: "divider", id: nextId++, text });
}

// Whether anyone said anything since the session began, so an empty session leaves no divider.
export const hasSpoken = () => spoke;
export function resetSpoken(): void { spoke = false; }
