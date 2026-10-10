// @vitest-environment jsdom
// Panel History: lib/history.ts, state/history.ts, panel/History.tsx. Run: make test (or npm test in web/)
import assert from "node:assert/strict";
import { h, render } from "preact";
import { act } from "preact/test-utils";
import { afterEach, beforeAll, beforeEach, test } from "vitest";
import { byDay, dayLabel, marks, span, tally, type DayDetail, type Days, type Found, type Hit } from "../src/lib/history";

// ── Helpers ─────────────────────────────────────────────────────

test("days are called Today, Yesterday, or by name, with the year only when it isn't this one", () => {
  assert.equal(dayLabel("2026-10-08", "2026-10-08"), "Today");
  assert.equal(dayLabel("2026-10-07", "2026-10-08"), "Yesterday");
  assert.equal(dayLabel("2026-10-05", "2026-10-08"), "Monday 5 October");
  assert.equal(dayLabel("2025-12-31", "2026-01-01"), "Yesterday");
  assert.equal(dayLabel("2025-12-30", "2026-01-01"), "Tuesday 30 December 2025");
});

test("a day's times and how much was said", () => {
  assert.equal(span("09:14", "21:53"), "09:14–21:53");
  assert.equal(span("09:14", "09:14"), "09:14");
  assert.equal(tally(1, 1), "1 exchange");
  assert.equal(tally(12, 15), "12 exchanges · 3 updates");
  assert.equal(tally(0, 1), "0 exchanges · 1 update");
});

test("a search's words are marked in any case, longest first", () => {
  assert.deepEqual(marks("Run the Tests, then test again", ["test", "tests"]), [
    { text: "Run the ", hit: false }, { text: "Tests", hit: true }, { text: ", then ", hit: false },
    { text: "test", hit: true }, { text: " again", hit: false },
  ]);
  assert.deepEqual(marks("a (b) c", ["(b)"]).filter((m) => m.hit), [{ text: "(b)", hit: true }]);
  assert.deepEqual(marks("plain", []), [{ text: "plain", hit: false }]);
  assert.deepEqual(marks("", ["x"]), []);
});

test("hits run together by day, in order", () => {
  const hit = (date: string, n: number) => ({ date, n }) as Hit;
  const grouped = byDay([hit("2026-10-08", 3), hit("2026-10-08", 1), hit("2026-10-05", 9)]);
  assert.deepEqual(grouped.map((g) => [g.date, g.hits.map((x) => x.n)]), [["2026-10-08", [3, 1]], ["2026-10-05", [9]]]);
});

// ── History view ────────────────────────────────────────────────

const DAYS: Days = {
  today: "2026-10-08", more: true, days: [
    { date: "2026-10-08", count: 3, exchanges: 2, first: "09:00", last: "10:30", preview: "and the tests?" },
    { date: "2026-10-07", count: 1, exchanges: 1, first: "22:00", last: "22:00", preview: "good night" },
  ],
};
const OLDER: Days = {
  today: "2026-10-08", more: false, days: [
    { date: "2026-10-05", count: 1, exchanges: 1, first: "08:00", last: "08:00", preview: "morning" },
  ],
};
const DAY: DayDetail = {
  date: "2026-10-08", newer: null, older: "2026-10-07", entries: [
    { n: 3, time: "10:30", kind: "announcement", said: "Task 12, the login page, is done." },
    { n: 2, time: "09:05", kind: "exchange", who: "Alex", said: "and the tests?", approved: ["make test"], reply: "", interrupted: true },
    { n: 1, time: "09:00", kind: "exchange", who: "Alex", said: "what's on today?", approved: [], reply: "Two meetings.", interrupted: false },
  ],
};
const FOUND: Found = {
  text: "tests", more: false, hits: [
    { ...DAY.entries[1]!, date: "2026-10-08" },
    { n: 1, time: "22:00", kind: "exchange", who: "Alex", said: "are the TESTS green?", approved: [], reply: "Yes.", interrupted: false,
      date: "2026-10-07" },
  ],
};

let calls: { action: string; body: any }[] = [];
let answers: Record<string, unknown> = {};
let ui: typeof import("../src/components/panel/History");
let state: typeof import("../src/state/panel");
let hist: typeof import("../src/state/history");

beforeAll(async () => {
  (globalThis as any).matchMedia = () => ({ matches: false, addEventListener() {}, removeEventListener() {} });
  Element.prototype.scrollIntoView = () => {};
  globalThis.fetch = (async (url: string, init: any) => {
    const action = String(url).split("/").pop()!, body = JSON.parse(init?.body ?? "{}");
    calls.push({ action, body });
    const answer = typeof answers[action] === "function" ? (answers[action] as (b: any) => unknown)(body) : answers[action];
    return new Response(JSON.stringify({ result: answer ?? null }), { status: 200 });
  }) as any;
  state = await import("../src/state/panel");
  hist = await import("../src/state/history");
  ui = await import("../src/components/panel/History");
});

