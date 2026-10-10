// lib/mic.ts: earphone controls and mic recovery. Run: make test (or npm test in web/)
import assert from "node:assert/strict";
import { test } from "vitest";
import { chime, mediaKeys, micGuard, silentWav, type GuardRoom, type MediaKeys } from "../src/lib/mic";

const nextTick = () => new Promise((resolve) => setImmediate(resolve));

// ── silentWav ───────────────────────────────────────────────────

test("silentWav is a valid mono 16-bit WAV of near-silence, not pure zeros", () => {
  const buf = silentWav(1, 8000), v = new DataView(buf);
  const text = (at: number, n: number) => String.fromCharCode(...new Uint8Array(buf, at, n));
  assert.equal(text(0, 4), "RIFF");
  assert.equal(text(8, 4), "WAVE");
  assert.equal(v.getUint16(22, true), 1);  // channels
  assert.equal(v.getUint32(24, true), 8000);  // sample rate
  assert.equal(v.getUint16(34, true), 16);  // bits per sample
  assert.equal(v.getUint32(40, true), 16000);
  assert.equal(buf.byteLength, 44 + 16000);
  const samples = Array.from({ length: 8000 }, (_, i) => v.getInt16(44 + i * 2, true));
  assert.ok(samples.every((s) => s >= -1 && s <= 1));
  assert.ok(samples.some((s) => s !== 0));
});

// ── chime ───────────────────────────────────────────────────────

function fakeContext(state = "running") {
  const notes: [number, number][] = [], ctx: any = {
    state, currentTime: 10, destination: {}, resumed: 0,
    resume() { ctx.resumed++; return Promise.resolve(); },
    createGain: () => ({ gain: { setValueAtTime() {}, exponentialRampToValueAtTime() {} }, connect: (d: unknown) => d }),
    createOscillator: () => {
      const osc = { frequency: { value: 0 }, connect: (g: unknown) => g, start(at: number) { notes.push([osc.frequency.value, at]); }, stop() {} };
      return osc;
    },
  };
  return { ctx, notes };
}

test("chime falls for muted and rises for listening", () => {
  const down = fakeContext(), up = fakeContext();
  chime(down.ctx, true);
  chime(up.ctx, false);
  assert.deepEqual(down.notes.map(([hz]) => hz), [880, 587]);
  assert.deepEqual(up.notes.map(([hz]) => hz), [587, 880]);
  assert.ok(down.notes[1]![1] > down.notes[0]![1]);
});

test("chime resumes a suspended context, and skips a missing or closed one", () => {
  const { ctx, notes } = fakeContext("interrupted");
  chime(ctx, true);
  assert.equal(ctx.resumed, 1);
  assert.equal(notes.length, 2);
  const closed = fakeContext("closed");
  chime(closed.ctx, true);
  chime(null, true);
  assert.equal(closed.notes.length, 0);
});

// ── mediaKeys ───────────────────────────────────────────────────

function fakeAudio(): any {
  return {
    src: "", loop: false, paused: true, plays: 0, loads: 0,
    play() { this.paused = false; this.plays++; return Promise.resolve(); },
    pause() { this.paused = true; },
    removeAttribute(name: string) { this[name] = ""; },
    load() { this.loads++; },
  };
}

function fakeSession({ toggleMic = true, micActive = true } = {}) {
  const s: any = {
    handlers: {}, playbackState: "none", metadata: null, micCalls: [],
    setActionHandler(action: string, fn: unknown) {
      if (action === "togglemicrophone" && !toggleMic) throw new TypeError("unknown action");
      if (fn) s.handlers[action] = fn; else delete s.handlers[action];
    },
  };
  if (micActive) s.setMicrophoneActive = (on: boolean) => { s.micCalls.push(on); return Promise.resolve(); };
  return s;
}

class Metadata { constructor(init: object) { Object.assign(this, init); } }

