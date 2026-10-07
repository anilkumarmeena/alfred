// Tests for src/lib/retry.ts and src/lib/build.ts: the pauses between tries after JARVIS goes away, and reloading into
// a newer build of the page. Run with: make test (or npm test in web/)
import assert from "node:assert/strict";
import { afterEach, beforeEach, test, vi } from "vitest";
import { builds, type Page } from "../src/lib/build";
import { backoff, napper, retrier } from "../src/lib/retry";

beforeEach(() => { vi.useFakeTimers(); });
afterEach(() => { vi.useRealTimers(); });

// ── retry ───────────────────────────────────────────────────────

test("the pause starts short and grows to a few seconds, then stays there", () => {
  assert.deepEqual([0, 1, 2, 3, 4, 5, 50].map((n) => backoff(n)), [500, 1000, 2000, 4000, 5000, 5000, 5000]);
});

test("a retrier runs after each pause, once per schedule, and starts over when reset", () => {
  const runs: number[] = [];
  const r = retrier(() => runs.push(Date.now()));
  const t0 = Date.now();
  r.schedule(); r.schedule();  // already waiting: no second timer
  vi.advanceTimersByTime(499); assert.equal(runs.length, 0);
  vi.advanceTimersByTime(1); assert.deepEqual(runs, [t0 + 500]);
  r.schedule(); vi.advanceTimersByTime(1000); assert.equal(runs.length, 2);
  r.schedule(); vi.advanceTimersByTime(1999); assert.equal(runs.length, 2); vi.advanceTimersByTime(1); assert.equal(runs.length, 3);
  r.reset(); r.schedule(); vi.advanceTimersByTime(500); assert.equal(runs.length, 4);
});

test("cancel stops a waiting retrier and starts the pauses over", () => {
  let runs = 0;
  const r = retrier(() => runs++);
  r.schedule(); vi.advanceTimersByTime(500); assert.equal(runs, 1);
  r.schedule(); r.cancel(); vi.advanceTimersByTime(10000); assert.equal(runs, 1);
  r.schedule(); vi.advanceTimersByTime(500); assert.equal(runs, 2);
});

test("a nap can be cut short", async () => {
  const n = napper();
  let woke = false;
  const p = n.nap(5000).then(() => { woke = true; });
  await vi.advanceTimersByTimeAsync(100); assert.ok(!woke);
  n.wake(); await p; assert.ok(woke);
  n.wake();  // nothing waiting: harmless
});

// ── build ───────────────────────────────────────────────────────

function page(build: string) {
  const store = new Map<string, string>();
  const p: Page & { reloads: number; store: Map<string, string> } = {
    build, reloads: 0, store,
    storage: { getItem: (k) => store.get(k) ?? null, setItem: (k, v) => { store.set(k, v); }, removeItem: (k) => { store.delete(k); } },
    reload() { p.reloads++; },
  };
  return p;
}

test("the same build, or none said, leaves the page be", () => {
  const p = page("aaa"), b = builds(p);
  assert.equal(b.served("aaa"), false);
  assert.equal(b.served(undefined), false);
  assert.equal(b.served(""), false);
  assert.equal(p.reloads, 0);
});

test("a newer build reloads the page at once when no session is going", () => {
  const p = page("aaa"), b = builds(p);
  assert.equal(b.served("bbb"), true);
  assert.equal(p.reloads, 1);
  assert.equal(b.takeResume(), null);
});

test("the dev server's page, with no build of its own, never reloads", () => {
  const p = page(""), b = builds(p);
  assert.equal(b.served("bbb"), false);
  assert.equal(p.reloads, 0);
});

test("during a session it waits, then reloads into the newer build and brings the session back", () => {
  const p = page("aaa"), b = builds(p);
  let busy = true, resume: { muted: boolean } | null = null;
  b.follow({ busy: () => busy, resume: () => resume });
  assert.equal(b.served("bbb"), false);
  assert.equal(p.reloads, 0);
  busy = false; resume = { muted: true };  // the session dropped and is coming back
  assert.equal(b.reload(), true);
  assert.equal(p.reloads, 1);
  // The reloaded page (the same tab's storage) carries on with it, once
  const after = builds({ ...p, build: "bbb" });
  assert.deepEqual(after.takeResume(), { muted: true });
  assert.equal(after.takeResume(), null);
});

test("a reload that didn't take isn't tried again for the same build", () => {
  const p = page("aaa"), b = builds(p);
  assert.equal(b.served("bbb"), true);
  const again = builds(p);  // still the old page: the server, or a cache, handed it back
  assert.equal(again.served("bbb"), false);
  assert.equal(p.reloads, 1);
  assert.equal(again.served("ccc"), true);  // but a build newer still does
  assert.equal(p.reloads, 2);
});
