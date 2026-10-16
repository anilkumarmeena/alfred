// LiveKit session: connect, mic and mute, speaker switch, mic recovery after iOS takes it, and auto-reconnect
// (JARVIS restart, network drop, iOS background cut).
import { Room, RoomEvent, Track, type Participant } from "livekit-client";
import type { AgentState } from "../types";
import { audioContext, audioCtx, resumeAudio, tap, untap, type Tap } from "../lib/audio";
import { postAuthedJson, Unauthorized } from "../lib/api";
import { build } from "../lib/build";
import { retrier } from "../lib/retry";
import { DEVICE, mobile, standalone } from "../lib/device";
import { chime, mediaKeys, micGuard, type MediaKeys, type MicGuard } from "../lib/mic";
import { bindNative, levelTap, nativeAudio, type NativeSession } from "../lib/native";
import { wakeReactor, type ReactorInput } from "../lib/reactor";
import { trace } from "../lib/trace";
import { keepAwake } from "../lib/wake";
import {
  agentState, audioBlocked, canSilence, connected, hideLogin, joinMuted, live, loginShown, micMode, micPaused, muted, passcode, paused,
  phoneMuted, quality, resetSpeaker, resuming, setLink, setPasscode, setSpeakerOff, setState, showError, showLogin, speakerOff, startedAt,
} from "./store";
import { addDivider, addLine, hasSpoken, resetSpoken } from "./transcript";

let room: Room | null = null;
let endNote: string | null = null;  // set when the page or JARVIS ended the session on purpose
let wantMuted = false;  // user's mute choice; the mic follows once it can
let mic: Tap | null = null, agent: Tap | null = null;  // reactor analysers; mic also for silence detection
let heardTrack: MediaStreamTrack | null = null;  // watched mic track: on iOS an AirPods press (un)mutes it directly
let player: HTMLAudioElement | null = null;  // plays JARVIS
let keys: MediaKeys;  // set by initSession
let agentReady = false;  // agent session started, so lk.chat messages reach it
const outbox: string[] = [];  // typed messages queued for the agent
let connecting = false;
let agentWait: ReturnType<typeof setTimeout> | undefined;
let shortDrops = 0;  // consecutive sessions the agent left soon after joining; repeated = crash loop
const AGENT_WAIT_MS = 15000;  // no agent by now = room was dispatched mid-restart and never will get one
const STEADY_MS = 20000;  // agent stayed this long = not a crash
const MAX_SHORT_DROPS = 3;
const SPEAKER = "jarvis.speaker";  // participant attribute; "off" = agent makes no speech

// In the native apps the JarvisAudio plugin owns the session (mic survives background, earphone button mutes).
// No Room here: plugin events drive the page.
const native: NativeSession | null = (() => {
  const plugin = nativeAudio();
  return plugin && bindNative(plugin, {
    setLink, setState,
    setConnected(v) { connected.value = v; },
    setLive(v, at) {
      live.value = v;
      if (v) { startedAt.value = at ?? Date.now(); micPaused.value = ""; joinMuted.value = false; keepAwake(true); trace(`native session started: ${navigator.userAgent}`); }
      syncMute();
    },
    setMuted(v) { wantMuted = v; syncMute(); },
    setPaused(v) { micPaused.value = v; syncMute(); },
    setQuality(q) { quality.value = q; },
    line(id, who, text) { addLine(who, id, text); },
    ended(msg) { endSession(msg ?? ""); resetSpeaker(); },  // a call may have turned the speaker off
    unauthorized(msg) { endSession(""); setPasscode(null); showLogin(msg); },
    setMicMode(m) { micMode.value = m; },
    setSpeakerOff(off, byCall) { setSpeakerOff(off, !byCall); },
  });
})();
if (native && !native.canSilence) canSilence.value = false;  // app build without the speaker switch
const nativeTaps = native && {  // plugin voice levels, shaped like analysers for the reactor
  agent: levelTap(() => native.levels().agent) as unknown as Tap,
  mic: levelTap(() => native.levels().mic) as unknown as Tap,
};
const inSession = () => !!room || !!native?.connected();

function setRoom(r: Room | null) { room = r; connected.value = !!r; }

const micTrack = () => room?.localParticipant.getTrackPublication(Track.Source.Microphone)?.track?.mediaStreamTrack;

export const reactorInput = (): ReactorInput => ({
  state: agentState.peek(), connected: connected.peek(), muted: muted.peek(), paused: !!paused.peek(),
  agent: nativeTaps ? nativeTaps.agent : agent, mic: nativeTaps ? nativeTaps.mic : mic,
});