// Live, unmuted, visible session with a manual clock and timers.
function keysWith(opts: { toggleMic?: boolean; micActive?: boolean } = {}) {
  const audio = fakeAudio(), session = fakeSession(opts), calls: boolean[] = [], cues: boolean[] = [], logged: string[] = [];
  const timers: [number, () => void][] = [];
  let muted = false, clock = 100000, tapping = false;
  const keys = mediaKeys({
    audio, session, Metadata,
    setMuted: (m: boolean) => { calls.push(m); muted = m; },
    isMuted: () => muted,
    cue: (m) => cues.push(m),
    log: (line) => logged.push(line),
    gesture: () => tapping,
    now: () => clock,
    later: (fn: () => void, ms: number) => timers.push([clock + ms, fn]),
  });
  const tick = (ms: number) => {
    clock += ms;
    for (const t of timers.filter(([at]) => at <= clock)) { timers.splice(timers.indexOf(t), 1); t[1](); }
  };
  keys.enable();
  keys.sync({ live: true, muted: false, paused: false });
  const syncMuted = () => keys.sync({ live: true, muted, paused: false });
  // Core tap: set mute and sync within the gesture.
  const tap = (m: boolean) => { tapping = true; muted = m; syncMuted(); tapping = false; };
  return { keys, audio, session, calls, cues, logged, tick, syncMuted, tap };
}

test("play, pause, stop and a bare togglemicrophone each toggle the mute, with a cue", () => {
  const { session, calls, cues, tick } = keysWith();
  assert.deepEqual(Object.keys(session.handlers).sort(), ["pause", "play", "stop", "togglemicrophone"]);
  session.handlers.pause(); tick(1000);
  session.handlers.pause(); tick(1000);  // iOS may repeat pause: still a press
  session.handlers.play(); tick(1000);
  session.handlers.stop(); tick(1000);
  session.handlers.togglemicrophone();
  assert.deepEqual(calls, [true, false, true, false, true]);
  assert.deepEqual(cues, [true, false, true, false, true]);
});

test("togglemicrophone follows the state Safari reports, instead of toggling", () => {
  const { session, calls, cues, tick, syncMuted } = keysWith();
  session.handlers.togglemicrophone({ action: "togglemicrophone", isActivating: false });
  syncMuted(); tick(1000);
  session.handlers.togglemicrophone({ action: "togglemicrophone", isActivating: false });  // a toggle would wrongly unmute
  syncMuted(); tick(1000);
  session.handlers.togglemicrophone({ action: "togglemicrophone", isActivating: true });
  syncMuted(); tick(100);
  session.handlers.togglemicrophone({ action: "togglemicrophone", isActivating: true });
  assert.deepEqual(calls, [true, false]);
  assert.deepEqual(cues, [true, false]);
  assert.deepEqual(session.micCalls, [true]);  // Safari already switched the mic: not asked again
});

test("when the page and Safari disagree, Safari's report wins", () => {
  const { session, calls, tick, tap } = keysWith();
  tap(true); tick(1000);  // muted on screen
  session.handlers.togglemicrophone({ isActivating: false });  // already muted: no-op
  assert.deepEqual(calls, []);
  session.handlers.togglemicrophone({ isActivating: true });
  assert.deepEqual(calls, [false]);
});

test("a report of where a press left the mic counts right after a toggle, but a second toggle doesn't", () => {
  const { session, calls, tick } = keysWith();
  session.handlers.togglemicrophone({ isActivating: false });
  tick(100);
  session.handlers.pause();  // same press reported as a media key
  assert.deepEqual(calls, [true]);
  tick(100);
  session.handlers.togglemicrophone({ isActivating: true });  // a real second press, however quick
  assert.deepEqual(calls, [true, false]);
});

test("one press reported twice toggles once", () => {
  const { session, calls, tick } = keysWith();
  session.handlers.togglemicrophone();
  session.handlers.pause();
  tick(100);
  session.handlers.play();
  assert.deepEqual(calls, [true]);
  tick(1000);
  session.handlers.play();
  assert.deepEqual(calls, [true, false]);
});

