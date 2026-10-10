// @vitest-environment jsdom
// Photo attach: lib/photo.ts (fit) and state/photo.ts (upload, consume), endpoints faked. Run: make test (or npm test in web/)
import assert from "node:assert/strict";
import { beforeEach, test, vi } from "vitest";

const fake = vi.hoisted(() => {
  globalThis.matchMedia ??= ((q: string) => ({ matches: false, media: q, addEventListener() {}, removeEventListener() {} })) as any;
  let n = 0;
  URL.createObjectURL = () => `blob:photo-${++n}`;
  URL.revokeObjectURL = () => {};
  return { posts: [] as [string, Record<string, unknown>][], fail: null as Error | null };
});

vi.mock("../src/lib/api", async (orig) => {
  const m = await orig<typeof import("../src/lib/api")>();
  return {
    ...m,
    postAuthedJson: async (path: string, fields: Record<string, unknown>) => {
      fake.posts.push([path, fields]);
      if (fake.fail) throw fake.fail;
      return path === "/api/picture" ? { ok: true, id: `20261008-120000-abcde${fake.posts.length}.jpg` } : { ok: true };
    },
  };
});

import { fitSize } from "../src/lib/photo";
import { attach, discard, photo, photoReady } from "../src/state/photo";
import { passcode } from "../src/state/store";
import { addLine } from "../src/state/transcript";
import { Unauthorized } from "../src/lib/api";

const file = () => new File([new Uint8Array([0xff, 0xd8, 0xff, 0xe0, 1, 2, 3])], "IMG_0001.jpg", { type: "image/jpeg" });

beforeEach(() => {
  discard();
  fake.posts.length = 0;
  fake.fail = null;
  passcode.value = "123456";
});

test("a photo is scaled down to fit, never up", () => {
  assert.deepEqual(fitSize(4032, 3024), [1568, 1176]);
  assert.deepEqual(fitSize(3024, 4032), [1176, 1568]);
  assert.deepEqual(fitSize(800, 600), [800, 600]);
  assert.deepEqual(fitSize(1568, 10), [1568, 10]);
  assert.deepEqual(fitSize(0, 0), [1, 1]);
});

test("a photo picked goes to the Mac at once, and waits for the next turn", async () => {
  attach(file());
  assert.equal(photo.value?.state, "sending");
  assert.equal(await photoReady(), true);
  assert.equal(photo.value?.state, "ready");
  const [path, fields] = fake.posts[0]!;
  assert.equal(path, "/api/picture");
  assert.equal(fields.data, btoa(String.fromCharCode(0xff, 0xd8, 0xff, 0xe0, 1, 2, 3)));  // jsdom can't decode: sent as is
  assert.ok(photo.value?.id);
});

test("the next thing the user says or types takes it", async () => {
  attach(file());
  await photoReady();
  addLine("jarvis").value = "Hello.";  // JARVIS lines don't consume it
  assert.equal(photo.value?.state, "ready");
  addLine("you").value = "What's this?";
  assert.equal(photo.value, null);
});

test("removing it before asking takes it back off the Mac", async () => {
  attach(file());
  await photoReady();
  const id = photo.value!.id;
  discard();
  assert.equal(photo.value, null);
  assert.deepEqual(fake.posts[1], ["/api/picture/drop", { id }]);
});

test("picking another replaces it, and one removed while sending is dropped when it lands", async () => {
  attach(file());
  const first = photoReady();
  discard();  // still uploading: nothing to drop yet
  assert.equal(await first, false);
  assert.equal(fake.posts.at(-1)![0], "/api/picture/drop");
  attach(file());
  await photoReady();
  attach(file());
  assert.equal(fake.posts.filter(([p]) => p === "/api/picture/drop").length, 2);
});

test("a photo that doesn't get there says why, and isn't sent with the next turn", async () => {
  fake.fail = new Error("That isn't a picture JARVIS can read.");
  attach(file());
  assert.equal(await photoReady(), false);
  assert.equal(photo.value?.state, "failed");
  assert.match(photo.value!.error!, /isn't a picture/);
  addLine("you").value = "hello";
  assert.equal(photo.value?.state, "failed");  // stays until removed
});

test("a wrong passcode asks for it again", async () => {
  fake.fail = new Unauthorized("Wrong passcode.");
  attach(file());
  await photoReady();
  assert.equal(passcode.value, null);
});
