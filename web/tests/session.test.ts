// @vitest-environment jsdom
// state/session.ts auto-reconnect (Alfred restart, network drop, no agent), with Room and /api/token faked.
// Run: make test (or npm test in web/)
import assert from "node:assert/strict";
import { afterEach, beforeAll, test, vi } from "vitest";

const fake = vi.hoisted(() => {
  vi.useFakeTimers();
  globalThis.matchMedia ??= ((q: string) => ({ matches: false, media: q, addEventListener() {}, removeEventListener() {} })) as any;
  const store = new Map<string, string>();
  return {
    page: {
      build: "aaa", reloads: 0, store,
      storage: {
        getItem: (k: string) => store.get(k) ?? null,
        setItem: (k: string, v: string) => { store.set(k, v); },
        removeItem: (k: string) => { store.delete(k); },
      },
      reload() { fake.page.reloads++; },
    },
    token: null as null | (() => Promise<Record<string, any>>),
    tokens: 0,
    asked: [] as Record<string, any>[],  // /api/token request bodies
    rooms: [] as any[],
    micError: null as unknown,
  };
});

vi.mock("../src/lib/build", async (orig) => {
  const m = await orig<typeof import("../src/lib/build")>();
  return { ...m, build: m.builds(fake.page) };
});

vi.mock("../src/lib/api", async (orig) => {
  const m = await orig<typeof import("../src/lib/api")>();
  return {
    ...m,
    postAuthedJson: async (_path: string, body: Record<string, any>) => {
      fake.tokens++; fake.asked.push(body);
      return fake.token ? fake.token() : { url: "ws://livekit", token: "jwt", build: "aaa" };
    },
  };
});

vi.mock("livekit-client", () => {
  const RoomEvent = new Proxy({}, { get: (_, name) => name });
  class Room {
    handlers = new Map<string, ((...a: any[]) => void)[]>();
    remoteParticipants = new Map<string, any>();
    canPlaybackAudio = true;
    connected = false; ended = false; mic = false;
    attrs: Record<string, string>[] = [];  // attributes the page set
    localParticipant = {
      identity: "me",
      attributes: {} as Record<string, string>,
      setAttributes: async (a: Record<string, string>) => { this.attrs.push(a); Object.assign(this.localParticipant.attributes, a); },
      setMicrophoneEnabled: async (on: boolean) => { if (on && fake.micError) throw fake.micError; this.mic = on; },
      getTrackPublication: () => undefined,
      sendText: async () => {},
    };
    constructor() { fake.rooms.push(this); }
    on(ev: string, fn: (...a: any[]) => void) { this.handlers.set(ev, [...(this.handlers.get(ev) ?? []), fn]); return this; }
    emit(ev: string, ...a: any[]) { for (const fn of this.handlers.get(ev) ?? []) fn(...a); }
    registerTextStreamHandler() {}
    async connect() {  // apply the token's attributes, as the server does
      this.connected = true;
      this.localParticipant.attributes = { "alfred.speaker": fake.asked.at(-1)?.speaker ?? "on" };
    }
    async startAudio() {}
    async disconnect() { if (this.ended) return; this.ended = true; this.emit("Disconnected"); }
    agentJoins(state = "listening") {
      const p = { isAgent: true, identity: "agent", attributes: { "lk.agent.state": state } };
      this.remoteParticipants.set("agent", p);
      this.emit("ParticipantConnected", p);
    }
    agentLeaves() {
      const p = this.remoteParticipants.get("agent");
      this.remoteParticipants.delete("agent");
      this.emit("ParticipantDisconnected", p);
    }
  }
  return { Room, RoomEvent, Track: { Source: { Microphone: "microphone" }, Kind: { Audio: "audio" } } };
});

let S: typeof import("../src/state/session");
let store: typeof import("../src/state/store");
let player: HTMLAudioElement;

beforeAll(async () => {
  S = await import("../src/state/session");
  store = await import("../src/state/store");
  store.setPasscode("123456");
  player = document.createElement("audio");
  S.initSession({ player, keepalive: document.createElement("audio") });
});

afterEach(async () => {
  S.hangup();
  await vi.advanceTimersByTimeAsync(0);
  fake.token = null; fake.micError = null; fake.tokens = 0;
  if (store.speakerOff.peek()) S.toggleSpeaker();
  store.joinMuted.value = false;
  assert.equal(store.connected.peek(), false);
});