test("browsers without togglemicrophone still get play, pause and stop", () => {
  const { session, logged } = keysWith({ toggleMic: false });
  assert.deepEqual(Object.keys(session.handlers).sort(), ["pause", "play", "stop"]);
  assert.ok(logged.includes("no togglemicrophone action"));
});

test("presses do nothing outside a session", () => {
  const { keys, session, calls, cues } = keysWith();
  keys.sync({ live: false, muted: false, paused: false });
  session.handlers.pause();
  assert.deepEqual(calls, []);
  assert.deepEqual(cues, []);
});

test("iOS muting the mic track on a press mutes the page, and unmuting unmutes it", () => {
  const { keys, calls, cues, tick, syncMuted } = keysWith();
  let trackMuted = true;
  keys.micMuted(() => trackMuted);
  assert.deepEqual(calls, []);  // delayed
  tick(400);
  assert.deepEqual(calls, [true]);
  syncMuted(); tick(1000);
  trackMuted = false;
  keys.micMuted(() => trackMuted); tick(400);
  assert.deepEqual(calls, [true, false]);
  assert.deepEqual(cues, [true, false]);
});

test("a track mute the page caused, or one that undoes itself, isn't a press", () => {
  const { keys, calls, tick, syncMuted } = keysWith();
  let trackMuted = true;
  keys.micMuted(() => trackMuted);
  trackMuted = false;  // blip: unmuted before the delay
  keys.micMuted(() => trackMuted);
  tick(400);
  assert.deepEqual(calls, []);
  tick(1000);
  keys.handlers.pause(); syncMuted(); tick(1000);  // page muted, so Safari mutes the track
  trackMuted = true;
  keys.micMuted(() => trackMuted); tick(400);
  assert.deepEqual(calls, [true]);
});

test("track mutes are ignored on the way to the background, just after returning and during recovery", () => {
  const { keys, calls, tick } = keysWith();
  keys.micMuted(() => true);
  keys.setVisible(false);  // iOS mutes the mic on backgrounding
  tick(400);
  keys.micMuted(() => true); tick(400);
  keys.setVisible(true);
  keys.micMuted(() => true); tick(400);  // iOS still handing the mic back
  tick(2000);
  keys.sync({ live: true, muted: false, paused: true });
  keys.micMuted(() => true); tick(400);
  assert.deepEqual(calls, []);
  keys.sync({ live: true, muted: false, paused: false });
  keys.micMuted(() => true); tick(400);
  assert.deepEqual(calls, [true]);
});

// heard(): an unreported iOS mute shows only as digital silence.
function hearing(keys: MediaKeys, tick: (ms: number) => void, silent: boolean | null, ms: number) {  // reports every 200 ms
  for (let t = 0; t < ms; t += 200) { keys.heard(silent); tick(200); }
}

test("lasting digital silence is iOS muting the mic, and sound coming back unmutes", () => {
  const { keys, session, calls, cues, logged, tick, syncMuted } = keysWith();
  tick(2000);
  hearing(keys, tick, false, 1000);
  hearing(keys, tick, true, 800);
  assert.deepEqual(calls, []);  // under a second
  hearing(keys, tick, true, 400);
  assert.deepEqual(calls, [true]);
  assert.deepEqual(cues, [true]);
  assert.equal(keys.phoneMuted, true);
  syncMuted();
  assert.deepEqual(session.micCalls, [true]);  // Safari not told: it would hide the returning sound
  hearing(keys, tick, true, 5000);  // still silent: no further action
  keys.heard(false);  // next press unmutes
  assert.deepEqual(calls, [true, false]);
  assert.deepEqual(cues, [true, false]);
  assert.equal(keys.phoneMuted, false);
  syncMuted();
  assert.deepEqual(session.micCalls, [true]);
  assert.ok(logged.includes("mic silence: mute") && logged.includes("mic sound: unmute"));
});

test("silence is only trusted once sound has come through", () => {
  const { keys, calls, tick } = keysWith();
  tick(2000);
  hearing(keys, tick, true, 5000);  // mic copy that never carries sound
  assert.deepEqual(calls, []);
});

