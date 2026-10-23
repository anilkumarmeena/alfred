// @vitest-environment jsdom
// Helpers beside tasks: quick lookups and tool calls carrying on, in panel/Tasks.tsx's list and state/panel.ts.
// Run: make test (or npm test in web/)
import assert from "node:assert/strict";
import { h, render } from "preact";
import { act } from "preact/test-utils";
import { afterEach, beforeAll, beforeEach, test } from "vitest";
import type { Helper } from "../src/state/panel";

const ago = (seconds: number) => Date.now() / 1000 - seconds;
const helper = (id: string, about: string, extra: Partial<Helper> = {}): Helper =>
  ({ id, kind: "computer_lookup", about, status: "running", activity: null, started: ago(75), ended: null, ...extra });

let calls: { action: string; body: any }[] = [];
let answers: Record<string, unknown> = {};
let ui: typeof import("../src/components/panel/Tasks");
let state: typeof import("../src/state/panel");
let store: typeof import("../src/state/store");

beforeAll(async () => {
  (globalThis as any).matchMedia = () => ({ matches: false, addEventListener() {}, removeEventListener() {} });
  globalThis.fetch = (async (url: string, init: any) => {
    const action = String(url).split("/").pop()!, body = JSON.parse(init?.body ?? "{}");
    calls.push({ action, body });
    return new Response(JSON.stringify({ result: answers[action] ?? null }), { status: 200 });
  }) as any;
  state = await import("../src/state/panel");
  store = await import("../src/state/store");
  ui = await import("../src/components/panel/Tasks");
});

let box: HTMLElement;
afterEach(() => { render(null, box); });
beforeEach(() => {
  calls = [];
  answers = {};
  box = document.createElement("div");
  document.body.replaceChildren(box);
  state.tasks.value = [];
  state.helpers.value = [];
  store.now.value = Date.now();
});

const flush = () => act(async () => { await new Promise((r) => setTimeout(r, 0)); });
const show = () => act(() => { render(h(ui.TaskList, {}), box); });
const texts = (sel: string) => [...box.querySelectorAll(sel)].map((e) => e.textContent);

test("a running helper is listed under Running as a quick lookup, with its latest step and how long it has run", async () => {
  state.helpers.value = [helper("a1", "the computer's health", { activity: "Bash: command uptime" })];
  await show();
  assert.deepEqual(texts(".task.helper .title"), ["the computer's health"]);
  assert.ok(texts(".task.helper .tag").includes("quick lookup"));
  assert.match(box.querySelector(".task.helper .meta")!.textContent!, /^checking the computer · 1m 1\ds$/);
  assert.deepEqual(texts(".task.helper .doing"), ["Bash: command uptime"]);
  assert.match(box.querySelector(".tsum")!.textContent!, /0 running · 1 quick lookup/);
  assert.equal(box.textContent!.includes("Nothing running."), false);
});

test("its stop button asks the runner to stop it by its id", async () => {
  state.helpers.value = [helper("a1", "the news")];
  answers.helper_stop = { ...helper("a1", "the news"), status: "stopping" };
  answers.tasks = { tasks: [], approvals: [], helpers: [answers.helper_stop] };
  await show();
  await act(() => { (box.querySelector('[aria-label="Stop the lookup on the news"]') as HTMLButtonElement).click(); });
  await flush();
  assert.deepEqual(calls.map((c) => [c.action, c.body.id]), [["helper_stop", "a1"], ["tasks", undefined]]);
  assert.deepEqual(texts(".task.helper .tag"), ["quick lookup", "stopping"]);
  assert.equal(box.querySelector('[aria-label="Stop the lookup on the news"]'), null);  // no second stop
});

test("a finished helper shows under Recent with its outcome and no stop button", async () => {
  state.helpers.value = [helper("b2", "flights to Lisbon", { status: "timed_out", ended: ago(15), activity: "carries on as task 12" })];
  await show();
  assert.deepEqual(texts(".task.helper.ended .tag"), ["quick lookup", "timed out"]);
  assert.match(box.querySelector(".task.helper .meta")!.textContent!, /took 1m 00s/);
  assert.deepEqual(texts(".task.helper .doing"), ["carries on as task 12"]);
  assert.equal(box.querySelector(".task.helper .ico"), null);
  assert.ok(box.textContent!.includes("Nothing running."));
  assert.equal(box.textContent!.includes("No tasks in the last day."), false);
});
