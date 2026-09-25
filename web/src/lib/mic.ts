// Earphone controls and mic recovery for the page. No DOM or LiveKit imports: the page hands in what
// they need, and tests/mic.test.ts drives them with fakes.
import type { Paused } from "../types";

const sleep = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms));
const quietly = (fn: () => unknown) => {
  try { const p = fn() as Promise<unknown> | undefined; if (p && p.catch) p.catch(() => {}); } catch {}
};

// A couple of seconds of near-silence as a WAV file: ±1 step of 16-bit noise, far below hearing.
// iOS only hands earphone presses to a page that is playing real media, and may ignore pure zeros.
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

// Just the parts of an AudioContext the chime uses. state also includes iOS's "interrupted".
export interface ChimeContext {
  state: string;
  currentTime: number;
  destination: AudioNode;
  resume(): Promise<void>;
  createOscillator(): OscillatorNode;
  createGain(): GainNode;
}

// A short two-note cue for an earphone press: falling for muted, rising for listening again.
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

// The <audio> element and Media Session that mediaKeys drives, as far as it uses them.
export interface KeysAudio {
  src: string;
  loop: boolean;
  play(): Promise<void>;
  pause(): void;
  removeAttribute(name: string): void;
  load(): void;
}
// isActivating: Safari's togglemicrophone says which way it switched the mic.
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

