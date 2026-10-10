// Earphone controls and mic recovery. No DOM or LiveKit imports: dependencies are injected so tests can fake them.
import type { Paused } from "../types";

const sleep = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms));
const quietly = (fn: () => unknown) => {
  try { const p = fn() as Promise<unknown> | undefined; if (p && p.catch) p.catch(() => {}); } catch {}
};

// Near-silent WAV (±1 LSB of 16-bit noise). iOS routes earphone presses only to a page playing real media,
// and may ignore pure zeros.
export function silentWav(seconds = 2, rate = 8000): ArrayBuffer {
  const n = seconds * rate, v = new DataView(new ArrayBuffer(44 + n * 2));
  const text = (at: number, s: string) => { for (let i = 0; i < s.length; i++) v.setUint8(at + i, s.charCodeAt(i)); };
  text(0, "RIFF"); v.setUint32(4, 36 + n * 2, true); text(8, "WAVE");
  text(12, "fmt "); v.setUint32(16, 16, true); v.setUint16(20, 1, true); v.setUint16(22, 1, true);  // PCM, mono
  v.setUint32(24, rate, true); v.setUint32(28, rate * 2, true); v.setUint16(32, 2, true); v.setUint16(34, 16, true);
  text(36, "data"); v.setUint32(40, n * 2, true);
  for (let i = 0, seed = 1; i < n; i++) { seed = seed * 16807 % 2147483647; v.setInt16(44 + i * 2, seed % 3 - 1, true); }
  return v.buffer;
}

// AudioContext subset used by chime. state may also be iOS's "interrupted".
export interface ChimeContext {
  state: string;
  currentTime: number;
  destination: AudioNode;
  resume(): Promise<void>;
  createOscillator(): OscillatorNode;
  createGain(): GainNode;
}

// Two-note earphone-press cue: falling = muted, rising = listening.
export function chime(ctx: ChimeContext | null, muted: boolean): void {
  if (!ctx || ctx.state === "closed") return;
  if (ctx.state !== "running") quietly(() => ctx.resume());
  try {
    const t = ctx.currentTime + 0.02;
    (muted ? [880, 587] : [587, 880]).forEach((hz, i) => {
      const osc = ctx.createOscillator(), gain = ctx.createGain(), at = t + i * 0.1;
      osc.frequency.value = hz;
      gain.gain.setValueAtTime(0.0001, at);
      gain.gain.exponentialRampToValueAtTime(0.15, at + 0.01);
      gain.gain.exponentialRampToValueAtTime(0.0001, at + 0.08);
      osc.connect(gain).connect(ctx.destination);
      osc.start(at); osc.stop(at + 0.09);
    });
  } catch {}
}

// <audio> element and Media Session subsets used by mediaKeys.
export interface KeysAudio {
  src: string;
  loop: boolean;
  play(): Promise<void>;
  pause(): void;
  removeAttribute(name: string): void;
  load(): void;
}
// isActivating: Safari's togglemicrophone reports which way it switched the mic.
type ActionHandler = (details?: MediaSessionActionDetails & { isActivating?: boolean }) => void;
export interface KeysSession {
  playbackState: string;
  metadata: unknown;
  setActionHandler(action: string, handler: ActionHandler | null): void;
  setMicrophoneActive?(active: boolean): Promise<void> | void;  // Safari only
}
export interface KeysState { live: boolean; muted: boolean; paused: boolean }
export interface MediaKeysOptions {
  audio: KeysAudio | null;
  session: KeysSession | null | undefined;
  setMuted(muted: boolean): void;
  isMuted(): boolean;
  Metadata?: new (init: { title: string; artist: string; artwork: MediaImage[] }) => unknown;
  artwork?: MediaImage[];
  cue?(muted: boolean): void;
  log?(line: string): void;
  gesture?(): boolean;
  now?(): number;
  later?(fn: () => void, ms: number): unknown;
  gap?: number;
  settle?: number;
  delay?: number;
  hush?: number;
}
export type MediaKeys = ReturnType<typeof mediaKeys>;