function attachMic() {
  const t = micTrack();
  if (t && t !== mic?.track) { untap(mic); mic = tap(t, true); }
  if (t && t !== heardTrack) {
    heardTrack = t;
    const changed = () => { if (heardTrack === t) keys.micMuted(() => t.muted); };
    t.addEventListener("mute", changed); t.addEventListener("unmute", changed);
  }
}

// For keys.heard: true = digital silence, false = any sound, null = can't tell (no copy, audio stopped,
// Safari muted the mic, or JARVIS speaking).
function micSilent(): boolean | null {
  if (paused.peek()) return null;  // mic is being recovered
  const t = micTrack();
  if (t && t !== mic?.track) attachMic();
  const m = mic;
  if (!m || !m.copy || m.track !== t || m.copy.readyState !== "live" || m.copy.muted || audioContext()?.state !== "running") return null;
  if (Date.now() - m.at < 1000 || agentState.peek() === "speaking") return null;
  m.an.getFloatTimeDomainData(m.wave);
  return m.wave.every((v) => v === 0);
}

function micError(e: unknown): Error {
  const name = (e as { name?: string } | null)?.name;
  if (name === "NotAllowedError") return new Error("Microphone access is blocked. Allow it for this site, then try again.");
  if (name === "NotFoundError") return new Error("No microphone found.");
  return e instanceof Error ? e : new Error(String(e));
}

// --- Mute: button, core, M key and earphone presses all land in setMuted ---
const guard: MicGuard = micGuard({
  room: () => (live.peek() ? room : null), wantMuted: () => wantMuted,
  audioContext, source: Track.Source.Microphone,
  onPaused(s) {
    if (s) trace(`mic paused: ${s}`);
    if (s === "recovering") mic = untap(mic);  // release the copy before LiveKit asks iOS for a fresh mic
    micPaused.value = s;
    if (!s) attachMic();
    if (s === "retry" && guard.error?.name === "NotAllowedError") showError(micError(guard.error).message);
    syncMute();
  },
});

function syncMute() {  // screen and earphones follow wantMuted
  const m = inSession() && wantMuted;
  muted.value = m;
  keys.sync({ live: inSession() && live.peek(), muted: m, paused: !!paused.peek() });
  phoneMuted.value = keys.phoneMuted;
}

async function setMuted(m: boolean): Promise<unknown> {
  if (native) {
    if (!native.connected()) return;
    wantMuted = m; syncMute();
    return native.setMuted(m);
  }
  const r = room;
  if (!r) return;
  wantMuted = m; syncMute();
  if (!live.peek()) return;  // still connecting: connect() honours wantMuted
  if (!m && paused.peek()) return guard.recover();
  try { await r.localParticipant.setMicrophoneEnabled(!m); }
  catch (e) {
    if (room !== r) return;
    if (m || (e as { name?: string })?.name === "NotAllowedError") { wantMuted = !m; showError(micError(e).message); }
    else return guard.recover();  // e.g. iOS not ready to hand the mic back yet
  }
  if (room !== r) return;
  if (!m) attachMic();
  guard.check(); syncMute();
}

// Before a session, toggles how the next one starts, so a muted start never hears anything.
export function toggleMute() {
  if (inSession()) return setMuted(!wantMuted);
  joinMuted.value = !joinMuted.peek();
  if (connecting) wantMuted = joinMuted.peek();  // mid-connect: connect() honours wantMuted
  if (resume) resume.muted = joinMuted.peek();
}
export function pressMute() { keys.enable(); return toggleMute(); }

// --- Speaker off: JARVIS replies only in the transcript ---
// SPEAKER attribute (also in the token, so the greeting is silent) stops agent speech; muting the player cuts a reply
// already playing.
function setSpeaker(off: boolean) {
  setSpeakerOff(off);
  if (player) player.muted = off;
  if (native) return native.setSpeakerOff(off);
  if (room && live.peek()) tellSpeaker(room);  // else connect() sends it once live
}
export function toggleSpeaker() { return setSpeaker(!speakerOff.peek()); }

const speakerWord = () => (speakerOff.peek() ? "off" : "on");
function tellSpeaker(r: Room) {
  const want = speakerWord();
  if (r.localParticipant.attributes[SPEAKER] === want) return;
  r.localParticipant.setAttributes({ [SPEAKER]: want })
    .then(() => { if (room === r && speakerWord() !== want) tellSpeaker(r); })  // toggled again meanwhile
    .catch((e) => { if (room === r) showError(`Couldn't change the speaker: ${(e as Error).message || e}`); });
}

