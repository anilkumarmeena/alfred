// The JARVIS app's native voice session, for the web page: copy this file to the jarvis repo as
// web/src/lib/native.ts (see web-hook/README.md for the rest of the change).
//
// In the iOS and Android apps the JarvisAudio plugin runs the LiveKit session natively, so the mic keeps going
// in the background and the earphone button mutes it. The page then doesn't open a Room of its own: it asks the
// plugin to start, stop, mute and send text, and follows the plugin's events. In a browser nativeAudio() is null
// and nothing here runs. No imports: Capacitor's bridge puts the plugin on window.Capacitor.
// The iPhone app's notifications go through the plugin too (appNotifications): the web view has no Web Push.

type Handle = { remove(): Promise<void> | void };
type Listener = (e: any) => void;

/** iOS's Mic Mode: `active` is processing the mic now, `preferred` is what the user picked in Control Center. */
export interface MicMode { active: string; preferred: string }

/** The mic and speaker as they were when a phone call came in: what the app offers to restore once it's over. */
export interface BeforeCall { muted: boolean; speakerOff: boolean }

/** The part of the JarvisAudio plugin the page uses (the full API is in jarvis-app's packages/jarvis-audio). */
export interface NativeAudio {
  start(o: { serverUrl?: string; passcode: string; device?: string; muted?: boolean; speakerOff?: boolean }): Promise<unknown>;
  stop(): Promise<void>;
  setMuted(o: { muted: boolean }): Promise<{ muted: boolean }>;
  setSpeaker?(o: { off: boolean }): Promise<{ off: boolean }>;  // newer builds: speaker off, JARVIS replies in text only
  restoreAfterCall?(o: { restore: boolean }): Promise<void>;  // newer builds: the answer to the offer after a call
  sendText(o: { text: string }): Promise<void>;
  getStatus(): Promise<{ connection: string; agent?: string; muted: boolean; interrupted: boolean; startedAt?: number; micMode?: MicMode;
    speakerOff?: boolean; afterCall?: BeforeCall }>;
  showMicModes?(): Promise<void>;  // iOS only: Apple's Mic Mode picker
  checkPermissions?(): Promise<{ notifications?: string }>;
  requestPermissions?(o?: { permissions: string[] }): Promise<{ notifications?: string }>;
  registerNotifications?(o: { serverUrl?: string; passcode: string }): Promise<{ notifications?: string }>;  // newer builds: asks, then hands the token to the Mac now
  openSettings?(): Promise<unknown>;  // newer builds: JARVIS's notification settings in iOS Settings
  addListener(event: string, fn: Listener): Promise<Handle> | Handle;
}

/** The plugin when running inside the JARVIS app, else null. */
export function nativeAudio(win: any = globalThis): NativeAudio | null {
  const cap = win?.Capacitor;
  if (!cap?.isNativePlatform?.()) return null;
  return cap.Plugins?.JarvisAudio ?? null;
}

/** "ios" or "android" inside the JARVIS app, else "". */
export function nativePlatform(win: any = globalThis): string {
  const cap = win?.Capacitor;
  return cap?.isNativePlatform?.() ? String(cap.getPlatform?.() ?? "") : "";
}

/** What iOS says about JARVIS's notifications: "prompt" until the user has been asked. */
export type Permission = "granted" | "denied" | "prompt";

// UIApplication.openSettingsURLString: JARVIS's page in iOS Settings. Capacitor hands a top-level link it doesn't
// serve to iOS, so this opens it even in builds whose plugin has no openSettings.
export const APP_SETTINGS = "app-settings:";

/** The iPhone app's own notifications (APNs, registered by the plugin), for the page's switch. */
export interface AppNotifications {
  state(): Promise<Permission>;
  /** Asks iOS (it only shows its dialog the first time). `registered`: the token went to the Mac now, rather than
   * when the next session starts, as older builds do. */
  request(passcode: string): Promise<{ state: Permission; registered: boolean }>;
  /** Opens JARVIS in iOS Settings, the only place notifications can be allowed again once refused. */
  openSettings(): Promise<void>;
}

const permission = (v: unknown): Permission => (v === "granted" || v === "denied" ? v : "prompt");