let box: HTMLElement;
afterEach(() => { render(null, box); });
beforeEach(() => {
  calls = [];
  answers = { conversations: (b: any) => (b.before ? OLDER : DAYS), conversation: DAY, conversation_search: FOUND };
  box = document.createElement("div");
  document.body.replaceChildren(box);
  hist.days.value = null;
  hist.day.value = null;
  hist.found.value = null;
  state.item.value = null;
  state.section.value = "tasks";
});

const flush = () => act(async () => { await new Promise((r) => setTimeout(r, 0)); });
const text = (el: Element | null | undefined) => el?.textContent ?? "";

test("History lists the days newest first, with how much was said and a preview, and pages back", async () => {
  state.go("history");
  await flush();
  await act(() => { render(h(ui.DayList, {}), box); });
  const rows = [...box.querySelectorAll(".hday")];
  assert.deepEqual(rows.map((r) => text(r.querySelector(".title"))), ["Today", "Yesterday"]);
  assert.equal(text(rows[0]!.querySelector(".id")), "09:00–10:30");
  assert.equal(text(rows[0]!.querySelector(".meta")), "2 exchanges · 1 update");
  assert.equal(text(rows[0]!.querySelector(".hprev")), "and the tests?");
  await act(() => { (box.querySelector(".hmore") as HTMLButtonElement).click(); });
  await flush();
  assert.deepEqual(calls.filter((c) => c.action === "conversations").map((c) => c.body.before), [undefined, "2026-10-07"]);
  assert.deepEqual([...box.querySelectorAll(".hday .title")].map(text), ["Today", "Yesterday", "Monday 5 October"]);
  assert.equal(box.querySelector(".hmore"), null);  // no older days left
  // Reopening refreshes page one and keeps older pages
  await act(() => hist.loadDays());
  assert.equal(box.querySelectorAll(".hday").length, 3);
});

test("a day opens to who said what, newest first, with the days either side", async () => {
  hist.days.value = DAYS;
  state.go("history", "2026-10-08");
  await act(() => { render(h(ui.HistorySection, {}), box); });
  await flush();
  assert.equal(calls.find((c) => c.action === "conversation")?.body.date, "2026-10-08");
  assert.equal(text(box.querySelector(".big")), "Today");
  assert.equal(text(box.querySelector(".hsum")), "Thursday 8 October · 2 exchanges · 1 update · 09:00–10:30 · newest first");
  const shown = [...box.querySelectorAll(".hx")];
  assert.deepEqual(shown.map((x) => text(x.querySelector(".hx-time"))), ["10:30", "09:05", "09:00"]);
  assert.equal(text(shown[0]!.querySelector(".ann .who")), "JARVIS · update");
  assert.equal(text(shown[0]!.querySelector(".tref")), "Task 12");  // task link button
  assert.equal(text(shown[1]!.querySelector(".you .who")), "Alex");
  assert.equal(text(shown[1]!.querySelector(".hx-yes code")), "make test");
  assert.equal(text(shown[1]!.querySelector(".jarvis .cut")), "Interrupted before replying");
  assert.equal(text(shown[2]!.querySelector(".jarvis p")), "Two meetings.");
  const [newer, older] = [...box.querySelectorAll<HTMLButtonElement>(".hsteps .btn")];
  assert.ok(newer!.disabled);
  assert.equal(text(older), "Yesterday");
  answers.conversation = { ...DAY, date: "2026-10-07", newer: "2026-10-08", older: null, entries: [] };
  await act(() => { older!.click(); });
  await flush();
  assert.equal(state.item.value, "2026-10-07");
  assert.equal(text(box.querySelector(".big")), "Yesterday");
});

test("a search shows the exchanges that mention its words, by day, and clears back to the days", async () => {
  hist.days.value = DAYS;
  await act(() => { render(h(ui.DayList, {}), box); });
  const field = box.querySelector<HTMLInputElement>(".hsearch input")!;
  await act(() => { field.value = "tests"; field.dispatchEvent(new Event("input")); });
  await act(() => { box.querySelector<HTMLFormElement>(".hsearch form")!.requestSubmit(); });
  await flush();
  assert.equal(calls.find((c) => c.action === "conversation_search")?.body.text, "tests");
  assert.match(text(box.querySelector(".grp")), /^2 matches for “tests”/);
  assert.deepEqual([...box.querySelectorAll(".hdate")].map(text), ["Today", "Yesterday"]);
  assert.deepEqual([...box.querySelectorAll("mark")].map(text), ["tests", "TESTS"]);
  await act(() => { box.querySelector<HTMLButtonElement>(".hdate")!.click(); });
  assert.equal(state.section.value, "history");
  assert.equal(state.item.value, "2026-10-08");
  await act(() => { box.querySelector<HTMLButtonElement>(".grp .act")!.click(); });
  assert.equal(box.querySelectorAll(".hday").length, 2);
});