function micChanged(r: Room) {  // LiveKit (un)muted the mic, by request or because the track ended
  if (room !== r || !live.peek()) return;
  attachMic();
  if (!guard.micDown()) return;
  if (document.hidden) guard.check(); else guard.recover({ settle: 500 });
}

// --- Auto-reconnect ---
// Retries with backoff while the page is open, and immediately on return to screen or network. Phones wait until
// visible: iOS won't give a background page the mic.
let resume: { muted: boolean } | null = null;  // lost session to restore, with its mute state
const retry = retrier(() => resumeNow());
const offScreen = () => mobile && document.hidden;

function resumeLater(m: boolean) {
  resume = { muted: m }; joinMuted.value = m;
  showError(""); resuming.value = true;
  setLink("linking"); setState("reconnecting");
  if (!offScreen()) retry.schedule();
}
function resumeNow() {
  if (!resume || room || connecting || offScreen()) return;
  if (build.reload(resume)) return;  // newer build: reload into it, which resumes the session
  connect({ muted: resume.muted });
}
function resumeSoon() { retry.cancel(); resumeNow(); }  // try now; backoff restarts if it fails
function stopResuming() {  // user ended it, or it needs user action
  resume = null; retry.cancel();
  if (!resuming.peek()) return;
  resuming.value = false;
  if (!room) { setLink("off"); setState("idle"); }
  syncMute();
}
// A room dispatched mid-restart never gets an agent: drop it for a fresh one.
function awaitAgent(r: Room) {
  clearTimeout(agentWait);
  agentWait = setTimeout(() => {
    if (room !== r || [...r.remoteParticipants.values()].some((p) => p.isAgent)) return;
    trace("no agent joined: trying a fresh room");
    r.disconnect();  // counts as lost, so Disconnected reconnects
  }, AGENT_WAIT_MS);
}
function returned() {
  native?.adopt();  // sync with the app's session, which kept running while the page was paused
  if (inSession()) keepAwake(true);
  resumeAudio();
  wakeReactor();
  if (resume) resumeSoon();
  else if (room && live.peek()) guard.recover({ settle: 600 });  // settle: let LiveKit try first
}

// Typed messages go out on lk.chat; the agent answers them like speech.
function flush(r: Room) {
  while (agentReady && room === r && outbox.length) {
    r.localParticipant.sendText(outbox.shift()!, { topic: "lk.chat" })
      .catch((e) => { if (room === r) showError(`Couldn't send that: ${(e as Error).message || e}`); });
  }
}

// The agent reports its state in the lk.agent.state attribute.
function agentChanged(r: Room, state: string | undefined) {
  if (!state) return;
  setState(state as AgentState);
  if (state !== "initializing" && !agentReady) { agentReady = true; flush(r); }
}

// Reset to no session. keep: a reconnect follows, so queued messages stay.
function dropLink(msg?: string, keep = false) {
  agentReady = false;
  if (!keep) outbox.length = 0;
  agent = untap(agent); mic = untap(mic);
  live.value = false;
  syncMute();
  setLink("off"); setState("idle");
  if (msg) showError(msg);
}

function endSession(msg: string, keep = false) {
  setRoom(null); wantMuted = false; heardTrack = null;
  clearTimeout(agentWait);
  keepAwake(false);
  micPaused.value = ""; resuming.value = false;
  guard.reset();
  if (startedAt.peek() && hasSpoken()) {
    const m = Math.round((Date.now() - startedAt.peek()) / 60000);
    addDivider(`Session ended · ${m < 1 ? "under a minute" : m + " min"}`);
  }
  startedAt.value = 0;
  audioBlocked.value = false; quality.value = null;
  dropLink(msg, keep);
  if (!keep) build.reload();  // pick up a newer build released during the session
}

