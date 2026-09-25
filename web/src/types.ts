import type { Signal } from "@preact/signals";

// LiveKit's lk.agent.state, plus the page's own states around a connection.
export type AgentState = "idle" | "connecting" | "initializing" | "listening" | "thinking" | "speaking" | "reconnecting";

export type Link = "off" | "linking" | "on";

// The mic dropped out, typically while the Home Screen app was in the background: micGuard is getting
// it back ("recovering"), gave up until the next tap ("retry"), or the session itself is coming back.
export type Paused = "" | "recovering" | "retry" | "resuming";

// What a tapped notification was about: /?task=3, /?alert=calendar, or a file JARVIS made, /?file=TOKEN&name=…,
// which the card plays if it's audio and links to either way.
export interface Focus { label: string; body: string; talk: string; file?: { src: string; name: string; audio: boolean } }

export type Line =
  | { kind: "msg"; id: number; who: "you" | "jarvis"; text: Signal<string>; segment?: string }
  | { kind: "divider"; id: number; text: string };
