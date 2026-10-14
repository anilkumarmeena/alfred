// @vitest-environment jsdom
// Scheduled jobs: lib/schedule.ts and their views in panel/Tasks.tsx. Run: make test (or npm test in web/)
import assert from "node:assert/strict";
import { h, render } from "preact";
import { act } from "preact/test-utils";
import { afterEach, beforeAll, beforeEach, test } from "vitest";
import { at, jobItem, jobOf, kindLabel, nextRun, type Schedule, type ScheduleDetail } from "../src/lib/schedule";

// Tue 6 Oct 2026, 09:15 local
const NOW = new Date(2026, 9, 6, 9, 15).getTime();
const ts = (d: Date) => d.getTime() / 1000;

// ── Helpers ─────────────────────────────────────────────────────

test("when a job runs next is said as near as it is", () => {
  assert.equal(at(ts(new Date(2026, 9, 6, 9, 0)), NOW), "now");
  assert.equal(at(ts(new Date(2026, 9, 6, 17, 0)), NOW), "17:00");
  assert.equal(at(ts(new Date(2026, 9, 7, 9, 0)), NOW), "tomorrow 09:00");
  assert.equal(at(ts(new Date(2026, 9, 9, 9, 0)), NOW), "Fri 09:00");
  assert.equal(at(ts(new Date(2026, 9, 20, 8, 30)), NOW), "20 Oct 08:30");
  assert.equal(nextRun({ status: "active", next_run: ts(new Date(2026, 9, 6, 17, 0)) }, NOW), "next 17:00");
  assert.equal(nextRun({ status: "paused", next_run: ts(new Date(2026, 9, 6, 17, 0)) }, NOW), "paused");
  assert.equal(nextRun({ status: "active", next_run: null }, NOW), "no run to come");
});

test("kinds and items", () => {
  assert.deepEqual(["reminder", "task", "watch"].map(kindLabel), ["Reminder", "Scheduled task", "Watch"]);
  assert.equal(jobOf(jobItem(12)), 12);
  assert.equal(jobOf("12"), null);  // task item, not a job
  assert.equal(jobOf(null), null);
});

// ── Tasks page ──────────────────────────────────────────────────

const JOB: Schedule = {
  id: 3, kind: "task", title: "Morning brief", text: "Give me a **morning brief**.", status: "active",
  next_run: ts(new Date(Date.now() + 3600_000)), last_run: null, until: null, runs: 2,
  last_result: "Two meetings and **one** urgent email.", task_kind: "research", project: null,
  when: "every weekday at 09:00", check: "", does: "", only_changes: 0, notepad: null, folder: null,
};
const WATCH: Schedule = { ...JOB, id: 4, kind: "watch", title: "Deploy live", status: "paused", task_kind: null, check: "JARVIS checks the page: https://app.example" };

let calls: { action: string; body: any }[] = [];
let answers: Record<string, unknown> = {};
let ui: typeof import("../src/components/panel/Tasks");
let state: typeof import("../src/state/panel");

beforeAll(async () => {
  // Browser APIs the panel module needs that jsdom lacks
  (globalThis as any).matchMedia = () => ({ matches: false, addEventListener() {}, removeEventListener() {} });
  (globalThis as any).confirm = () => true;
  Element.prototype.scrollIntoView = () => {};
  globalThis.fetch = (async (url: string, init: any) => {
    const action = String(url).split("/").pop()!, body = JSON.parse(init?.body ?? "{}");
    calls.push({ action, body });
    return new Response(JSON.stringify({ result: answers[action] ?? null }), { status: 200 });
  }) as any;
  state = await import("../src/state/panel");
  ui = await import("../src/components/panel/Tasks");
});

let box: HTMLElement;
afterEach(() => { render(null, box); });  // unmount so signals don't leak between tests
beforeEach(() => {
  calls = [];
  answers = {};
  box = document.createElement("div");
  document.body.replaceChildren(box);
  state.schedules.value = [JOB, WATCH];
  state.tasks.value = [{
    id: 21, kind: "research", title: "Morning brief", project: null, status: "done", created: 1, updated: 2,
    activity: null, question: null, spoken: null, changes: null, error: null, cost: 0, seen: 1, in_place: 0,
    branch: null, worktree: false, requests_only: false, pending_edits: 0, job: 3, job_title: "Morning brief",
  }, {
    id: 22, kind: "research", title: "Look something up", project: null, status: "done", created: 1, updated: 2,
    activity: null, question: null, spoken: null, changes: null, error: null, cost: 0, seen: 1, in_place: 0,
    branch: null, worktree: false, requests_only: false, pending_edits: 0, job: null, job_title: null,
  }];
  state.item.value = null;
  state.quiet.value = null;
  state.runnerDown.value = "";
});

