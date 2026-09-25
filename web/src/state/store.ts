// The page's state, as signals. App.tsx mirrors the ones the stylesheet keys on to body[data-*]; the
// session (session.ts) and push (push.ts) write them, and components read them.
import { computed, signal } from "@preact/signals";
import type { AgentState, Focus, Link, Paused } from "../types";
import type { BeforeCall, MicMode, Permission } from "../lib/native";
import { CAPTIONS_KEY, PASSCODE_KEY, PUSH_KEY, SPEAKER_KEY, storage } from "../lib/storage";

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
export const link = signal<Link>("off");  // body[data-link]: the header's LED
export const connected = signal(false);  // a LiveKit room exists, live or still connecting
export const live = signal(false);  // body[data-live]: connected, with audio going
export const muted = signal(false);  // body[data-muted]: the user's mute choice, while connected
export const joinMuted = signal(false);  // the Mute button before a session: the next one starts with the mic off
// body[data-speaker="off"]: JARVIS replies only in the transcript, making no sound at all. Kept on this device, so a
// session started in a meeting is silent from its first word.
export const speakerOff = signal(storage.get(SPEAKER_KEY) === "off");
export const canSilence = signal(true);  // false in an app build whose plugin has no speaker switch
export const afterCall = signal<BeforeCall | null>(null);  // the iPhone app: a call ended, offer to put these back
export const phoneMuted = signal(false);  // iOS muted the mic on an earphone press, without Safari saying
export const micPaused = signal<Exclude<Paused, "resuming">>("");  // micGuard getting the mic back
export const resuming = signal(false);  // a lost session is coming back (a restart, the network, iOS's background)
export const paused = computed<Paused>(() => micPaused.value || (resuming.value ? "resuming" : ""));  // body[data-paused]
export const loginShown = signal(false);  // body[data-view="login"]
export const captions = signal(storage.get(CAPTIONS_KEY) !== "off");  // body[data-captions="off"] when not
export const passcode = signal(storage.get(PASSCODE_KEY));
export const audioBlocked = signal(false);  // the browser wants a tap before it plays JARVIS
export const quality = signal<string | null>(null);  // LiveKit's connection quality, once it has one
export const micMode = signal<MicMode | null>(null);  // the iOS app's Mic Mode, during a session there
export const startedAt = signal(0);  // when the live session began
export const now = signal(Date.now());  // the clock, ticked every second
export const error = signal("");
export const notice = signal("");
export const focus = signal<Focus | null>(null);
export const reading = signal(false);  // scrolled back up the transcript: the core shrinks to give it the room
// Room for the core and the transcript side by side: body[data-layout="split"] (App.tsx, styles.css)
const SIDE_BY_SIDE = matchMedia("(min-width: 960px) and (min-height: 560px)");
export const sideBySide = signal(SIDE_BY_SIDE.matches);
SIDE_BY_SIDE.addEventListener("change", () => { sideBySide.value = SIDE_BY_SIDE.matches; });
export const loginPrompt = signal({ shake: false });  // a new object each time the passcode card asks again

// Push notifications (push.ts): the Mac has a key to subscribe with, this device is subscribed, or the
// user put it off. In the iPhone app, appNotify is what iOS says instead ("" anywhere else).
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
export const clock = computed(() => {
  const d = new Date(now.value);
  return `${two(d.getHours())}:${two(d.getMinutes())}`;
});
export const linkText = computed(() => {
  if (link.value === "on" && startedAt.value) {
    const s = Math.max(0, Math.floor((now.value - startedAt.value) / 1000));
    return `Live ${two(Math.floor(s / 60))}:${two(s % 60)}`;
  }
  return link.value === "linking" ? "Linking" : "Offline";
});
setInterval(() => { now.value = Date.now(); }, 1000);

export function setState(state: AgentState): void {
  agentState.value = connected.value && state === "idle" ? "listening" : state;  // a live agent that's idle is waiting for you
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

export function setSpeakerOff(off: boolean): void {
  speakerOff.value = off;
  storage.set(SPEAKER_KEY, off ? "off" : null);
}

export function setCaptions(on: boolean): void {
  captions.value = on;
  storage.set(CAPTIONS_KEY, on ? null : "off");
}
export const toggleCaptions = () => setCaptions(!captions.value);

let noticeTimer: ReturnType<typeof setTimeout> | undefined;
export function showNotice(msg?: string): void {
  notice.value = msg || "";
  clearTimeout(noticeTimer);
  if (msg) noticeTimer = setTimeout(() => showNotice(""), 12000);
}
