// lib/native.ts: the native app voice session bridge. Run: make test (or npm test in web/)
import assert from "node:assert/strict";
import { test } from "vitest";
import { APP_SETTINGS, appNotifications, bindNative, levelTap, nativeAudio, nativePlatform, type NativeAudio, type NativePage } from "../src/lib/native";

// ── nativeAudio ─────────────────────────────────────────────────

test("nativeAudio is null in a browser, so the page keeps its own Room and web push", () => {
  assert.equal(nativeAudio({}), null);
  assert.equal(nativeAudio(globalThis), null);
  assert.equal(nativeAudio({ Capacitor: { isNativePlatform: () => false, Plugins: { JarvisAudio: {} } } }), null);
  assert.equal(nativeAudio({ Capacitor: { isNativePlatform: () => true, Plugins: {} } }), null);
});

test("nativeAudio finds the JarvisAudio plugin inside the app", () => {
  const plugin = {};
  assert.equal(nativeAudio({ Capacitor: { isNativePlatform: () => true, Plugins: { JarvisAudio: plugin } } }), plugin);
});

// ── bindNative ──────────────────────────────────────────────────

function fakePlugin(start: () => Promise<unknown> = async () => ({ connection: "connected", startedAt: 5 })) {
  const listeners: Record<string, (e: any) => void> = {}, calls: string[] = [];
  const plugin: NativeAudio = {
    start: async (o) => { calls.push(`start muted=${o.muted}`); return start(); },
    stop: async () => { calls.push("stop"); },
    setMuted: async ({ muted }) => { calls.push(`setMuted ${muted}`); return { muted }; },
    sendText: async ({ text }) => { calls.push(`sendText ${text}`); },
    getStatus: async () => ({ connection: "connected", agent: "speaking", muted: true, interrupted: false, startedAt: 7 }),
    addListener: (event, fn) => { listeners[event] = fn; return { remove() {} }; },
  };
  const emit = (event: string, e: any) => listeners[event]?.(e);
  return { plugin, calls, emit };
}

function fakePage() {
  const log: string[] = [];
  const page: NativePage = {
    setLink: (v) => log.push(`link ${v}`),
    setState: (s) => log.push(`state ${s}`),
    setConnected: (v) => log.push(`connected ${v}`),
    setLive: (v, at) => log.push(`live ${v} ${at}`),
    setMuted: (v) => log.push(`muted ${v}`),
    setPaused: (v) => log.push(`paused ${v}`),
    setQuality: (q) => log.push(`quality ${q}`),
    line: (id, who, text) => log.push(`line ${id} ${who} ${text}`),
    ended: (m) => log.push(`ended ${m}`),
    unauthorized: (m) => log.push(`unauthorized ${m}`),
  };
  return { page, log };
}

test("connect starts the plugin and follows it to a live session", async () => {
  const { plugin, calls, emit } = fakePlugin(), { page, log } = fakePage();
  const s = bindNative(plugin, page, "https://jarvis.test");
  await s.connect({ passcode: "pw" });
  assert.deepEqual(calls, ["start muted=false"]);
  assert.ok(s.connected());
  assert.ok(log.includes("live true 5"));
  assert.equal(log.at(-1), "state initializing");
  emit("agentState", { state: "listening" });
  assert.equal(log.at(-1), "state listening");
  emit("transcript", { id: "a", speaker: "agent", text: "Hello" });
  assert.equal(log.at(-1), "line a jarvis Hello");
});

test("a refused passcode asks again, and hangup ends quietly", async () => {
  const bad = fakePlugin(async () => { throw Object.assign(new Error("nope"), { code: "UNAUTHORIZED" }); });
  const p1 = fakePage();
  const s1 = bindNative(bad.plugin, p1.page);
  await s1.connect({ passcode: "wrong" });
  assert.equal(p1.log.at(-1), "unauthorized nope");
  assert.ok(!s1.connected());

  const good = fakePlugin(), p2 = fakePage();
  const s2 = bindNative(good.plugin, p2.page);
  await s2.connect({ passcode: "pw" });
  await s2.hangup();
  assert.equal(p2.log.at(-1), "ended undefined");
  assert.ok(!s2.connected());
});