// Earphone controls, active only while the page is visible.
// - Mic live: iOS (un)mutes the mic itself on a press; Safari reports togglemicrophone (isActivating) and a
//   track mute. The page follows that state rather than toggling, which would drift.
// - Safari may not report the press at all; then the only sign is digital silence (heard()).
// - play/pause/stop each toggle; a duplicate report of one press (within `gap`) is dropped.
// - The near-silent loop plays while visible since iOS only sends presses to a playing page. Hidden, everything
//   is torn down so no Now Playing controls show.
export function mediaKeys({ audio, session, setMuted, isMuted, Metadata, artwork = [], cue = () => {}, log = () => {},
  gesture = () => !!(typeof navigator !== "undefined" && navigator.userActivation && navigator.userActivation.isActive),
  now = Date.now, later = setTimeout, gap = 700, settle = 1500, delay = 400, hush = 1000 }: MediaKeysOptions) {
  let ready = false, visible = true, url = "", micActive: boolean | null = null, pressedAt = -Infinity, shownAt = -Infinity;
  let last: KeysState = { live: false, muted: false, paused: false };
  let quietSince: number | null = null, phoneMuted = false, trusted = false, doubted = false, liveAt = -Infinity;
  // phoneMuted: iOS muted the mic without Safari reporting it. trusted, doubted: see heard().

  // to: resulting mute state, or undefined for a toggle.
  function press(to: boolean | undefined, why: string) {
    const muted = to === undefined ? !isMuted() : to;
    if (!visible || !last.live) return log(`${why}: ignored, ${visible ? "no session" : "hidden"}`);
    if (to === undefined && now() - pressedAt < gap) return log(`${why}: same press`);
    if (muted === isMuted()) return log(`${why}: already ${muted ? "muted" : "listening"}`);
    log(`${why}: ${muted ? "mute" : "unmute"}`);
    pressedAt = now();
    setMuted(muted); cue(muted);
  }
  const handlers: Record<"play" | "pause" | "stop" | "togglemicrophone", ActionHandler> = {
    play: () => press(undefined, "play"),
    pause: () => press(undefined, "pause"),
    stop: () => press(undefined, "stop"),
    togglemicrophone: (details) => {
      const on = details && details.isActivating;
      if (typeof on !== "boolean") return press(undefined, "togglemicrophone");
      micActive = on;  // Safari already switched the mic
      press(!on, `togglemicrophone(${on ? "on" : "off"})`);
    },
  };

  // Mic track (un)muted without the page asking. Delayed, and dropped unless still visible and settled: iOS also
  // mutes on the way to background and unmutes just after return. Ignored during recovery.
  function micMuted(trackMuted: () => boolean) {
    const at = now();
    log(`mic track ${trackMuted() ? "muted" : "unmuted"}`);
    later(() => { if (visible && !last.paused && at - shownAt >= settle) press(trackMuted(), "mic track"); }, delay);
  }

  // Polled a few times a second. silent: true = all-zero samples (a working mic never gives that), false = sound,
  // null = unknown. Read from an unmuted copy of the mic track.
  // Sustained silence while unmuted = iOS muted on an unreported press; sound returning = the unmuting press.
  // Only trusted after sound has been heard once, and distrusted if a tap-unmute didn't bring sound back.
  function heard(silent: boolean | null | undefined) {
    if (silent === null || silent === undefined || !visible || !last.live || last.paused) { quietSince = null; return; }
    if (!silent) {
      quietSince = null; trusted = true; doubted = false;
      if (phoneMuted) {
        phoneMuted = false;
        log("mic sound again");
        if (isMuted()) press(false, "mic sound");
      }
      return;
    }
    if (!trusted || isMuted() || now() - Math.max(shownAt, liveAt) < settle) { quietSince = null; return; }
    if (quietSince === null) quietSince = now();
    else if (now() - quietSince >= hush) {
      quietSince = null;
      if (doubted) { trusted = false; return log("mic still silent after a tap: not muting"); }
      phoneMuted = true;
      log("mic silent");
      press(true, "mic silence");
    }
  }

  // Tell Safari whether the mic should be on. Turning it on only works within a user gesture.
  function micState(active: boolean) {
    if (!session!.setMicrophoneActive) return;
    log(`setMicrophoneActive(${active})`);
    quietly(() => Promise.resolve(session!.setMicrophoneActive!(active))
      .catch((e) => log(`setMicrophoneActive(${active}) refused: ${(e && e.name) || e}`)));
  }

  function attach() {
    if (!audio!.src) audio!.src = url;
    for (const [action, fn] of Object.entries(handlers)) {
      try { session!.setActionHandler(action, fn); } catch { log(`no ${action} action`); }  // unsupported here
    }
  }

  function detach() {  // unload, not just pause: iOS keeps controls for a paused element
    quietly(() => audio!.pause());
    audio!.removeAttribute("src"); quietly(() => audio!.load());
    for (const action of Object.keys(handlers)) { try { session!.setActionHandler(action, null); } catch {} }
    try { session!.metadata = null; } catch {}
    session!.playbackState = "none";
  }

  function show() {  // Now Playing metadata and the loop, while visible
    const { live, muted, paused } = last;
    session!.playbackState = live ? "playing" : "none";
    if (Metadata) {
      try {
        session!.metadata = live
          ? new Metadata({ title: "JARVIS", artist: paused ? "Mic paused" : muted ? "Muted" : "Listening", artwork })
          : null;
      } catch {}
    }
    if (live) quietly(() => audio!.play()); else quietly(() => audio!.pause());
  }

  function enable() {  // call within a user gesture so iOS allows later playback
    if (!audio || !session || !visible) return;
    if (!ready) {
      ready = true;
      audio.loop = true;
      url = audio.src || URL.createObjectURL(new Blob([silentWav()], { type: "audio/wav" }));
      attach();
    }
    quietly(() => audio.play());
  }

  // live: in a session. muted: user's choice. paused: mic dropped out and is being recovered.
  function sync(state: KeysState) {
    if (!ready) return;
    if (state.muted !== last.muted || state.live !== last.live) quietSince = null;
    if (!state.live) phoneMuted = trusted = doubted = false;
    else if (!last.live) liveAt = now();
    last = state;
    // Tell Safari the user's choice only, never a dropout (it would mute capture and undo recovery), and not
    // when iOS muted on its own (Safari muting too would hide the sound returning on the next press).
    const active = state.live ? !state.muted : null;
    if (active === null) micActive = null;
    else if (!active && phoneMuted) {}  // leave Safari thinking the mic is on
    else if (active && phoneMuted && gesture()) {
      // Tap-unmute after iOS muted on its own: off-then-on makes Safari tell iOS to unmute. If it stays
      // muted, heard() catches it.
      phoneMuted = false; doubted = true;
      micState(false); micState(true);
      micActive = true;
    } else if (active !== micActive) { micState(active); micActive = active; }
    if (visible) show();
  }

  function setVisible(on: boolean) {  // visibilitychange, pagehide, pageshow
    if (on === visible) return;
    visible = on;
    quietSince = null;
    if (on) shownAt = now();
    if (!ready) return;
    if (on) { attach(); show(); } else detach();
  }

  return {
    enable, sync, setVisible, micMuted, heard, handlers,
    get phoneMuted() { return phoneMuted; },
  };
}

