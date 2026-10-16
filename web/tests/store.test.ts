// @vitest-environment jsdom
// state/store.ts: the speaker choice kept per device. Run: make test (or npm test in web/)
import assert from "node:assert/strict";
import { test, vi } from "vitest";

vi.hoisted(() => {
  globalThis.matchMedia ??= ((q: string) => ({ matches: false, media: q, addEventListener() {}, removeEventListener() {} })) as any;
  const saved = new Map<string, string>();  // Node's own localStorage, without a file, keeps nothing
  vi.stubGlobal("localStorage", {
    getItem: (k: string) => saved.get(k) ?? null,
    setItem: (k: string, v: string) => { saved.set(k, v); },
    removeItem: (k: string) => { saved.delete(k); },
  });
});

import { SPEAKER_KEY, storage } from "../src/lib/storage";
import { resetSpeaker, setSpeakerOff, speakerOff } from "../src/state/store";

test("a phone call's speaker off shows, but the next session starts with the user's own choice", () => {
  setSpeakerOff(false);
  setSpeakerOff(true, false);  // a call turned it off
  assert.equal(speakerOff.peek(), true);
  assert.equal(storage.get(SPEAKER_KEY), null);
  resetSpeaker();  // the session ended during the call
  assert.equal(speakerOff.peek(), false);

  setSpeakerOff(true);  // the user's choice is kept
  assert.equal(storage.get(SPEAKER_KEY), "off");
  resetSpeaker();
  assert.equal(speakerOff.peek(), true);
});