test("sendText starts a muted session first, and mute goes through the plugin", async () => {
  const { plugin, calls, emit } = fakePlugin(), { page, log } = fakePage();
  const s = bindNative(plugin, page);
  await s.setMuted(true);  // no session: nothing to mute
  assert.deepEqual(calls, []);
  await s.sendText("hi", "pw");
  assert.deepEqual(calls, ["start muted=true", "sendText hi"]);
  await s.setMuted(false);
  assert.equal(s.muted(), false);
  emit("mute", { muted: true, source: "earphone" });
  assert.equal(s.muted(), true);
  assert.equal(log.at(-1), "muted true");
});

test("adopt picks up a session the app kept running", async () => {
  const { plugin } = fakePlugin(), { page, log } = fakePage();
  const s = bindNative(plugin, page);
  await s.adopt();
  assert.ok(s.connected());
  assert.ok(s.muted());
  assert.ok(log.includes("live true 7"));
  assert.equal(log.at(-1), "state speaking");
});

test("adopt puts right a stale Mic paused or mute: the page missed the plugin's events while off screen", async () => {
  const { plugin, emit } = fakePlugin(), { page, log } = fakePage();
  let status = { connection: "connected", agent: "listening", muted: true, interrupted: true };
  plugin.getStatus = async () => status;
  const s = bindNative(plugin, page);
  await s.connect({ passcode: "pw" });
  emit("interruption", { state: "began" });
  emit("mute", { muted: true, source: "earphone" });
  assert.deepEqual(log.slice(-2), ["paused recovering", "muted true"]);
  status = { ...status, muted: false, interrupted: false };  // call ended and user unmuted meanwhile
  await s.adopt();
  assert.ok(log.includes("paused "));
  assert.ok(log.includes("muted false"));
  assert.equal(s.muted(), false);
  assert.equal(log.at(-1), "state listening");
});

test("a refused mute shows the plugin's state, and a session that ended unseen is ended", async () => {
  const { plugin } = fakePlugin(), { page, log } = fakePage();
  plugin.setMuted = async () => { throw Object.assign(new Error("The microphone couldn't start."), { code: "NO_MICROPHONE" }); };
  let status = { connection: "connected", muted: true, interrupted: false };
  plugin.getStatus = async () => status;
  const s = bindNative(plugin, page);
  await s.connect({ passcode: "pw" });
  const before = log.length;
  await s.setMuted(false);
  assert.equal(s.muted(), true);
  assert.ok(log.slice(before).includes("muted true"));
  status = { ...status, connection: "disconnected" };
  await s.adopt();
  assert.ok(!s.connected());
  assert.equal(log.at(-1), "ended Connection lost. Tap the core to reconnect.");
});

test("adopt leaves a session that's still starting alone", async () => {
  let finish!: () => void;
  const { plugin } = fakePlugin(() => new Promise((resolve) => { finish = () => resolve({ connection: "connected" }); }));
  plugin.getStatus = async () => ({ connection: "disconnected", muted: false, interrupted: false });
  const { page, log } = fakePage();
  const s = bindNative(plugin, page);
  const starting = s.connect({ passcode: "pw" });
  await s.adopt();
  assert.ok(s.connected());
  assert.ok(!log.some((l) => l.startsWith("ended")));
  finish(); await starting;
  assert.ok(s.connected());
});

test("the iOS Mic Mode follows the session: from start, its events, and gone when it ends", async () => {
  const { plugin, calls, emit } = fakePlugin(async () => ({ connection: "connected", micMode: { active: "standard", preferred: "standard" } }));
  plugin.showMicModes = async () => { calls.push("showMicModes"); };
  const modes: unknown[] = [], { page } = fakePage();
  page.setMicMode = (m) => modes.push(m);
  const s = bindNative(plugin, page);
  emit("micMode", { active: "voiceIsolation", preferred: "voiceIsolation" });  // no session: ignored
  assert.deepEqual(modes, []);
  await s.connect({ passcode: "pw" });
  assert.deepEqual(modes.at(-1), { active: "standard", preferred: "standard" });
  emit("micMode", { active: "voiceIsolation", preferred: "voiceIsolation" });
  assert.deepEqual(modes.at(-1), { active: "voiceIsolation", preferred: "voiceIsolation" });
  s.showMicModes();
  assert.equal(calls.at(-1), "showMicModes");
  await s.hangup();
  assert.equal(modes.at(-1), null);
});