const tick = (ms = 0) => vi.advanceTimersByTimeAsync(ms);
const room = () => fake.rooms[fake.rooms.length - 1];
const reconnecting = () => store.agentState.peek() === "reconnecting" && store.resuming.peek() && store.link.peek() === "linking";

async function live({ muted = false } = {}) {
  await S.connect({ muted });
  room().agentJoins();
  assert.ok(store.live.peek());
  await tick(30000);  // a steady session
}

test("when Alfred restarts, the session comes back by itself, muted as it was", async () => {
  await live();
  await S.toggleMute();
  assert.equal(store.muted.peek(), true);
  const first = room(), before = fake.tokens;

  first.agentLeaves();  // agent service stopped
  assert.ok(first.ended);
  assert.ok(reconnecting());
  assert.equal(store.readout.peek().label, "Reconnecting");
  assert.equal(store.live.peek(), false);
  assert.equal(store.error.peek(), "");  // no "tap to reconnect": already retrying

  await tick(499);
  assert.equal(fake.tokens, before);
  await tick(1);
  assert.equal(fake.tokens, before + 1);
  assert.notEqual(room(), first);
  assert.ok(store.live.peek());
  assert.equal(store.muted.peek(), true);
  assert.equal(room().mic, false);  // muted: mic stays closed
  assert.equal(store.resuming.peek(), false);
  assert.equal(store.agentState.peek(), "initializing");
  room().agentJoins();
  assert.equal(store.agentState.peek(), "listening");
});

test("an open mic is opened again, so you can just keep talking", async () => {
  await live();
  room().agentLeaves();
  await tick(500);
  assert.ok(store.live.peek());
  assert.equal(store.muted.peek(), false);
  assert.equal(room().mic, true);
});

test("while Alfred is down it keeps trying: quickly, then every few seconds", async () => {
  await live();
  fake.token = () => Promise.reject(new TypeError("Failed to fetch"));
  room().disconnect();  // LiveKit gave up on the old connection
  const before = fake.tokens, at: number[] = [];
  for (const ms of [500, 1000, 2000, 4000, 5000, 5000, 5000]) {
    await tick(ms - 1);
    at.push(fake.tokens - before);
    await tick(1);
    assert.ok(reconnecting());
    assert.equal(store.error.peek(), "");
  }
  assert.deepEqual(at, [0, 1, 2, 3, 4, 5, 6]);
  assert.equal(fake.tokens - before, 7);

  fake.token = null;  // back up
  await tick(5000);
  assert.ok(store.live.peek());
  assert.equal(store.resuming.peek(), false);
});

test("back online, or back on screen, it tries at once", async () => {
  await live();
  fake.token = () => Promise.reject(new TypeError("Failed to fetch"));
  room().disconnect();
  await tick(500 + 1000 + 2000);
  const before = fake.tokens;
  fake.token = null;
  S.online();  // window online event
  await tick(0);
  assert.equal(fake.tokens, before + 1);
  assert.ok(store.live.peek());
});

test("a room no agent joins, made while Alfred was starting, is swapped for a fresh one", async () => {
  await S.connect();
  const first = room();
  assert.ok(store.live.peek());
  await tick(14999);
  assert.ok(!first.ended);
  await tick(1);
  assert.ok(first.ended);
  assert.ok(reconnecting());
  await tick(500);
  assert.notEqual(room(), first);
  room().agentJoins();
  await tick(20000);
  assert.ok(!room().ended);
});

test("a first connect that can't reach Alfred keeps trying too", async () => {
  fake.token = () => Promise.reject(new Error("Couldn't reach Alfred."));
  await S.connect();
  assert.ok(reconnecting());
  fake.token = null;
  await tick(500);
  assert.ok(store.live.peek());
});

test("End stops it trying", async () => {
  await live();
  fake.token = () => Promise.reject(new TypeError("Failed to fetch"));
  room().disconnect();
  await tick(500);
  const before = fake.tokens;
  S.hangup();
  assert.equal(store.resuming.peek(), false);
  assert.equal(store.agentState.peek(), "idle");
  assert.equal(store.link.peek(), "off");
  await tick(60000);
  assert.equal(fake.tokens, before);
});

test("a mic the browser won't give back without a tap leaves the session up, muted", async () => {
  await live();
  fake.micError = Object.assign(new Error("denied"), { name: "NotAllowedError" });
  room().agentLeaves();
  await tick(500);
  assert.ok(store.live.peek());
  assert.equal(store.muted.peek(), true);
  assert.match(store.error.peek(), /mic off/);
});