test("silence while muted, just after connecting or returning, or while paused isn't a press", () => {
  const { keys, calls, tick, tap } = keysWith();
  hearing(keys, tick, false, 400);
  hearing(keys, tick, true, 1000);  // session just started
  assert.deepEqual(calls, []);
  tap(true);  // muted on screen: Safari mutes the mic, so silence
  hearing(keys, tick, true, 3000);
  tap(false);  // unmuted: silence count restarts
  hearing(keys, tick, true, 800);
  hearing(keys, tick, false, 200);
  keys.setVisible(false); keys.setVisible(true);
  hearing(keys, tick, true, 1400);
  keys.sync({ live: true, muted: false, paused: true });
  hearing(keys, tick, true, 3000);
  keys.sync({ live: true, muted: false, paused: false });
  hearing(keys, tick, null, 3000);  // unknown: ignored
  assert.deepEqual(calls, []);
  hearing(keys, tick, true, 1200);
  assert.deepEqual(calls, [true]);
});

test("a tap to unmute after iOS muted the mic asks Safari to switch it off and on, once", () => {
  const { keys, session, calls, logged, tick, syncMuted, tap } = keysWith();
  tick(2000);
  hearing(keys, tick, false, 400);
  hearing(keys, tick, true, 1200);
  syncMuted();
  assert.deepEqual(calls, [true]);
  tap(false);
  assert.deepEqual(session.micCalls, [true, false, true]);
  assert.equal(keys.phoneMuted, false);
  hearing(keys, tick, true, 3000);  // still silent after tap-unmute: distrusted, no mute
  assert.deepEqual(calls, [true]);
  assert.ok(logged.includes("mic still silent after a tap: not muting"));
  hearing(keys, tick, false, 200);  // sound: trusted again
  hearing(keys, tick, true, 1200);
  assert.deepEqual(calls, [true, true]);
});

test("the earphones mute and unmute through Safari and through silence alike, without double counting", () => {
  const { keys, session, calls, cues, tick, syncMuted } = keysWith();
  tick(2000);
  hearing(keys, tick, false, 400);
  session.handlers.togglemicrophone({ isActivating: false });  // Safari reports the press and mutes the mic
  syncMuted();
  hearing(keys, tick, true, 3000);
  session.handlers.togglemicrophone({ isActivating: true });
  syncMuted();
  hearing(keys, tick, false, 400);
  assert.deepEqual(calls, [true, false]);
  assert.deepEqual(cues, [true, false]);
  assert.equal(keys.phoneMuted, false);
});

test("enable starts the near-silent loop once, from the tap", () => {
  const { keys, audio } = keysWith();
  const src = audio.src;
  keys.enable();
  assert.equal(audio.loop, true);
  assert.match(src, /^blob:/);
  assert.equal(audio.src, src);
  assert.equal(audio.plays, 3);  // two taps and the sync
});

test("on screen, the loop plays whatever the mute, so presses keep coming", () => {
  const { keys, audio, session } = keysWith();
  assert.equal(session.playbackState, "playing");
  assert.equal(session.metadata.artist, "Listening");
  assert.equal(audio.paused, false);

  keys.sync({ live: true, muted: true, paused: false });
  assert.equal(session.playbackState, "playing");
  assert.equal(session.metadata.artist, "Muted");
  assert.equal(audio.paused, false);

  keys.sync({ live: true, muted: true, paused: false });  // no change, no extra call
  assert.deepEqual(session.micCalls, [true, false]);

  keys.sync({ live: false, muted: false, paused: false });
  assert.equal(session.playbackState, "none");
  assert.equal(session.metadata, null);
  assert.equal(audio.paused, true);
});