function speakerPlugin() {
  const f = fakePlugin(), started: unknown[] = [];
  f.plugin.start = async (o) => { started.push(o.speakerOff); return { connection: "connected" }; };
  f.plugin.setSpeaker = async ({ off }) => { f.calls.push(`setSpeaker ${off}`); return { off }; };
  const speaker: boolean[] = [], byCall: boolean[] = [], { page, log } = fakePage();
  page.setSpeakerOff = (v, call = false) => { speaker.push(v); byCall.push(call); };
  return { ...f, started, speaker, byCall, page, log };
}

test("the speaker switch: asked for at the start, changed through the plugin, and followed when the plugin changes it", async () => {
  const { plugin, calls, emit, started, speaker, byCall, page } = speakerPlugin();
  const s = bindNative(plugin, page);
  assert.ok(s.canSilence);
  await s.setSpeakerOff(true);  // no session: page keeps the choice
  assert.deepEqual(calls, []);
  await s.sendText("hi", "pw", true);
  assert.deepEqual(started, [true]);  // typed-message session also starts silent
  await s.setSpeakerOff(false);
  assert.equal(calls.at(-1), "setSpeaker false");
  assert.equal(speaker.at(-1), false);
  assert.equal(byCall.at(-1), false);  // the user's choice
  emit("speaker", { off: true, source: "call" });  // phone call
  assert.equal(speaker.at(-1), true);
  assert.equal(byCall.at(-1), true);  // not the user's choice, so not kept for the next session
});

test("a call's end needs no answer: the plugin puts the mic and speaker back itself", async () => {
  const { plugin, calls, emit, speaker, byCall, page, log } = speakerPlugin();
  plugin.restoreAfterCall = async ({ restore }) => { calls.push(`restoreAfterCall ${restore}`); };
  const s = bindNative(plugin, page);
  await s.connect({ passcode: "pw" });
  emit("call", { state: "began" });
  emit("mute", { muted: true, source: "call" });
  emit("speaker", { off: true, source: "call" });
  emit("call", { state: "ended" });
  emit("speaker", { off: false, source: "call" });
  emit("mute", { muted: false, source: "call" });
  assert.ok(!calls.some((c) => c.startsWith("restoreAfterCall")));
  assert.deepEqual(speaker.slice(-2), [true, false]);
  assert.deepEqual(byCall.slice(-2), [true, true]);
  assert.equal(log.at(-1), "muted false");

  // Reloaded mid-call: the plugin's speaker off is the call's, not the user's choice
  plugin.getStatus = async () => ({ connection: "connected", muted: true, interrupted: true, speakerOff: true, onCall: true });
  await s.adopt();
  assert.equal(speaker.at(-1), true);
  assert.equal(byCall.at(-1), true);
  plugin.getStatus = async () => ({ connection: "connected", muted: false, interrupted: false, speakerOff: true });
  await s.adopt();
  assert.equal(byCall.at(-1), false);  // no call on: the user's own choice
});

test("older app builds offer the pre-call state back: the page takes it unless the user chose during the call", async () => {
  const { plugin, calls, emit, page } = speakerPlugin();
  plugin.restoreAfterCall = async ({ restore }) => { calls.push(`restoreAfterCall ${restore}`); };
  const s = bindNative(plugin, page);
  await s.connect({ passcode: "pw" });
  emit("mute", { muted: true, source: "user" });  // before the call: doesn't count
  emit("call", { state: "began" });
  emit("mute", { muted: true, source: "call" });
  emit("call", { state: "ended", restore: { muted: false, speakerOff: false } });
  assert.equal(calls.at(-1), "restoreAfterCall true");

  emit("call", { state: "began" });
  emit("mute", { muted: false, source: "earphone" });  // the user unmuted during the call
  emit("call", { state: "ended", restore: { muted: false, speakerOff: false } });
  assert.equal(calls.at(-1), "restoreAfterCall false");

  emit("call", { state: "began" });
  emit("speaker", { off: false, source: "user" });  // the speaker counts too
  emit("call", { state: "ended", restore: { muted: false, speakerOff: false } });
  assert.equal(calls.at(-1), "restoreAfterCall false");

  // Missed while hidden: adopt takes the offer still open
  emit("call", { state: "began" });
  calls.length = 0;
  plugin.getStatus = async () => ({ connection: "connected", muted: true, interrupted: false, speakerOff: true, afterCall: { muted: false, speakerOff: false } });
  await s.adopt();
  assert.deepEqual(calls, ["restoreAfterCall true"]);
});