// LiveKit room subset used by micGuard.
export interface GuardTrack {
  isMuted: boolean;
  mediaStreamTrack: { readyState: string; muted: boolean };
  restartTrack(): Promise<void>;
}
export interface GuardRoom {
  localParticipant: {
    readonly isMicrophoneEnabled: boolean;
    getTrackPublication(source: never): { track?: GuardTrack } | undefined;
    setMicrophoneEnabled(on: boolean): Promise<unknown>;
  };
  startAudio(): Promise<void>;
}
export type PausedState = Exclude<Paused, "resuming">;  // "resuming" is session-level, not mic
export interface MicGuardOptions {
  room(): GuardRoom | null;
  wantMuted(): boolean;
  audioContext(): { state: string; resume(): Promise<void> } | null;
  source: unknown;  // Track.Source.Microphone
  onPaused(state: PausedState): void;
  wait?(ms: number): Promise<void>;
  attempts?: number;
  gap?: number;
}
export type MicGuard = ReturnType<typeof micGuard>;

// Recover the mic after a dropout (iOS cuts it when the Home Screen app backgrounds; LiveKit then mutes the track).
// Acts only when unmuted; LiveKit reacquires an ended track on unmute anyway.
// onPaused gets "recovering", "retry" (gave up; next tap retries) or "" (mic back).
export function micGuard({ room, wantMuted, audioContext, source, onPaused, wait = sleep, attempts = 3, gap = 800 }: MicGuardOptions) {
  let busy: Promise<boolean> | null = null, state: PausedState = "", error: (Error & { name: string }) | null = null;
  const set = (s: PausedState) => { if (s !== state) { state = s; onPaused(s); } };
  const micTrack = (r: GuardRoom) => r.localParticipant.getTrackPublication(source as never)?.track;

  function micDown() {  // unmuted but mic not live
    const r = room();
    if (!r || wantMuted()) return false;
    const t = r.localParticipant.isMicrophoneEnabled && micTrack(r)?.mediaStreamTrack;
    return !t || t.readyState === "ended" || t.muted;
  }

  function check() {  // flags a dropout; recover() fixes it
    const down = micDown();
    if (down && state !== "retry") set("recovering");
    if (!down && !busy) set("");
    return down;
  }

  async function run(settle: number) {
    // These don't settle until iOS allows it, so don't await them.
    const ctx = audioContext();
    if (ctx && ctx.state !== "running" && ctx.state !== "closed") quietly(() => ctx.resume());
    const r0 = room();
    if (r0) quietly(() => r0.startAudio());

    if (settle && micDown()) await wait(settle);  // give LiveKit a chance to restart it
    error = null;
    for (let i = 0; i < attempts && micDown(); i++) {
      if (i) await wait(gap * i);
      const r = room();
      if (!r || !micDown()) break;
      set("recovering");
      try {
        const t = micTrack(r);
        if (t && !t.isMuted) await t.restartTrack();  // LiveKit thinks it's on: get a fresh track
        else await r.localParticipant.setMicrophoneEnabled(true);  // unmuting reacquires an ended track
        error = null;
      } catch (e) { error = e as Error; }
      await wait(300);  // a fresh track can report muted for a moment
    }
    const r = room();
    if (r && wantMuted() && r.localParticipant.isMicrophoneEnabled) {  // user muted meanwhile
      try { await r.localParticipant.setMicrophoneEnabled(false); } catch {}
    }
    const down = micDown();
    set(down ? "retry" : "");
    return !down;
  }

  function recover({ settle = 0 } = {}): Promise<boolean> {  // true once mic is live or unwanted
    check();
    return busy || (busy = run(settle).finally(() => { busy = null; }));
  }

  function reset() { error = null; set(""); }

  return {
    check, recover, reset, micDown,
    get state() { return state; },
    get error() { return error; },
  };
}