const flush = () => act(async () => { await new Promise((r) => setTimeout(r, 0)); });

test("Scheduled comes after Running, a row per job with its state light, kind, next run and buttons", async () => {
  await act(() => { render(h(ui.TaskList, {}), box); });
  assert.deepEqual([...box.querySelectorAll(".grp")].map((g) => g.textContent), ["Running", "Scheduled", "Recent"]);
  const rows = [...box.querySelectorAll(".sched")];
  assert.deepEqual(rows.map((r) => r.querySelector(".title")?.textContent), ["Morning brief", "Deploy live"]);
  assert.match(rows[0]!.querySelector(".meta")!.textContent!, /^Scheduled task · next /);
  assert.equal(rows[1]!.querySelector(".meta")!.textContent, "Watch · paused");
  assert.deepEqual(rows.map((r) => r.querySelector(".dot")?.className), ["dot ok", "dot off"]);
  assert.deepEqual(rows.map((r) => [...r.querySelectorAll("button.ico")].map((b) => b.getAttribute("title"))),
    [["Pause", "Cancel"], ["Resume", "Cancel"]]);
  assert.ok(rows[1]!.classList.contains("paused"));
});

test("what waits on the user comes first, under a summary line", async () => {
  state.tasks.value = [...state.tasks.value, {
    id: 23, kind: "research", title: "Pick a venue", project: null, status: "waiting", created: 1, updated: 2,
    activity: null, question: "Which one?", spoken: null, changes: null, error: null, cost: 0, seen: 1, in_place: 0,
    branch: null, worktree: false, requests_only: false, pending_edits: 0, job: null, job_title: null,
  }];
  await act(() => { render(h(ui.TaskList, {}), box); });
  assert.equal(box.querySelector(".tsum")?.textContent, "0 running · 1 needs you");
  assert.deepEqual([...box.querySelectorAll(".grp")].map((g) => g.textContent), ["Waiting for you", "Running", "Scheduled", "Recent"]);
});

test("with nothing scheduled it says how to schedule something, and there's no form", async () => {
  state.schedules.value = [];
  await act(() => { render(h(ui.TaskList, {}), box); });
  assert.ok([...box.querySelectorAll(".empty")].some((e) => /Nothing scheduled\. Ask JARVIS/.test(e.textContent!)));
  assert.equal(box.querySelector(".scheds"), null);
  assert.equal(box.querySelector("form, input, textarea"), null);  // jobs are created by voice only
});

test("tasks a job started show the job's name on a row of its own", async () => {
  await act(() => { render(h(ui.TaskList, {}), box); });
  const cards = [...box.querySelectorAll("button.task")];
  assert.equal(cards.length, 2);
  assert.equal(cards[0]!.querySelector(".jobmark")?.textContent, "Morning brief");
  assert.equal(cards[0]!.querySelector(".jobmark")?.getAttribute("title"), "Scheduled: Morning brief");
  assert.equal(cards[1]!.querySelector(".jobmark"), null);
});

test("pause, resume and cancel on a job's page ask JARVIS and show the list it sends back", async () => {
  const page = document.createElement("div");
  document.body.append(page);
  await act(() => { render(h(ui.TaskList, {}), box); });
  state.item.value = "job-3";
  answers.schedule = { ...JOB, recent: [], runs_problem: "" };
  await act(() => { render(h(ui.ScheduleView, { id: 3 }), page); });
  await flush();
  const button = (text: string) => [...page.querySelectorAll("button.btn")].find((b) => b.textContent === text) as HTMLButtonElement;

  answers.schedule_pause = [{ ...JOB, status: "paused" }, WATCH];
  answers.schedule = { ...JOB, status: "paused", recent: [], runs_problem: "" };
  await act(() => { button("Pause").click(); });
  await flush();
  assert.deepEqual(calls.filter((c) => c.action.startsWith("schedule_")).map((c) => [c.action, c.body.id]), [["schedule_pause", 3]]);
  assert.equal(box.querySelector(".sched .meta")!.textContent, "Scheduled task · paused");
  assert.ok(button("Resume"));

  answers.schedule_cancel = [WATCH];
  await act(() => { button("Cancel").click(); });
  await flush();
  assert.equal(calls.filter((c) => c.action === "schedule_cancel").length, 1);
  assert.deepEqual([...box.querySelectorAll(".sched .title")].map((t) => t.textContent), ["Deploy live"]);
  render(null, page);
});