// Earphone controls, only while the page is on screen. With the mic live, iOS doesn't send an
// earphone press to the page as play/pause: it mutes or unmutes the mic itself (that's the tone you
// hear) and Safari reports it as a togglemicrophone action whose isActivating says which way it went,
// and as the mic track muting. The page follows that state; toggling instead would drift the moment
// the two disagreed. If Safari doesn't pass the press on at all, iOS still mutes the mic, and the
// only sign is the mic going digitally silent (heard). play, pause and stop, from earphones iOS
// treats as media controls, each toggle; a second report of one press is dropped. The near-silent
// loop keeps playing while the page is visible, as iOS only sends those to a page that's playing.
// In the background it all goes, so no Now Playing controls show and a press is left to other apps.
export function mediaKeys({ audio, session, setMuted, isMuted, Metadata, artwork = [], cue = () => {}, log = () => {},
  gesture = () => !!(typeof navigator !== "undefined" && navigator.userActivation && navigator.userActivation.isActive),
  now = Date.now, later = setTimeout, gap = 700, settle = 1500, delay = 400, hush = 1000 }: MediaKeysOptions) {
  let ready = false, visible = true, url = "", micActive: boolean | null = null, pressedAt = -Infinity, shownAt = -Infinity;
  let last: KeysState = { live: false, muted: false, paused: false };
  let quietSince: number | null = null, phoneMuted = false, trusted = false, doubted = false, liveAt = -Infinity;
  // phoneMuted: iOS muted the mic without Safari saying so. trusted, doubted: see heard().

  // to: the mute state the press left the mic in, or undefined for a toggle.
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
      micActive = on;  // Safari already switched the mic; no need to ask it again
      press(!on, `togglemicrophone(${on ? "on" : "off"})`);
    },
  };

  // The mic track went muted or unmuted without the page asking; trackMuted reads its state now.
  // Acted on a moment later, and only if the page stayed on screen: iOS also mutes the mic on the way
  // to the background, and hands it back just after returning. Ignored while it's being recovered.
  function micMuted(trackMuted: () => boolean) {
    const at = now();
    log(`mic track ${trackMuted() ? "muted" : "unmuted"}`);
    later(() => { if (visible && !last.paused && at - shownAt >= settle) press(trackMuted(), "mic track"); }, delay);
  }

  // What the mic is picking up, a few times a second while in a session: silent is true for digital
  // silence (every sample exactly zero, which a working mic never gives), false for any sound, null if
  // it can't tell. Read from a copy of the mic track that the page never mutes, so it still hears
  // once the page is muted. Silence that lasts while the user wants to be heard is iOS muting the mic
  // on a press Safari didn't pass on; sound coming back afterwards is the press that unmuted it.
  // Only trusted once some sound has come through, so a copy that never carries sound can't mute
  // anyone, and not again if a tap to unmute didn't bring the sound back.
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

  // Tells Safari whether the mic is meant to be on. Turning it on only works from a tap.
  function micState(active: boolean) {
    if (!session!.setMicrophoneActive) return;
    log(`setMicrophoneActive(${active})`);
    quietly(() => Promise.resolve(session!.setMicrophoneActive!(active))
      .catch((e) => log(`setMicrophoneActive(${active}) refused: ${(e && e.name) || e}`)));
  }

  function attach() {
    if (!audio!.src) audio!.src = url;
    for (const [action, fn] of Object.entries(handlers)) {
      try { session!.setActionHandler(action, fn); } catch { log(`no ${action} action`); }  // unknown to this browser
    }
  }

  function detach() {  // unloaded, not just paused: iOS keeps showing controls for a paused element
    quietly(() => audio!.pause());
    audio!.removeAttribute("src"); quietly(() => audio!.load());
    for (const action of Object.keys(handlers)) { try { session!.setActionHandler(action, null); } catch {} }
    try { session!.metadata = null; } catch {}
    session!.playbackState = "none";
  }

  function show() {  // what the session shows, and the loop, while on screen
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

  function enable() {  // call from a tap, so iOS lets the element play later on its own
    if (!audio || !session || !visible) return;
    if (!ready) {
      ready = true;
      audio.loop = true;
      url = audio.src || URL.createObjectURL(new Blob([silentWav()], { type: "audio/wav" }));
      attach();
    }
    quietly(() => audio.play());
  }

  // live: in a session. muted: the user's choice. paused: the mic dropped out and is being brought back.
  function sync(state: KeysState) {
    if (!ready) return;
    if (state.muted !== last.muted || state.live !== last.live) quietSince = null;
    if (!state.live) phoneMuted = trusted = doubted = false;
    else if (!last.live) liveAt = now();
    last = state;
    // Safari is told the user's choice, not a dropout: it mutes capture itself when told the mic is
    // off, which would undo the recovery. Nor when iOS muted the mic on its own: Safari muting it
    // too would hide the sound coming back when the next press unmutes it.
    const active = state.live ? !state.muted : null;
    if (active === null) micActive = null;
    else if (!active && phoneMuted) {}  // Safari keeps thinking the mic is on
    else if (active && phoneMuted && gesture()) {
      // Unmuting from a tap after iOS muted the mic on its own: off then on has Safari tell iOS to
      // unmute, so the tap works without the earphones. If iOS keeps it muted, heard() says so again.
      phoneMuted = false; doubted = true;
      micState(false); micState(true);
      micActive = true;
    } else if (active !== micActive) { micState(active); micActive = active; }
    if (visible) show();
  }

  function setVisible(on: boolean) {  // from visibilitychange, pagehide and pageshow
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

// Just enough of a LiveKit room for micGuard; tests/mic.test.ts fakes it.
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
export type PausedState = Exclude<Paused, "resuming">;  // "resuming" belongs to the session, not the mic
export interface MicGuardOptions {
  room(): GuardRoom | null;
  wantMuted(): boolean;
  audioContext(): { state: string; resume(): Promise<void> } | null;
  source: unknown;  // LiveKit's Track.Source.Microphone, passed to getTrackPublication
  onPaused(state: PausedState): void;
  wait?(ms: number): Promise<void>;
  attempts?: number;
  gap?: number;
}
export type MicGuard = ReturnType<typeof micGuard>;

// Gets the mic back after it dropped out: iOS cuts it when the Home Screen app goes to the background,
// and LiveKit, failing to restart it there, mutes the track. Only acts when the user wants to be heard;
// a muted mic is left alone, as LiveKit reacquires an ended track when it's unmuted.
// onPaused gets "recovering", "retry" (it gave up: the next tap should try again) or "" (mic back).
export function micGuard({ room, wantMuted, audioContext, source, onPaused, wait = sleep, attempts = 3, gap = 800 }: MicGuardOptions) {
  let busy: Promise<boolean> | null = null, state: PausedState = "", error: (Error & { name: string }) | null = null;
  const set = (s: PausedState) => { if (s !== state) { state = s; onPaused(s); } };
  const micTrack = (r: GuardRoom) => r.localParticipant.getTrackPublication(source as never)?.track;

  function micDown() {  // the user wants to be heard, but the mic isn't live
    const r = room();
    if (!r || wantMuted()) return false;
    const t = r.localParticipant.isMicrophoneEnabled && micTrack(r)?.mediaStreamTrack;
    return !t || t.readyState === "ended" || t.muted;
  }

  function check() {  // marks a dropout now; recover() fixes it
    const down = micDown();
    if (down && state !== "retry") set("recovering");
    if (!down && !busy) set("");
    return down;
  }

  async function run(settle: number) {
    // Neither of these settles until iOS allows it, so don't wait on them.
    const ctx = audioContext();
    if (ctx && ctx.state !== "running" && ctx.state !== "closed") quietly(() => ctx.resume());
    const r0 = room();
    if (r0) quietly(() => r0.startAudio());

    if (settle && micDown()) await wait(settle);  // LiveKit restarts the mic itself when it can
    error = null;
    for (let i = 0; i < attempts && micDown(); i++) {
      if (i) await wait(gap * i);
      const r = room();
      if (!r || !micDown()) break;
      set("recovering");
      try {
        const t = micTrack(r);
        if (t && !t.isMuted) await t.restartTrack();  // LiveKit still thinks it's on: get a fresh track
        else await r.localParticipant.setMicrophoneEnabled(true);  // unmuting reacquires an ended track
        error = null;
      } catch (e) { error = e as Error; }
      await wait(300);  // a fresh track can report muted for a moment
    }
    const r = room();
    if (r && wantMuted() && r.localParticipant.isMicrophoneEnabled) {  // the user muted while we worked
      try { await r.localParticipant.setMicrophoneEnabled(false); } catch {}
    }
    const down = micDown();
    set(down ? "retry" : "");
    return !down;
  }

  function recover({ settle = 0 } = {}): Promise<boolean> {  // resolves true once the mic is live, or not wanted
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
