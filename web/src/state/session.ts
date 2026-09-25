// The LiveKit session: connecting with the passcode, the mic and its mute, the speaker switch, getting the mic back
// after iOS takes it, and bringing a lost session back by itself: after JARVIS restarts, the network drops, or iOS
// cuts it in the background.
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
  afterCall, agentState, audioBlocked, canSilence, connected, hideLogin, joinMuted, live, loginShown, micMode, micPaused, muted, passcode,
  paused, phoneMuted, quality, resuming, setLink, setPasscode, setSpeakerOff, setState, showError, showLogin, speakerOff, startedAt,
} from "./store";
import { addDivider, addLine, hasSpoken, resetSpoken } from "./transcript";

let room: Room | null = null;
let endNote: string | null = null;  // why the session ended, if the page or JARVIS ended it
let wantMuted = false;  // the user's mute choice, which the mic follows once it can
let mic: Tap | null = null, agent: Tap | null = null;  // analysers for the reactor, and the mic for silence
let heardTrack: MediaStreamTrack | null = null;  // the mic track we follow: on iOS an AirPods press mutes or unmutes it directly
let player: HTMLAudioElement | null = null;  // plays JARVIS
let keys: MediaKeys;  // set by initSession, once the page's <audio> elements exist
let agentReady = false;  // the agent's session has started, so a message on lk.chat reaches it
const outbox: string[] = [];  // typed messages waiting for the agent
let connecting = false;  // connect() is opening a Room
let agentWait: ReturnType<typeof setTimeout> | undefined;  // awaitAgent's timer
let shortDrops = 0;  // sessions in a row the agent left soon after joining: a crash, not a restart, if it keeps on
const AGENT_WAIT_MS = 15000;  // a room the agent hasn't joined by now never gets one (it was made while JARVIS restarted)
const STEADY_MS = 20000;  // an agent that stayed this long wasn't crashing
const MAX_SHORT_DROPS = 3;
const SPEAKER = "jarvis.speaker";  // our participant attribute: "off" and the agent makes no speech (jarvis.voice.quiet)

