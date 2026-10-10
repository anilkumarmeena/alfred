// Transcript lines, one per LiveKit segment. Each line's text is its own signal, so a streamed chunk updates one
// text node rather than the list.
import { signal, type Signal } from "@preact/signals";
import type { Line } from "../types";

const MAX_LINES = 80;

export const lines = signal<Line[]>([]);
const bySegment = new Map<string, Signal<string>>();  // a re-sent segment replaces its line
let nextId = 0, spoke = false;

function push(line: Line) {
  const next = [...lines.value, line];
  while (next.length > MAX_LINES) {
    const old = next.shift()!;
    if (old.kind === "msg" && old.segment) bySegment.delete(old.segment);
  }
  lines.value = next;
}

// New line to stream into, or the existing one cleared if the segment is re-sent. said: full text for lines that
// arrive whole (iOS app captions), so they aren't cleared and refilled.
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

// Whether anything was said this session; an empty session gets no divider.
export const hasSpoken = () => spoke;
export function resetSpoken(): void { spoke = false; }