test("a job opens to its runs and results, Markdown rendered", async () => {
  const detail: ScheduleDetail = {
    ...JOB, recent: [
      { id: 21, status: "done", created: 100, updated: 160, hidden: false, result: "Two meetings and **one** urgent email." },
      { id: 19, status: "done", created: 50, updated: 70, hidden: true, result: "nothing to report." },
      { id: 17, status: "failed", created: 20, updated: 30, hidden: true, result: "couldn't check" },
    ], runs_problem: "",
  };
  answers.schedule = detail;
  await act(() => { render(h(ui.TaskList, {}), box); });
  await act(() => { (box.querySelector(".sched .open") as HTMLButtonElement).click(); });
  assert.equal(state.item.value, "job-3");
  assert.equal(state.section.value, "tasks");

  await act(() => { render(h(ui.ScheduleView, { id: 3 }), box); });
  await flush();
  assert.equal(box.querySelector(".big")?.textContent, "Morning brief");
  assert.equal(box.querySelector(".summary strong")?.textContent, "morning brief");  // instruction rendered as Markdown
  const runs = [...box.querySelectorAll("button.task.run")];
  assert.equal(runs.length, 3);
  assert.equal(runs[0]!.querySelector(".meta strong")?.textContent, "one");
  assert.deepEqual(runs.map((r) => r.querySelector(".tag")?.textContent), ["done", "nothing new", "failed"]);
  await act(() => { (runs[0] as HTMLButtonElement).click(); });
  assert.equal(state.item.value, "21");  // a run opens its task
});

test("a task job's view shows its notepad for the next run, and when it speaks", async () => {
  answers.schedule = { ...JOB, only_changes: 1, notepad: { at: 100, text: "PRs seen: 12, 14." }, recent: [], runs_problem: "" };
  state.item.value = "job-3";
  await act(() => { render(h(ui.ScheduleView, { id: 3 }), box); });
  await flush();
  const labels = [...box.querySelectorAll(".lbl")].map((l) => l.textContent);
  assert.ok(labels.some((l) => l?.startsWith("Notepad for the next run")));
  assert.match(box.textContent!, /PRs seen: 12, 14\./);
  assert.match(box.textContent!, /only when there's something new/);
});

const USAGE = { said: "Claude hasn't reported anything.", waiting: "" };

test("do not disturb lives in its Settings page only, not above the jobs", async () => {
  state.quiet.value = { hold: { since: 1, until: 3601, said: "on until 10:15" }, usage: USAGE };
  await act(() => { render(h(ui.TaskList, {}), box); });
  assert.doesNotMatch(box.textContent!, /Do not disturb/);
  assert.equal(box.querySelector(".holdbar"), null);
});

test("do not disturb is turned on in its Settings page, for an hour, and off with its switch", async () => {
  const { SettingsPage } = await import("../src/components/panel/Settings");
  state.quiet.value = { hold: null, usage: USAGE };
  await act(() => { render(h(SettingsPage, { page: "dnd" }), box); });
  const bar = box.querySelector(".holdbar")!;
  assert.deepEqual([...bar.querySelectorAll("button")].map((b) => b.textContent), ["1 hour", "Until then"]);
  answers.hold_on = { hold: { since: 1, until: 3601, said: "on until 10:15" }, usage: USAGE };
  await act(() => { (bar.querySelector("button") as HTMLButtonElement).click(); });
  await flush();
  assert.deepEqual(calls.map((c) => [c.action, c.body.minutes]), [["hold_on", 60]]);
  const sw = box.querySelector('button.switch[aria-label="Do not disturb"]') as HTMLButtonElement;
  assert.equal(sw.getAttribute("aria-checked"), "true");

  answers.hold_off = { hold: null, usage: USAGE };
  await act(() => { sw.click(); });
  await flush();
  assert.equal(calls.at(-1)!.action, "hold_off");
  assert.equal(sw.getAttribute("aria-checked"), "false");
});

test("scheduled runs waiting for Claude's usage to reset are said above the jobs", async () => {
  state.quiet.value = { hold: null, usage: { said: "The five-hour limit is used up.", waiting: "Scheduled tasks wait for its reset at 3 PM." } };
  await act(() => { render(h(ui.TaskList, {}), box); });
  assert.equal(box.querySelector(".grp + .note.amber")!.textContent, "The five-hour limit is used up. Scheduled tasks wait for its reset at 3 PM.");
});