test("in the background there's nothing for Now Playing to show, and it all comes back on return", () => {
  const { keys, audio, session, calls } = keysWith();
  const src = audio.src;
  const pause = session.handlers.pause;
  keys.setVisible(false);
  assert.deepEqual(session.handlers, {});
  assert.equal(session.metadata, null);
  assert.equal(session.playbackState, "none");
  assert.equal(audio.paused, true);
  assert.equal(audio.src, "");
  assert.equal(audio.loads, 1);

  keys.sync({ live: true, muted: true, paused: false });  // not shown while hidden
  assert.equal(session.playbackState, "none");
  assert.equal(session.metadata, null);
  pause();  // press already in flight
  assert.deepEqual(calls, []);

  keys.setVisible(true);
  assert.deepEqual(Object.keys(session.handlers).sort(), ["pause", "play", "stop", "togglemicrophone"]);
  assert.equal(audio.src, src);
  assert.equal(audio.paused, false);
  assert.equal(session.playbackState, "playing");
  assert.equal(session.metadata.artist, "Muted");
});

test("a mic dropout shows as paused but doesn't tell Safari the mic is off", () => {
  const { keys, session } = keysWith();
  keys.sync({ live: true, muted: false, paused: true });
  assert.equal(session.metadata.artist, "Mic paused");
  assert.deepEqual(session.micCalls, [true]);
});

test("a new session tells Safari the mic state again", () => {
  const { keys, session } = keysWith();
  keys.sync({ live: false, muted: false, paused: false });
  keys.sync({ live: true, muted: false, paused: false });
  assert.deepEqual(session.micCalls, [true, true]);
});

test("without an audio element (desktop) nothing is registered or changed", () => {
  const session = fakeSession();
  const keys = mediaKeys({ audio: null, session, Metadata, setMuted() {}, isMuted: () => false });
  keys.enable();
  keys.sync({ live: true, muted: false, paused: false });
  keys.setVisible(false);
  keys.setVisible(true);
  assert.deepEqual(session.handlers, {});
  assert.equal(session.playbackState, "none");
});

// ── micGuard ────────────────────────────────────────────────────

// Minimal LiveKit room fake whose mic track can be ended, OS-muted or LiveKit-muted.
function fakeRoom({ ended = false, osMuted = false, lkMuted = false, failures = 0 } = {}) {
  const log: string[] = [];
  const newTrack = () => ({ readyState: "live", muted: false });
  const track = {
    isMuted: lkMuted,
    mediaStreamTrack: { readyState: ended ? "ended" : "live", muted: osMuted },
    async restartTrack() {
      log.push("restart");
      if (failures-- > 0) throw Object.assign(new Error("not now"), { name: "InvalidStateError" });
      track.mediaStreamTrack = newTrack();
    },
  };
  const lp = {
    get isMicrophoneEnabled() { return !track.isMuted; },
    getTrackPublication: (source: unknown) => (source === "microphone" ? { track } : undefined),
    async setMicrophoneEnabled(on: boolean) {
      log.push(on ? "enable" : "disable");
      if (on && failures-- > 0) throw Object.assign(new Error("not now"), { name: "InvalidStateError" });
      if (on && track.mediaStreamTrack.readyState === "ended") track.mediaStreamTrack = newTrack();  // LiveKit's unmute reacquires
      track.isMuted = !on;
    },
  };
  return { localParticipant: lp, track, log, startAudio: async () => { log.push("startAudio"); } };
}

type Ctx = { state: string; resume(): Promise<void> };
function guardWith(room: GuardRoom | null, { muted = false, ctx = null as Ctx | null, attempts = 3 } = {}) {
  const states: string[] = [], waits: number[] = [];
  const g = micGuard({
    room: () => room, wantMuted: () => muted, audioContext: () => ctx, source: "microphone", attempts,
    onPaused: (s) => states.push(s),
    wait: async (ms) => { waits.push(ms); },
  });
  return { g, states, waits, setMuted: (m: boolean) => { muted = m; } };
}

test("a live mic is left alone", async () => {
  const room = fakeRoom();
  const { g, states } = guardWith(room);
  assert.equal(await g.recover({ settle: 600 }), true);
  assert.deepEqual(states, []);
  assert.deepEqual(room.log, ["startAudio"]);
});