// In the iOS and Android apps the JarvisAudio plugin runs the session natively (lib/native.ts), so the mic
// keeps going in the background and the earphone button mutes it: no Room here, the plugin's events drive the page.
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
    ended(msg) { endSession(msg ?? ""); },
    unauthorized(msg) { endSession(""); setPasscode(null); showLogin(msg); },
    setMicMode(m) { micMode.value = m; },
    setSpeakerOff,
    setAfterCall(was) { afterCall.value = was; },
  });
})();
if (native && !native.canSilence) canSilence.value = false;  // an app build from before the speaker switch
const nativeTaps = native && {  // the reactor reads the plugin's voice levels like analysers
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

// For keys.heard: true if the mic gave digital silence just now, false for any sound, null if it can't tell
// (no copy to listen to, audio stopped, Safari itself muted the mic, or JARVIS talking over it).
function micSilent(): boolean | null {
  if (paused.peek()) return null;  // the mic is being brought back
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

// --- Mute: the on-screen button, the core, M, and earphone presses all land in setMuted ---
const guard: MicGuard = micGuard({
  room: () => (live.peek() ? room : null), wantMuted: () => wantMuted,
  audioContext, source: Track.Source.Microphone,
  onPaused(s) {
    if (s) trace(`mic paused: ${s}`);
    if (s === "recovering") mic = untap(mic);  // let go of the copy before LiveKit asks iOS for a fresh mic
    micPaused.value = s;
    if (!s) attachMic();
    if (s === "retry" && guard.error?.name === "NotAllowedError") showError(micError(guard.error).message);
    syncMute();
  },
});

function syncMute() {  // the screen and the earphones follow the user's mute choice
  const m = inSession() && wantMuted;
  muted.value = m;
  keys.sync({ live: inSession() && live.peek(), muted: m, paused: !!paused.peek() });
  phoneMuted.value = keys.phoneMuted;
}

async function setMuted(m: boolean): Promise<unknown> {
  afterCall.value = null;  // the user has chosen
  if (native) {
    if (!native.connected()) return;
    wantMuted = m; syncMute();
    return native.setMuted(m);
  }
  const r = room;
  if (!r) return;
  wantMuted = m; syncMute();
  if (!live.peek()) return;  // still connecting: connect opens the mic, or not, as this says
  if (!m && paused.peek()) return guard.recover();  // unmuting while the mic is out: bring it back
  try { await r.localParticipant.setMicrophoneEnabled(!m); }
  catch (e) {
    if (room !== r) return;
    if (m || (e as { name?: string })?.name === "NotAllowedError") { wantMuted = !m; showError(micError(e).message); }
    else return guard.recover();  // e.g. iOS not ready to hand the mic back yet: retry
  }
  if (room !== r) return;
  if (!m) attachMic();
  guard.check(); syncMute();
}

// Before a session the button says how the next one starts, so nothing is heard from its first moment.
export function toggleMute() {
  if (inSession()) return setMuted(!wantMuted);
  joinMuted.value = !joinMuted.peek();
  if (connecting) wantMuted = joinMuted.peek();  // fetching the token: connect opens the mic, or not, as this says
  if (resume) resume.muted = joinMuted.peek();
}
export function pressMute() { keys.enable(); return toggleMute(); }

// --- Speaker off: JARVIS replies only in the transcript, for a meeting, a crowd or a call ---
// The agent then makes no speech at all, told by our SPEAKER attribute (set in the token, so even the greeting is
// silent); muting the player as well silences a reply already being spoken.
function setSpeaker(off: boolean) {
  setSpeakerOff(off);
  afterCall.value = null;
  if (player) player.muted = off;
  if (native) return native.setSpeakerOff(off);
  if (room && live.peek()) tellSpeaker(room);  // connecting: connect tells it once in
}
export function toggleSpeaker() { return setSpeaker(!speakerOff.peek()); }

const speakerWord = () => (speakerOff.peek() ? "off" : "on");
function tellSpeaker(r: Room) {
  const want = speakerWord();
  if (r.localParticipant.attributes[SPEAKER] === want) return;
  r.localParticipant.setAttributes({ [SPEAKER]: want })
    .then(() => { if (room === r && speakerWord() !== want) tellSpeaker(r); })  // switched again meanwhile: catch up
    .catch((e) => { if (room === r) showError(`Couldn't change the speaker: ${(e as Error).message || e}`); });
}

// The iPhone app's offer after a phone call, which turned the mic and speaker off: put them back, or leave them.
export function restoreAfterCall(restore: boolean) { native?.restoreAfterCall(restore); }

function micChanged(r: Room) {  // LiveKit muted or unmuted the mic: ours, or its own after the mic ended
  if (room !== r || !live.peek()) return;
  attachMic();
  if (!guard.micDown()) return;
  if (document.hidden) guard.check(); else guard.recover({ settle: 500 });
}

// --- Bringing a lost session back: JARVIS restarting, the network dropping, iOS cutting the mic in the background ---
// Tries again with a growing pause (lib/retry.ts) for as long as the page is open, saying "Reconnecting" meanwhile,
// and at once when the page comes back on screen or online. A phone waits until it's on screen: iOS won't give a page
// in the background the mic.
let resume: { muted: boolean } | null = null;  // the lost session, to bring back as it was: muted or not
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
  if (build.reload(resume)) return;  // a newer build is served: reload into it, which brings the session back
  connect({ muted: resume.muted });
}
function resumeSoon() { retry.cancel(); resumeNow(); }  // try now, and quickly again if that fails
function stopResuming() {  // the user ended it, or it can't come back without them
  resume = null; retry.cancel();
  if (!resuming.peek()) return;
  resuming.value = false;
  if (!room) { setLink("off"); setState("idle"); }
  syncMute();
}
// A room the agent doesn't join: dispatched while JARVIS was restarting, it never will be. Try a fresh one.
function awaitAgent(r: Room) {
  clearTimeout(agentWait);
  agentWait = setTimeout(() => {
    if (room !== r || [...r.remoteParticipants.values()].some((p) => p.isAgent)) return;
    trace("no agent joined: trying a fresh room");
    r.disconnect();  // lost, so Disconnected brings it back
  }, AGENT_WAIT_MS);
}
function returned() {
  native?.adopt();  // the app's session carried on while the page was paused: show it as it is now
  if (inSession()) keepAwake(true);
  resumeAudio();
  wakeReactor();
  if (resume) resumeSoon();
  else if (room && live.peek()) guard.recover({ settle: 600 });  // LiveKit gets the first go
}

// Typed messages go to the agent on LiveKit's chat topic: it answers them like speech, spoken and captioned.
function flush(r: Room) {
  while (agentReady && room === r && outbox.length) {
    r.localParticipant.sendText(outbox.shift()!, { topic: "lk.chat" })
      .catch((e) => { if (room === r) showError(`Couldn't send that: ${(e as Error).message || e}`); });
  }
}

// The agent says what it's doing in its lk.agent.state attribute, from joining until it leaves.
function agentChanged(r: Room, state: string | undefined) {
  if (!state) return;
  setState(state as AgentState);
  if (state !== "initializing" && !agentReady) { agentReady = true; flush(r); }
}

// Back to no session: after one ends, or fails to start. keep: it's coming back, so typed messages still wait for it.
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
  if (!keep) build.reload();  // over, and a newer build came out during it: load that now
}

