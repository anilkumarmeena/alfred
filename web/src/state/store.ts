// Page state as signals. App.tsx mirrors the ones CSS keys on to body[data-*].
import { computed, signal } from "@preact/signals";
import type { AgentState, Focus, Link, Paused } from "../types";
import type { MicMode, Permission } from "../lib/native";
import { PASSCODE_KEY, PUSH_KEY, SPEAKER_KEY, storage } from "../lib/storage";

const STATES: Record<AgentState, [string, string]> = {
  idle: ["Standing by", "Tap the core to connect"],
  connecting: ["Connecting", "Opening a secure channel"],
  initializing: ["Starting up", "Warming up systems"],
  listening: ["Listening", "Go ahead, I'm listening"],
  thinking: ["Thinking", "Working on it"],
  speaking: ["Speaking", "Talk any time to interrupt"],
  reconnecting: ["Reconnecting", "Lost JARVIS for a moment, hold on"],
};
const PAUSED: Record<Exclude<Paused, "">, string> = {
  recovering: "Getting the mic back",
  retry: "Tap the core to turn the mic back on",
  resuming: "Reconnecting to JARVIS",
};

export const agentState = signal<AgentState>("idle");  // body[data-state]
export const link = signal<Link>("off");  // body[data-link]: the header's status pill
export const connected = signal(false);  // session exists, live or connecting
export const live = signal(false);  // body[data-live]: connected with audio flowing
export const muted = signal(false);  // body[data-muted]: user's mute choice, while connected
export const joinMuted = signal(false);  // pre-session Mute: next session starts with the mic off
// body[data-speaker="off"]: text-only replies. Persisted per device so a session starts silent.
const savedSpeakerOff = () => storage.get(SPEAKER_KEY) === "off";
export const speakerOff = signal(savedSpeakerOff());
export const canSilence = signal(true);  // false in app builds without the speaker switch
export const phoneMuted = signal(false);  // iOS muted the mic on an unreported earphone press
export const micPaused = signal<Exclude<Paused, "resuming">>("");  // micGuard state
export const resuming = signal(false);  // auto-reconnecting a lost session
export const paused = computed<Paused>(() => micPaused.value || (resuming.value ? "resuming" : ""));  // body[data-paused]
export const loginShown = signal(false);  // body[data-view="login"]
export const passcode = signal(storage.get(PASSCODE_KEY));
export const audioBlocked = signal(false);  // autoplay blocked until a tap
export const quality = signal<string | null>(null);  // LiveKit connection quality
export const micMode = signal<MicMode | null>(null);  // iOS app Mic Mode, during a session
export const startedAt = signal(0);  // live session start, ms
export const now = signal(Date.now());  // ticks every second
export const error = signal("");
export const notice = signal("");
export const focus = signal<Focus | null>(null);
export const reading = signal(false);  // transcript scrolled back: the core shrinks to make room
// Core and transcript side by side: body[data-layout="split"]
const SIDE_BY_SIDE = matchMedia("(min-width: 960px) and (min-height: 560px)");
export const sideBySide = signal(SIDE_BY_SIDE.matches);
SIDE_BY_SIDE.addEventListener("change", () => { sideBySide.value = SIDE_BY_SIDE.matches; });
export const loginPrompt = signal({ shake: false });  // new object per prompt, so effects re-run

// Push: server VAPID key, this device's subscription, user deferral. In the iOS app appNotify holds the iOS
// permission instead ("" elsewhere).
export const pushKey = signal("");
export const pushSub = signal<PushSubscription | null>(null);
export const appNotify = signal<Permission | "">("");
export const pushOn = computed(() => !!pushSub.value || appNotify.value === "granted");
export const pushLater = signal(storage.get(PUSH_KEY) === "later");

export const readout = computed(() => {
  const s = agentState.value, why = paused.value ? PAUSED[paused.value] : "";
  let [label, hint] = STATES[s] || STATES.idle;
  if (why && s !== "reconnecting") [label, hint] = ["Mic paused", why];
  else if (connected.value && muted.value && s !== "connecting" && s !== "reconnecting") {
    [label, hint] = ["Muted", phoneMuted.value ? "Press the earphone button to unmute" : "Tap the core to unmute"];
  } else if (speakerOff.value && s === "speaking") [label, hint] = ["Replying", "Speaker off: the reply is in the transcript"];
  else if (speakerOff.value && s === "listening") hint = "Speaker off: replies come as text";
  if (!connected.value && loginShown.value) hint = "Passcode required";
  return { label, hint };
});

export const coreLabel = computed(() => !connected.value ? "Connect to JARVIS"
  : paused.value ? "Turn the microphone back on" : muted.value ? "Unmute microphone" : "Mute microphone");

const two = (n: number) => String(n).padStart(2, "0");
export const linkText = computed(() => {
  if (link.value === "on" && startedAt.value) {
    const s = Math.max(0, Math.floor((now.value - startedAt.value) / 1000));
    return `Live ${two(Math.floor(s / 60))}:${two(s % 60)}`;
  }
  if (link.value === "linking") return agentState.value === "reconnecting" ? "Reconnecting" : "Linking";
  return "Offline";
});
setInterval(() => { now.value = Date.now(); }, 1000);

export function setState(state: AgentState): void {
  agentState.value = connected.value && state === "idle" ? "listening" : state;  // idle while connected = listening
}
export function setLink(value: Link): void { link.value = value; now.value = Date.now(); }
export function showError(msg?: string): void { error.value = msg || ""; }

export function setPasscode(code: string | null): void {
  storage.set(PASSCODE_KEY, code);
  passcode.value = code;
}

export function showLogin(msg?: string): void {
  loginShown.value = true;
  showError(msg);
  loginPrompt.value = { shake: !!msg };
}
export function hideLogin(): void { loginShown.value = false; }

// save: false when a phone call turned it off, which isn't the user's choice for the next session.
export function setSpeakerOff(off: boolean, save = true): void {
  speakerOff.value = off;
  if (save) storage.set(SPEAKER_KEY, off ? "off" : null);
}
export function resetSpeaker(): void { speakerOff.value = savedSpeakerOff(); }  // back to the user's choice

let noticeTimer: ReturnType<typeof setTimeout> | undefined;
export function showNotice(msg?: string): void {
  notice.value = msg || "";
  clearTimeout(noticeTimer);
  if (msg) noticeTimer = setTimeout(() => showNotice(""), 12000);
}