test("an ended mic that LiveKit still thinks is on gets a fresh track", async () => {
  const room = fakeRoom({ ended: true });
  const { g, states, waits } = guardWith(room);
  assert.equal(g.check(), true);  // flags the dropout immediately
  assert.equal(await g.recover({ settle: 600 }), true);
  assert.deepEqual(states, ["recovering", ""]);
  assert.deepEqual(room.log, ["startAudio", "restart"]);
  assert.equal(waits[0], 600);  // LiveKit gets the first try
});

test("a mic muted by the OS is restarted too", async () => {
  const room = fakeRoom({ osMuted: true });
  const { g } = guardWith(room);
  assert.equal(await g.recover(), true);
  assert.equal(room.track.mediaStreamTrack.muted, false);
});

test("a mic LiveKit muted after failing to restart it is unmuted", async () => {
  const room = fakeRoom({ ended: true, lkMuted: true });
  const { g, states } = guardWith(room);
  assert.equal(await g.recover(), true);
  assert.deepEqual(room.log, ["startAudio", "enable"]);
  assert.equal(room.localParticipant.isMicrophoneEnabled, true);
  assert.deepEqual(states, ["recovering", ""]);
});

test("a mic the user muted stays muted, even if its track ended", async () => {
  const room = fakeRoom({ ended: true, lkMuted: true });
  const { g, states } = guardWith(room, { muted: true });
  assert.equal(g.check(), false);
  assert.equal(await g.recover(), true);
  assert.deepEqual(room.log, ["startAudio"]);
  assert.deepEqual(states, []);
});

test("it retries, then asks for a tap, and the tap brings the mic back", async () => {
  const room = fakeRoom({ ended: true, failures: 3 });
  const { g, states, waits } = guardWith(room);
  assert.equal(await g.recover(), false);
  assert.deepEqual(room.log.filter((x) => x === "restart").length, 3);
  assert.deepEqual(states, ["recovering", "retry"]);
  assert.equal(g.state, "retry");
  assert.equal(g.error?.name, "InvalidStateError");
  assert.ok(waits.includes(800) && waits.includes(1600));  // backs off between attempts

  assert.equal(await g.recover(), true);  // the tap
  assert.deepEqual(states, ["recovering", "retry", "recovering", ""]);
  assert.equal(g.error, null);
});

test("if the user mutes during recovery, the mic ends up muted", async () => {
  const room = fakeRoom({ ended: true, lkMuted: true });
  const { g, states, setMuted } = guardWith(room);
  const orig = room.localParticipant.setMicrophoneEnabled;
  room.localParticipant.setMicrophoneEnabled = async (on) => { await orig(on); if (on) setMuted(true); };  // user mutes mid-recovery
  assert.equal(await g.recover(), true);
  assert.deepEqual(room.log, ["startAudio", "enable", "disable"]);
  assert.equal(room.localParticipant.isMicrophoneEnabled, false);
  assert.equal(states.at(-1), "");
});

test("overlapping calls (visibilitychange and pageshow) share one recovery", async () => {
  const room = fakeRoom({ ended: true });
  const { g } = guardWith(room);
  const [a, b] = [g.recover(), g.recover()];
  assert.equal(a, b);
  await a;
  assert.equal(room.log.filter((x) => x === "restart").length, 1);
});

test("a suspended or interrupted audio context is resumed without waiting on it", async () => {
  for (const state of ["suspended", "interrupted"]) {
    let resumed = false;
    const ctx = { state, resume: () => { resumed = true; return new Promise<void>(() => {}); } };  // iOS may never settle
    const { g } = guardWith(fakeRoom(), { ctx });
    assert.equal(await g.recover(), true);
    assert.equal(resumed, true);
  }
});

test("no room: nothing to do", async () => {
  const { g, states } = guardWith(null);
  assert.equal(g.check(), false);
  assert.equal(await g.recover(), true);
  assert.deepEqual(states, []);
});

test("reset clears a paused state when the session ends", async () => {
  const room = fakeRoom({ ended: true, failures: 9 });
  const { g, states } = guardWith(room, { attempts: 1 });
  await g.recover();
  g.reset();
  await nextTick();
  assert.deepEqual(states, ["recovering", "retry", ""]);
});
