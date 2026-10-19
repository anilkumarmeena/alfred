import type { Signal } from "@preact/signals";

// LiveKit's lk.agent.state plus page-side connection states.
export type AgentState = "idle" | "connecting" | "initializing" | "listening" | "thinking" | "speaking" | "reconnecting";

export type Link = "off" | "linking" | "on";

// Why the mic is out: micGuard "recovering", "retry" (gave up until next tap), or the session "resuming".
export type Paused = "" | "recovering" | "retry" | "resuming";

// Focus card for a tapped notification: /?task=3, /?alert=calendar, or /?file=TOKEN&name=… (audio plays inline).
export interface Focus { label: string; body: string; talk: string; file?: { src: string; name: string; audio: boolean } }

export type Line =
  | { kind: "msg"; id: number; who: "you" | "alfred"; text: Signal<string>; segment?: string }
  | { kind: "divider"; id: number; text: string };