// startMuted: don't open the mic (defaults to the pre-session Mute button). On failure it retries with backoff;
// only a wrong passcode or a refused mic stops it.
export async function connect({ muted: startMuted = joinMuted.peek() } = {}): Promise<void> {
  if (!passcode.peek()) { stopResuming(); return showLogin(); }
  if (connecting || agentState.peek() === "connecting") return;
  const again = !!resume;
  showError(""); endNote = null; resetSpoken(); agentReady = false;
  if (native) return native.connect({ passcode: passcode.peek()!, muted: startMuted, speakerOff: speakerOff.peek() });
  if (!again) shortDrops = 0;  // user-initiated: reset crash count
  setLink("linking"); setState(again ? "reconnecting" : "connecting");
  connecting = true;
  wantMuted = startMuted;  // a press while connecting may change it
  let r: Room | null = null, micFailed = false;
  try {
    const data = await postAuthedJson("/api/token", { device: DEVICE, speaker: speakerWord() });
    if (build.served(data.build, { muted: wantMuted })) return;  // reloading into a newer build, which reconnects
    const lr = r = new Room({ audioCaptureDefaults: { echoCancellation: true, noiseSuppression: true, autoGainControl: true } });
    let agentAt = 0, agentLeft = false;  // agent join time; whether its leaving ended the session
    const joined = (p: Participant) => {
      if (!p.isAgent) return;
      agentAt ||= Date.now();
      agentChanged(lr, p.attributes["lk.agent.state"]);
    };
    setRoom(lr); syncMute();
    lr.on(RoomEvent.TrackSubscribed, (track) => {
      if (track.kind !== Track.Kind.Audio) return;
      if (player) { track.attach(player); player.muted = speakerOff.peek(); }  // attach unmutes it
      untap(agent); agent = tap(track.mediaStreamTrack);
    });
    lr.on(RoomEvent.TrackUnsubscribed, (track) => { if (track.mediaStreamTrack === agent?.track) agent = untap(agent); });
    lr.on(RoomEvent.ParticipantAttributesChanged, (changed) => agentChanged(lr, changed["lk.agent.state"]));
    lr.on(RoomEvent.ParticipantConnected, joined);
    lr.on(RoomEvent.ParticipantDisconnected, (p) => {  // likely a JARVIS restart: a fresh room gets a fresh agent
      if (p.isAgent && room === lr) { agentLeft = true; lr.disconnect(); }
    });
    lr.on(RoomEvent.Reconnecting, () => { setLink("linking"); setState("reconnecting"); });
    lr.on(RoomEvent.Reconnected, () => { setLink("on"); setState("listening"); awaitAgent(lr); });
    lr.on(RoomEvent.AudioPlaybackStatusChanged, () => { audioBlocked.value = !lr.canPlaybackAudio; });
    lr.on(RoomEvent.ConnectionQualityChanged, (q, p) => { if (p === lr.localParticipant) quality.value = q; });
    for (const ev of [RoomEvent.TrackMuted, RoomEvent.TrackUnmuted] as const) {
      lr.on(ev, (_, p) => { if (p === lr.localParticipant) micChanged(lr); });
    }
    lr.on(RoomEvent.Disconnected, () => {
      if (room !== lr) return;
      const lost = endNote == null, m = wantMuted;
      if (lost && agentLeft) shortDrops = agentAt && Date.now() - agentAt < STEADY_MS ? shortDrops + 1 : 0;
      if (lost && shortDrops < MAX_SHORT_DROPS) {
        trace(agentLeft ? "agent left: reconnecting" : "connection lost: reconnecting");
        endSession("", true);
        resumeLater(m);
      } else {
        stopResuming();
        endSession(endNote ?? "JARVIS keeps ending the session. Tap the core to reconnect.");
      }
    });
    lr.registerTextStreamHandler("lk.transcription", async (reader, participant) => {
      const who = participant.identity === lr.localParticipant.identity ? "you" : "jarvis";
      const line = addLine(who, reader.info.attributes?.["lk.segment_id"]);
      for await (const chunk of reader) line.value += chunk;
    });

    await lr.connect(data.url, data.token);
    for (const p of lr.remoteParticipants.values()) joined(p);  // already in the room
    awaitAgent(lr);
    await lr.startAudio();
    if (room !== lr) return;  // ended meanwhile
    audioBlocked.value = !lr.canPlaybackAudio;
    let micNote = "";
    if (!wantMuted) {
      try { await lr.localParticipant.setMicrophoneEnabled(true); }
      catch (e) {
        if (room !== lr) return;
        // On auto-reconnect there's no user gesture, so the browser may refuse the mic: continue muted
        if (!again) { micFailed = true; throw micError(e); }
        wantMuted = true;
        micNote = "Reconnected with the mic off. Tap the core to turn it back on.";
      }
    }
    if (room !== lr) return;
    attachMic();
    live.value = true; startedAt.value = Date.now(); joinMuted.value = false;  // session mute takes over
    tellSpeaker(lr);  // apply a toggle made while connecting
    micPaused.value = ""; resume = null; retry.reset(); resuming.value = false;
    setLink("on"); keepAwake(true); syncMute();
    if (micNote) showError(micNote);
    trace(`session ${again ? "back" : "started"}${wantMuted ? " muted" : ""}${speakerOff.peek() ? ", speaker off" : ""}: ${navigator.userAgent}${standalone ? ", Home Screen app" : ""}`);
    if (["connecting", "reconnecting"].includes(agentState.peek())) setState("initializing");
  } catch (e) {
    if (r && room !== r) return;  // ended meanwhile: Disconnected or hangup handled it
    if (r) { setRoom(null); r.disconnect(); }
    if (e instanceof Unauthorized) { stopResuming(); dropLink(); setPasscode(null); return showLogin(e.message); }
    const msg = (e as Error).message || String(e);
    if (micFailed) { stopResuming(); return dropLink(msg); }
    trace(`couldn't connect: ${msg}`);
    dropLink(undefined, true);
    resumeLater(wantMuted);  // JARVIS restart or network: retry
  } finally {
    connecting = false;
  }
}