export function appNotifications(plugin: NativeAudio, origin = globalThis.location?.origin,
  go = (url: string) => globalThis.location.assign(url)): AppNotifications {
  return {
    async state() {
      try { return permission((await plugin.checkPermissions?.())?.notifications); } catch { return "prompt"; }
    },
    async request(passcode) {
      if (plugin.registerNotifications) {
        const r = await plugin.registerNotifications({ serverUrl: origin, passcode });
        const state = permission(r?.notifications);
        return { state, registered: state === "granted" };
      }
      return { state: permission((await plugin.requestPermissions?.({ permissions: ["notifications"] }))?.notifications), registered: false };
    },
    async openSettings() {
      if (plugin.openSettings) { try { await plugin.openSettings(); return; } catch {} }
      go(APP_SETTINGS);
    },
  };
}

/** What the session needs from the page: session.ts implements it with its store. */
export interface NativePage {
  setLink(v: "off" | "linking" | "on"): void;
  setState(s: string): void;  // an AgentState, or "connecting" / "reconnecting"
  setConnected(v: boolean): void;  // a session exists, live or still connecting
  setLive(v: boolean, startedAt?: number): void;  // connected with audio going
  setMuted(v: boolean): void;
  setPaused(v: "" | "recovering"): void;  // a call or Siri has the mic for now
  setQuality(q: string | null): void;
  line(id: string, who: "you" | "jarvis", text: string): void;  // a caption: replace the line with this id
  ended(message?: string): void;  // back to no session, saying why if it wasn't the user
  unauthorized(message: string): void;  // the passcode was refused: ask again
  setMicMode?(m: MicMode | null): void;  // iOS: the Mic Mode in use during a session, null without one
  setSpeakerOff?(v: boolean): void;  // the plugin turned the speaker off or on: a phone call, or the answer to its offer
  setAfterCall?(was: BeforeCall | null): void;  // a call ended: offer to put the mic and speaker back as they were
}

export interface NativeSession {
  /** A session exists (connecting, live or reconnecting). */
  connected(): boolean;
  muted(): boolean;
  /** The app can turn the speaker off (newer builds). */
  canSilence: boolean;
  connect(o: { passcode: string; muted?: boolean; speakerOff?: boolean }): Promise<void>;
  hangup(): Promise<void>;
  setMuted(m: boolean): Promise<void>;
  setSpeakerOff(off: boolean): Promise<void>;
  /** The answer to the offer after a phone call: put the mic and speaker back as they were, or leave them off. */
  restoreAfterCall(restore: boolean): Promise<void>;
  /** Starts a muted session first if there's none, as the page does for typed messages. */
  sendText(text: string, passcode: string, speakerOff?: boolean): Promise<void>;
  /** Voice levels from 0 to 1, for the reactor. */
  levels(): { mic: number; agent: number };
  /** Shows the plugin's state as it is now: a session it kept running while the page reloaded, or what changed
   * while the page was off screen, where its JavaScript is paused and events can be missed. */
  adopt(): Promise<void>;
  /** iOS: opens Apple's Mic Mode picker. */
  showMicModes(): void;
}

