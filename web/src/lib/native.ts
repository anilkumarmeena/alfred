// Bridge to the native apps' JarvisAudio plugin, which runs the LiveKit session natively (mic survives background,
// earphone button mutes). The page opens no Room; it drives the plugin and follows its events.
// In a browser nativeAudio() is null. No imports: Capacitor exposes the plugin on window.Capacitor.
// iOS app notifications also go through the plugin (appNotifications): the web view has no Web Push.

type Handle = { remove(): Promise<void> | void };
type Listener = (e: any) => void;

/** iOS Mic Mode: `active` is in effect now, `preferred` is the user's Control Center choice. */
export interface MicMode { active: string; preferred: string }

/** JarvisAudio plugin subset used by the page. Optional methods may be missing in older app builds. */
export interface NativeAudio {
  start(o: { serverUrl?: string; passcode: string; device?: string; muted?: boolean; speakerOff?: boolean }): Promise<unknown>;
  stop(): Promise<void>;
  setMuted(o: { muted: boolean }): Promise<{ muted: boolean }>;
  setSpeaker?(o: { off: boolean }): Promise<{ off: boolean }>;  // off = text-only replies
  // Older app builds only: they offer the pre-call mic and speaker back (status afterCall, call event restore)
  // where newer ones put them back themselves.
  restoreAfterCall?(o: { restore: boolean }): Promise<void>;
  sendText(o: { text: string }): Promise<void>;
  getStatus(): Promise<{ connection: string; agent?: string; muted: boolean; interrupted: boolean; startedAt?: number; micMode?: MicMode;
    speakerOff?: boolean; onCall?: boolean; afterCall?: object }>;
  showMicModes?(): Promise<void>;  // iOS only
  checkPermissions?(): Promise<{ notifications?: string }>;
  requestPermissions?(o?: { permissions: string[] }): Promise<{ notifications?: string }>;
  registerNotifications?(o: { serverUrl?: string; passcode: string }): Promise<{ notifications?: string }>;  // asks, then sends the token to JARVIS
  openSettings?(): Promise<unknown>;  // JARVIS's page in iOS Settings
  addListener(event: string, fn: Listener): Promise<Handle> | Handle;
}

/** The plugin inside the JARVIS app, else null. */
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

/** Notification permission; "prompt" until the user has been asked. */
export type Permission = "granted" | "denied" | "prompt";

// UIApplication.openSettingsURLString. Capacitor passes unserved top-level links to iOS, so this works without
// the plugin's openSettings.
export const APP_SETTINGS = "app-settings:";

