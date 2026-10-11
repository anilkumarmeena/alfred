// @vitest-environment jsdom
// To-dos: panel/Todos.tsx and the to-do state in state/panel.ts. Run: make test (or npm test in web/)
import assert from "node:assert/strict";
import { h, render } from "preact";
import { act } from "preact/test-utils";
import { afterEach, beforeAll, beforeEach, test } from "vitest";
import type { Todo } from "../src/state/panel";

const MIN = 60;  // to-do times are in seconds
const at = (minutesAgo: number) => Date.now() / 1000 - minutesAgo * MIN;
const item = (id: number, text: string, extra: Partial<Todo> = {}): Todo => ({ id, text, done: false, created: at(3), done_at: null, ...extra });

let calls: { action: string; body: any }[] = [];
let answers: Record<string, unknown> = {};
let ui: typeof import("../src/components/panel/Todos");
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
  ui = await import("../src/components/panel/Todos");
});

let box: HTMLElement;
afterEach(() => { render(null, box); });
beforeEach(() => {
  calls = [];
  answers = {};
  box = document.createElement("div");
  document.body.replaceChildren(box);
  state.todos.value = null;
  state.freshTodos.value = new Set();
  state.toast("");
  store.now.value = Date.now();
});

const flush = () => act(async () => { await new Promise((r) => setTimeout(r, 0)); });
const show = () => act(() => { render(h(ui.TodosSection, {}), box); });
const texts = (sel: string) => [...box.querySelectorAll(sel)].map((e) => e.textContent);

test("a to-do the stream brings is highlighted; the first load and this device's own adds aren't", async () => {
  state.setTodos([item(1, "Renew the domain")]);  // first load
  assert.equal(state.freshTodos.value.size, 0);
  state.setTodos([item(1, "Renew the domain"), item(2, "Buy milk")]);  // said by voice
  assert.deepEqual([...state.freshTodos.value], [2]);

  answers.todo_add = [item(1, "Renew the domain"), item(2, "Buy milk"), item(3, "Call the bank")];
  await state.todoAct("todo_add", { text: "Call the bank" });
  assert.deepEqual([...state.freshTodos.value], [2]);

  await show();
  assert.deepEqual(texts(".todo-open .row.fresh .tap"), ["Buy milk"]);
  await act(() => { box.querySelector(".row.fresh")!.dispatchEvent(new Event("pointerdown", { bubbles: true })); });
  assert.equal(box.querySelector(".row.fresh"), null);  // touched: no longer new
});

test("each to-do says how long ago it was added, or done", async () => {
  state.todos.value = [item(1, "Renew the domain"), item(2, "Book the car service", { done: true, done_at: at(120) })];
  await show();
  assert.deepEqual(texts(".sub.when"), ["added 3 min ago", "done 2 h ago"]);
});

test("a delete can be undone from the toast, putting the same to-do back", async () => {
  const gone = item(1, "Renew the domain");
  state.todos.value = [gone];
  answers.todo_delete = [];
  await show();
  await act(() => { (box.querySelector('[aria-label="Delete"]') as HTMLButtonElement).click(); });
  await flush();
  assert.equal(state.toastText.value, "Deleted");
  answers.todo_restore = [gone];
  await act(() => { state.toastAction.value!.run(); });
  await flush();
  const { passcode: _, ...sent } = calls.at(-1)!.body;
  assert.equal(calls.at(-1)!.action, "todo_restore");
  assert.deepEqual(sent, gone);
  assert.deepEqual(texts(".todo-open .tap"), ["Renew the domain"]);
});

test("tapping an open to-do's text edits it, but not a link in it", async () => {
  state.todos.value = [item(1, "Read [the spec](https://example.com)"), item(2, "Old one", { done: true, done_at: at(5) })];
  await show();
  await act(() => { (box.querySelector(".tap a") as HTMLElement).click(); });
  assert.equal(box.querySelector(".todo-open input.field"), null);
  await act(() => { (box.querySelector(".tap") as HTMLElement).click(); });
  assert.equal((box.querySelector(".todo-open input.field") as HTMLInputElement).value, "Read [the spec](https://example.com)");
  assert.equal(box.querySelector(".todo-done .tap"), null);  // done ones aren't edited
});