export function bindNative(plugin: NativeAudio, page: NativePage, origin = globalThis.location?.origin): NativeSession {
  let muted = false, live = false, agentKnown = false, last = "disconnected", starting = false;
  const connected = () => last !== "disconnected";
  const levels = { mic: 0, agent: 0 };
  const micMode = (m?: MicMode) => { if (m?.active) page.setMicMode?.({ active: m.active, preferred: m.preferred || m.active }); };

  // state is "connecting", "reconnecting" or "connected"
  function up(state: string, startedAt?: number) {
    if (state === last) return;  // start() resolving repeats the event that already came
    last = state;
    page.setConnected(true);
    page.setLink(state === "connected" ? "on" : "linking");
    if (state !== "connected") return page.setState(state);
    if (!live) { live = true; page.setLive(true, startedAt ?? Date.now()); }
    page.setState(agentKnown ? "listening" : "initializing");
  }

  function down(reason?: string, message?: string) {
    const was = connected();
    live = false; muted = false; agentKnown = false; last = "disconnected";
    levels.mic = levels.agent = 0;
    page.setMicMode?.(null);
    page.setAfterCall?.(null);
    if (reason === "unauthorized") return page.unauthorized(message || "Wrong passcode.");
    if (was || message) page.ended(reason === "hangup" ? undefined : message || "Connection lost. Tap the core to reconnect.");
  }

  const on = (event: string, fn: Listener) => { plugin.addListener(event, fn); };
  on("connection", (e) => (e.state === "disconnected" ? down(e.reason, e.message) : up(e.state)));
  on("agentState", (e) => { agentKnown = true; if (connected()) page.setState(e.state); });
  on("mute", (e) => { muted = !!e.muted; page.setMuted(muted); });
  on("transcript", (e) => page.line(e.id, e.speaker === "you" ? "you" : "jarvis", e.text));
  on("interruption", (e) => page.setPaused(e.state === "began" ? "recovering" : ""));
  on("quality", (e) => page.setQuality(e.quality));
  on("levels", (e) => { levels.mic = +e.mic || 0; levels.agent = +e.agent || 0; });
  on("micMode", (e) => { if (connected()) micMode(e); });
  on("speaker", (e) => page.setSpeakerOff?.(!!e.off));
  on("call", (e) => page.setAfterCall?.(e.state === "ended" && e.restore ? e.restore : null));

  async function connect({ passcode, muted: startMuted = false, speakerOff = false }: { passcode: string; muted?: boolean; speakerOff?: boolean }) {
    if (connected()) return;
    up("connecting");
    muted = startMuted; page.setMuted(startMuted);
    starting = true;
    try {
      const s: any = await plugin.start({ serverUrl: origin, passcode, muted: startMuted, speakerOff });
      micMode(s?.micMode);
      up(s?.connection || "connected", s?.startedAt);
    } catch (e: any) {
      const code = e?.code, message = e?.message || String(e);
      down(code === "UNAUTHORIZED" ? "unauthorized" : "server", message);
    } finally {
      starting = false;
    }
  }

  async function adopt() {
    const s = await plugin.getStatus().catch(() => null);
    if (!s) return;
    if (s.connection === "disconnected") {  // it ended while the page wasn't looking (not still starting)
      if (connected() && !starting) down("server");
      return;
    }
    if (s.agent) agentKnown = true;
    muted = s.muted; page.setMuted(s.muted);
    page.setPaused(s.interrupted ? "recovering" : "");  // "Mic paused" only while a call or Siri really has it
    if (s.speakerOff != null) page.setSpeakerOff?.(s.speakerOff);
    page.setAfterCall?.(s.afterCall ?? null);
    micMode(s.micMode);
    up(s.connection, s.startedAt);
    if (s.agent && s.connection === "connected") page.setState(s.agent);
  }

  return {
    connected,
    muted: () => muted,
    canSilence: !!plugin.setSpeaker,
    connect,
    async hangup() { await plugin.stop(); if (connected()) down("hangup"); },
    async setMuted(m) {
      if (!connected()) return;
      // Refused (the mic couldn't start, or the session is gone): show what the plugin has, not what was asked.
      try { muted = (await plugin.setMuted({ muted: m })).muted; page.setMuted(muted); } catch { await adopt(); }
    },
    async setSpeakerOff(off) {  // with no session the page just keeps the choice, for the next one's start
      if (!connected() || !plugin.setSpeaker) return;
      try { page.setSpeakerOff?.((await plugin.setSpeaker({ off })).off); } catch { await adopt(); }
    },
    async restoreAfterCall(restore) {
      page.setAfterCall?.(null);
      try { await plugin.restoreAfterCall?.({ restore }); } catch { await adopt(); }
    },
    async sendText(text, passcode, speakerOff = false) {
      if (!connected()) await connect({ passcode, muted: true, speakerOff });
      if (connected()) await plugin.sendText({ text });
    },
    levels: () => levels,
    showMicModes() { plugin.showMicModes?.().catch(() => {}); },
    adopt,
  };
}

/** Something the reactor can read like an analyser Tap, drawn from a level: a soft spectrum that follows it. */
export function levelTap(level: () => number) {
  const freq = new Uint8Array(128);
  return {
    freq,
    an: {
      getByteFrequencyData(out: Uint8Array) {
        const v = Math.max(0, Math.min(1, level())), t = Date.now() / 180;
        for (let i = 0; i < out.length; i++) {
          const shape = Math.exp(-i / 18) * (0.75 + 0.25 * Math.sin(t + i * 0.7));
          out[i] = Math.round(255 * Math.min(1, v * 2.2) * shape);
        }
      },
    },
  };
}