test("the mic refused on a tap still stops it, as before", async () => {
  fake.micError = Object.assign(new Error("denied"), { name: "NotAllowedError" });
  await S.connect();
  assert.equal(store.live.peek(), false);
  assert.equal(store.resuming.peek(), false);
  assert.match(store.error.peek(), /Microphone access is blocked/);
  await tick(10000);
  assert.equal(fake.tokens, 1);
});

test("an agent that keeps leaving straight after joining is given up on", async () => {
  await live();
  for (let i = 0; i < 3; i++) {
    room().agentLeaves();
    await tick(500);
    room().agentJoins();
    await tick(1000);
  }
  room().agentLeaves();
  assert.equal(store.resuming.peek(), false);
  assert.match(store.error.peek(), /keeps ending the session/);
  const before = fake.tokens;
  await tick(30000);
  assert.equal(fake.tokens, before);
});

test("a wrong passcode on the way back stops it and asks for the passcode", async () => {
  const { Unauthorized } = await import("../src/lib/api");
  await live();
  fake.token = () => Promise.reject(new Unauthorized("Wrong passcode."));
  room().agentLeaves();
  await tick(500);
  assert.equal(store.resuming.peek(), false);
  assert.ok(store.loginShown.peek());
  store.setPasscode("123456"); store.hideLogin();
});

test("a message typed while it reconnects waits for the session that comes back", async () => {
  await live();
  fake.token = () => Promise.reject(new TypeError("Failed to fetch"));
  room().disconnect();
  await tick(500);
  fake.token = null;
  S.sendText("still there?");
  await tick(0);
  const sent: string[] = [];
  room().localParticipant.sendText = async (text: string) => { sent.push(text); };
  room().agentJoins();
  assert.deepEqual(sent, ["still there?"]);
});

test("a newer build served on the way back reloads the page, which picks the session up", async () => {
  await live({ muted: true });
  fake.token = async () => ({ url: "ws://livekit", token: "jwt", build: "bbb" });
  room().agentLeaves();
  await tick(500);
  assert.equal(fake.page.reloads, 1);
  assert.equal(fake.page.store.get("alfred.resume"), "muted");
});

test("Mute before connecting starts the session with the mic closed, and the session's mute takes over", async () => {
  S.pressMute();
  assert.equal(store.joinMuted.peek(), true);
  assert.equal(store.connected.peek(), false);
  await S.connect();
  room().agentJoins();
  assert.ok(store.live.peek());
  assert.equal(room().mic, false);  // never opened
  assert.equal(store.muted.peek(), true);
  assert.equal(store.joinMuted.peek(), false);
  await S.toggleMute();
  assert.equal(room().mic, true);
});

test("a mute pressed while it connects holds: the mic isn't opened", async () => {
  const going = S.connect();
  await S.toggleMute();  // still fetching the token
  await going;
  assert.ok(store.live.peek());
  assert.equal(room().mic, false);
  assert.equal(store.muted.peek(), true);
});

test("speaker off: the session starts silent, and the switch reaches the agent during it", async () => {
  S.toggleSpeaker();  // before connecting
  assert.equal(store.speakerOff.peek(), true);
  await live();
  assert.equal(fake.asked.at(-1)!.speaker, "off");  // in the token, so the greeting is silent
  assert.deepEqual(room().attrs, []);
  assert.equal(store.readout.peek().hint, "Speaker off: replies come as text");
  room().emit("ParticipantAttributesChanged", { "lk.agent.state": "speaking" });
  assert.equal(store.readout.peek().label, "Replying");

  S.toggleSpeaker();
  assert.deepEqual(room().attrs, [{ "alfred.speaker": "on" }]);
  assert.equal(player.muted, false);
  S.toggleSpeaker();
  assert.deepEqual(room().attrs.at(-1), { "alfred.speaker": "off" });
  assert.equal(player.muted, true);  // in-progress reply stops at once
});

test("the speaker switched while it connects is told to the agent once it's in", async () => {
  const going = S.connect();
  S.toggleSpeaker();
  await going;
  assert.equal(fake.asked.at(-1)!.speaker, "on");  // requested before the toggle
  assert.deepEqual(room().attrs, [{ "alfred.speaker": "off" }]);
});