export function hangup() {
  stopResuming();
  if (native) { native.hangup().catch(() => {}); return; }
  if (room) { endNote = ""; room.disconnect(); }
}

// Core tap, Connect pill, Space, or "Talk to JARVIS about it".
export function activate() {
  audioCtx();
  if (!loginShown.peek()) keys.enable();  // within the gesture, so iOS allows playback later
  if (native?.connected()) return paused.peek() ? setMuted(false) : toggleMute();  // paused: unmuting recovers the mic
  if (room && paused.peek()) return guard.recover();
  if (room) return toggleMute();
  if (loginShown.peek()) return document.getElementById("passcode")?.focus();
  if (resume) return resumeSoon();  // reconnecting: try now
  connect();
}

// Passcode submitted; code is trimmed and non-empty.
export function unlock(code: string) {
  audioCtx(); keys.enable();
  setPasscode(code); hideLogin();
  connect();
}

export function unlockAudio() { room?.startAudio(); }

export function showMicModes() { native?.showMicModes(); }  // iOS app: system Mic Mode picker

// Send a typed message, starting a muted session if needed; it queues until the agent listens.
// Returns false when there's no passcode yet. shown: transcript text if different (e.g. marked with a photo).
export function sendText(text: string, shown = text): boolean {
  if (!passcode.peek()) { showLogin(); return false; }
  audioCtx(); keys.enable();  // within the gesture, so iOS lets the reply play
  if (native) {  // plugin starts a muted session if needed and queues the message
    addLine("you").value = shown;
    native.sendText(text, passcode.peek()!, speakerOff.peek()).catch((e) => showError(`Couldn't send that: ${(e as Error).message || e}`));
    return true;
  }
  if (!room) {
    if (resume) resumeSoon();
    else connect({ muted: true });  // resets the spoken flag, so must run before addLine
  }
  addLine("you").value = shown;
  outbox.push(text);
  if (room) flush(room);
  return true;
}

// Earphone controls only while visible; in the background they'd appear as Now Playing.
export function visibilityChanged() {
  if (room) trace(document.hidden ? "page hidden" : "page shown");
  keys.setVisible(!document.hidden);
  if (!document.hidden) returned();
}
export function pageHidden() { keys.setVisible(false); }
export function online() { if (resume) resumeSoon(); }  // skip the backoff
export function pageShown(e: PageTransitionEvent) {
  keys.setVisible(!document.hidden);
  if (e.persisted) returned();  // restored from bfcache
}

// Called after first render. player plays JARVIS; keepalive is a near-silent loop so phones route earphone presses
// to the page.
export function initSession(els: { player: HTMLAudioElement; keepalive: HTMLAudioElement }) {
  player = els.player;
  keys = mediaKeys({
    // In the app the plugin owns the earphone button.
    audio: mobile && !native ? els.keepalive : null, session: navigator.mediaSession, Metadata: window.MediaMetadata,
    setMuted, isMuted: () => wantMuted, log: trace,
    cue: (m) => { if (!speakerOff.peek()) chime(audioContext(), m); },
    artwork: [{ src: `${import.meta.env.BASE_URL}icon-512.png`, sizes: "512x512", type: "image/png" }],
  });
  // iOS may mute the mic on an earphone press without an event: detect the silence.
  if (mobile && !native) setInterval(() => { if (room) keys.heard(micSilent()); }, 200);
  native?.adopt();  // session the app kept running across a reload
  // Newer builds reload only between sessions or during a reconnect.
  build.follow({ busy: () => !!room, resume: () => resume });
  const was = build.takeResume();  // session to resume after a build reload
  if (was && !native && passcode.peek()) resumeLater(was.muted);
}