// startMuted: start without opening the mic (the Mute button before a session, by default). The speaker switch is the
// one on screen. While a lost session is coming back (resume), it says "Reconnecting" throughout and, if it fails,
// tries again shortly; so does a first connect that can't reach JARVIS. Only a wrong passcode or the mic being
// refused stops it.
export async function connect({ muted: startMuted = joinMuted.peek() } = {}): Promise<void> {
  if (!passcode.peek()) { stopResuming(); return showLogin(); }
  if (connecting || agentState.peek() === "connecting") return;
  const again = !!resume;
  showError(""); endNote = null; resetSpoken(); agentReady = false;
  if (native) return native.connect({ passcode: passcode.peek()!, muted: startMuted, speakerOff: speakerOff.peek() });
  if (!again) shortDrops = 0;  // the user asked: start counting crashes over
  setLink("linking"); setState(again ? "reconnecting" : "connecting");
  connecting = true;
  wantMuted = startMuted;  // a press while it connects changes it
  let r: Room | null = null, micFailed = false;
  try {
    const data = await postAuthedJson("/api/token", { device: DEVICE, speaker: speakerWord() });
    if (build.served(data.build, { muted: wantMuted })) return;  // reloading into the newer build, which reconnects
    const lr = r = new Room({ audioCaptureDefaults: { echoCancellation: true, noiseSuppression: true, autoGainControl: true } });
    let agentAt = 0, agentLeft = false;  // when the agent joined; whether its leaving ended the session
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
    lr.on(RoomEvent.ParticipantDisconnected, (p) => {  // JARVIS restarting, most likely: a fresh room gets a fresh agent
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
        resumeLater(m);  // as it was: muted or not
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
    for (const p of lr.remoteParticipants.values()) joined(p);  // there before us
    awaitAgent(lr);
    await lr.startAudio();
    if (room !== lr) return;  // ended meanwhile
    audioBlocked.value = !lr.canPlaybackAudio;
    let micNote = "";
    if (!wantMuted) {
      try { await lr.localParticipant.setMicrophoneEnabled(true); }
      catch (e) {
        if (room !== lr) return;
        // Coming back by itself, with no tap to ask in, the browser may refuse the mic: carry on muted, a tap away
        if (!again) { micFailed = true; throw micError(e); }
        wantMuted = true;
        micNote = "Reconnected with the mic off. Tap the core to turn it back on.";
      }
    }
    if (room !== lr) return;
    attachMic();
    live.value = true; startedAt.value = Date.now(); joinMuted.value = false;  // the session's own mute holds it now
    tellSpeaker(lr);  // a switch while it connected
    micPaused.value = ""; resume = null; retry.reset(); resuming.value = false;
    setLink("on"); keepAwake(true); syncMute();
    if (micNote) showError(micNote);
    trace(`session ${again ? "back" : "started"}${wantMuted ? " muted" : ""}${speakerOff.peek() ? ", speaker off" : ""}: ${navigator.userAgent}${standalone ? ", Home Screen app" : ""}`);
    if (["connecting", "reconnecting"].includes(agentState.peek())) setState("initializing");
  } catch (e) {
    if (r && room !== r) return;  // ended meanwhile: Disconnected or hangup has seen to it
    if (r) { setRoom(null); r.disconnect(); }
    if (e instanceof Unauthorized) { stopResuming(); dropLink(); setPasscode(null); return showLogin(e.message); }
    const msg = (e as Error).message || String(e);
    if (micFailed) { stopResuming(); return dropLink(msg); }
    trace(`couldn't connect: ${msg}`);
    dropLink(undefined, true);
    resumeLater(wantMuted);  // JARVIS restarting, or the network: try again shortly
  } finally {
    connecting = false;
  }
}

export function hangup() {
  stopResuming();
  if (native) { native.hangup().catch(() => {}); return; }
  if (room) { endNote = ""; room.disconnect(); }
}

// A tap on the core, the Connect pill, Space, or "Talk to JARVIS about it".
export function activate() {
  audioCtx();
  if (!loginShown.peek()) keys.enable();  // inside the tap, so iOS lets it play later
  if (native?.connected()) return paused.peek() ? setMuted(false) : toggleMute();  // "Mic paused": ask for it back
  if (room && paused.peek()) return guard.recover();
  if (room) return toggleMute();
  if (loginShown.peek()) return document.getElementById("passcode")?.focus();
  if (resume) return resumeSoon();  // reconnecting: try now
  connect();
}

// The passcode card was submitted: code is already trimmed and non-empty.
export function unlock(code: string) {
  audioCtx(); keys.enable();
  setPasscode(code); hideLogin();
  connect();
}

export function unlockAudio() { room?.startAudio(); }

export function showMicModes() { native?.showMicModes(); }  // the iOS app: Apple's Mic Mode picker

// A typed message, from the composer. Without a session it starts one muted, so no mic is needed, and the
// message waits until the agent is listening. False if it couldn't be taken (no passcode yet). shown: how the
// transcript shows it, if not as sent (marked as coming with a photo).
export function sendText(text: string, shown = text): boolean {
  if (!passcode.peek()) { showLogin(); return false; }
  audioCtx(); keys.enable();  // inside the tap or Return, so iOS lets JARVIS's reply play
  if (native) {  // the plugin starts a muted session if there's none, and holds the message until the agent listens
    addLine("you").value = shown;
    native.sendText(text, passcode.peek()!, speakerOff.peek()).catch((e) => showError(`Couldn't send that: ${(e as Error).message || e}`));
    return true;
  }
  if (!room) {
    if (resume) resumeSoon();
    else connect({ muted: true });  // clears the transcript's spoken flag, so the line below comes after
  }
  addLine("you").value = shown;
  outbox.push(text);
  if (room) flush(room);
  return true;
}

// Earphone controls only while on screen: in the background they'd show up as Now Playing controls.
export function visibilityChanged() {
  if (room) trace(document.hidden ? "page hidden" : "page shown");
  keys.setVisible(!document.hidden);
  if (!document.hidden) returned();
}
export function pageHidden() { keys.setVisible(false); }
export function online() { if (resume) resumeSoon(); }  // the network's back: don't wait out the pause
export function pageShown(e: PageTransitionEvent) {
  keys.setVisible(!document.hidden);
  if (e.persisted) returned();  // back from the back-forward cache
}

// Called once the page has rendered, with its two <audio> elements: one plays JARVIS, the other is the
// near-silent loop that lets a phone hand the page earphone presses.
export function initSession(els: { player: HTMLAudioElement; keepalive: HTMLAudioElement }) {
  player = els.player;
  keys = mediaKeys({
    // In the app the plugin owns the earphone button and Now Playing, so the page stays out of them.
    audio: mobile && !native ? els.keepalive : null, session: navigator.mediaSession, Metadata: window.MediaMetadata,
    setMuted, isMuted: () => wantMuted, log: trace,
    cue: (m) => { if (!speakerOff.peek()) chime(audioContext(), m); },  // speaker off: not a sound
    artwork: [{ src: `${import.meta.env.BASE_URL}icon-512.png`, sizes: "512x512", type: "image/png" }],
  });
  // iOS may mute the mic on an earphone press without Safari telling the page: listen for the silence.
  if (mobile && !native) setInterval(() => { if (room) keys.heard(micSilent()); }, 200);
  native?.adopt();  // a session the app kept running while this page reloaded
  // A newer build reloads the page only between sessions, or as a lost one comes back (lib/build.ts)
  build.follow({ busy: () => !!room, resume: () => resume });
  const was = build.takeResume();  // the session this page had before it reloaded into a newer build
  if (was && !native && passcode.peek()) resumeLater(was.muted);
}