/** iOS app notifications (APNs via the plugin). */
export interface AppNotifications {
  state(): Promise<Permission>;
  /** Ask iOS (dialog shows only once). `registered`: token already sent to JARVIS, not deferred to next session. */
  request(passcode: string): Promise<{ state: Permission; registered: boolean }>;
  /** Open iOS Settings, the only way to re-allow notifications once refused. */
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

/** Page callbacks the native session drives. */
export interface NativePage {
  setLink(v: "off" | "linking" | "on"): void;
  setState(s: string): void;  // AgentState, "connecting" or "reconnecting"
  setConnected(v: boolean): void;  // session exists, live or connecting
  setLive(v: boolean, startedAt?: number): void;  // connected with audio flowing
  setMuted(v: boolean): void;
  setPaused(v: "" | "recovering"): void;  // a call or Siri holds the mic
  setQuality(q: string | null): void;
  line(id: string, who: "you" | "jarvis", text: string): void;  // caption; replaces the line with this id
  ended(message?: string): void;  // message set unless the user ended it
  unauthorized(message: string): void;  // passcode refused
  setMicMode?(m: MicMode | null): void;  // iOS; null outside a session
  setSpeakerOff?(v: boolean, byCall?: boolean): void;  // plugin changed it; byCall: for a phone call, not the user's choice
}

export interface NativeSession {
  /** Session exists (connecting, live or reconnecting). */
  connected(): boolean;
  muted(): boolean;
  /** The app build supports speaker off. */
  canSilence: boolean;
  connect(o: { passcode: string; muted?: boolean; speakerOff?: boolean }): Promise<void>;
  hangup(): Promise<void>;
  setMuted(m: boolean): Promise<void>;
  setSpeakerOff(off: boolean): Promise<void>;
  /** Starts a muted session first if needed. */
  sendText(text: string, passcode: string, speakerOff?: boolean): Promise<void>;
  /** Voice levels 0..1, for the reactor. */
  levels(): { mic: number; agent: number };
  /** Resync with the plugin's current state (after a reload, or events missed while the page was paused). */
  adopt(): Promise<void>;
  /** iOS: open the system Mic Mode picker. */
  showMicModes(): void;
}

export function bindNative(plugin: NativeAudio, page: NativePage, origin = globalThis.location?.origin): NativeSession {
  let muted = false, live = false, agentKnown = false, last = "disconnected", starting = false;
  let choseInCall = false;  // the user set the mic or speaker since the last call began
  const connected = () => last !== "disconnected";
  const levels = { mic: 0, agent: 0 };
  const micMode = (m?: MicMode) => { if (m?.active) page.setMicMode?.({ active: m.active, preferred: m.preferred || m.active }); };
  const chose = (source?: string) => { if (source === "user" || source === "earphone") choseInCall = true; };
  // Older app builds offer the pre-call state back after a call: take it, unless the user chose meanwhile.
  const takeOffer = () => { plugin.restoreAfterCall?.({ restore: !choseInCall }).catch(() => {}); };

  // state: "connecting" | "reconnecting" | "connected"
  function up(state: string, startedAt?: number) {
    if (state === last) return;  // start() resolving repeats an event already seen
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
    if (reason === "unauthorized") return page.unauthorized(message || "Wrong passcode.");
    if (was || message) page.ended(reason === "hangup" ? undefined : message || "Connection lost. Tap the core to reconnect.");
  }

  const on = (event: string, fn: Listener) => { plugin.addListener(event, fn); };
  on("connection", (e) => (e.state === "disconnected" ? down(e.reason, e.message) : up(e.state)));
  on("agentState", (e) => { agentKnown = true; if (connected()) page.setState(e.state); });
  on("mute", (e) => { chose(e.source); muted = !!e.muted; page.setMuted(muted); });
  on("transcript", (e) => page.line(e.id, e.speaker === "you" ? "you" : "jarvis", e.text));
  on("interruption", (e) => page.setPaused(e.state === "began" ? "recovering" : ""));
  on("quality", (e) => page.setQuality(e.quality));
  on("levels", (e) => { levels.mic = +e.mic || 0; levels.agent = +e.agent || 0; });
  on("micMode", (e) => { if (connected()) micMode(e); });
  on("speaker", (e) => { chose(e.source); page.setSpeakerOff?.(!!e.off, e.source === "call"); });
  // The plugin quiets mic and speaker for a call and puts them back when it ends.
  on("call", (e) => { if (e.state === "began") choseInCall = false; else if (e.restore) takeOffer(); });

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
    if (s.connection === "disconnected") {  // ended while the page was paused (and not mid-start)
      if (connected() && !starting) down("server");
      return;
    }
    if (s.agent) agentKnown = true;
    muted = s.muted; page.setMuted(s.muted);
    page.setPaused(s.interrupted ? "recovering" : "");  // only while a call or Siri actually holds the mic
    if (s.speakerOff != null) page.setSpeakerOff?.(s.speakerOff, !!s.onCall);
    if (s.afterCall) takeOffer();
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
      // On refusal show the plugin's actual state.
      try { muted = (await plugin.setMuted({ muted: m })).muted; page.setMuted(muted); } catch { await adopt(); }
    },
    async setSpeakerOff(off) {  // no session: the page keeps the choice for the next start
      if (!connected() || !plugin.setSpeaker) return;
      try { page.setSpeakerOff?.((await plugin.setSpeaker({ off })).off); } catch { await adopt(); }
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

/** Fake analyser Tap for the reactor: a soft spectrum synthesised from a level. */
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