test("an app build without the speaker switch says so, and the page leaves it alone", async () => {
  const { plugin, calls } = fakePlugin(), { page } = fakePage();
  const s = bindNative(plugin, page);
  assert.equal(s.canSilence, false);
  await s.connect({ passcode: "pw" });
  await s.setSpeakerOff(true);
  assert.deepEqual(calls, ["start muted=false"]);
});

// ── appNotifications ────────────────────────────────────────────

test("nativePlatform says ios or android inside the app, nothing in a browser", () => {
  assert.equal(nativePlatform({}), "");
  assert.equal(nativePlatform({ Capacitor: { isNativePlatform: () => false, getPlatform: () => "web" } }), "");
  assert.equal(nativePlatform({ Capacitor: { isNativePlatform: () => true, getPlatform: () => "ios" } }), "ios");
});

test("the switch reads iOS's answer, and an older build asks through requestPermissions", async () => {
  const { plugin, calls } = fakePlugin();
  let answer = "prompt";
  plugin.checkPermissions = async () => ({ microphone: "granted", notifications: answer });
  plugin.requestPermissions = async (o) => { calls.push(`request ${o?.permissions}`); answer = "granted"; return { notifications: answer }; };
  const n = appNotifications(plugin, "https://jarvis.test");
  assert.equal(await n.state(), "prompt");
  assert.deepEqual(await n.request("pw"), { state: "granted", registered: false });  // token deferred to next session
  assert.deepEqual(calls, ["request notifications"]);
  assert.equal(await n.state(), "granted");
});

test("a newer build registers with JARVIS straight away", async () => {
  const { plugin, calls } = fakePlugin();
  plugin.requestPermissions = async () => { calls.push("requestPermissions"); return { notifications: "granted" }; };
  plugin.registerNotifications = async (o) => { calls.push(`register ${o.serverUrl} ${o.passcode}`); return { notifications: "granted" }; };
  const n = appNotifications(plugin, "https://jarvis.test");
  assert.deepEqual(await n.request("pw"), { state: "granted", registered: true });
  assert.deepEqual(calls, ["register https://jarvis.test pw"]);

  plugin.registerNotifications = async () => ({ notifications: "denied" });
  assert.deepEqual(await n.request("pw"), { state: "denied", registered: false });
});

test("a missing or failing check counts as not asked yet", async () => {
  const { plugin } = fakePlugin();
  assert.equal(await appNotifications(plugin).state(), "prompt");
  plugin.checkPermissions = async () => { throw new Error("no"); };
  assert.equal(await appNotifications(plugin).state(), "prompt");
});

test("openSettings uses the plugin's, else follows the app-settings: link that Capacitor hands to iOS", async () => {
  const { plugin, calls } = fakePlugin(), went: string[] = [];
  const n = appNotifications(plugin, "https://jarvis.test", (u) => went.push(u));
  await n.openSettings();
  assert.deepEqual(went, [APP_SETTINGS]);
  plugin.openSettings = async () => { calls.push("openSettings"); };
  await n.openSettings();
  assert.deepEqual(calls, ["openSettings"]);
  assert.deepEqual(went, [APP_SETTINGS]);
  plugin.openSettings = async () => { throw new Error("not implemented"); };  // bridge without the method
  await n.openSettings();
  assert.deepEqual(went, [APP_SETTINGS, APP_SETTINGS]);
});

// ── levelTap ────────────────────────────────────────────────────

test("levelTap draws silence at level 0 and a spectrum when there's sound", () => {
  let level = 0;
  const t = levelTap(() => level), out = new Uint8Array(128);
  t.an.getByteFrequencyData(out);
  assert.ok(out.every((v) => v === 0));
  level = 0.5;
  t.an.getByteFrequencyData(out);
  assert.ok(out[0]! > 0);
});
