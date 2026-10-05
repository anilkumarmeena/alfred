// The control panel beside the voice screen: tasks, to-dos, projects, past conversations (state/history.ts) and
// settings, kept in signals and loaded from
// jarvis.panel. It follows the task runner's events on /api/events, which pop up a task's request for a yes (the
// approval card), refresh the task list and fill the activity list behind the bell.
import { computed, effect, signal } from "@preact/signals";
import { panelCall, Unauthorized } from "../lib/api";
import { build } from "../lib/build";
import { backoff, napper } from "../lib/retry";
import { jobItem, jobOf, type Quiet, type Schedule, type ScheduleDetail } from "../lib/schedule";
import { ACTIVITY_KEY, PASSCODE_KEY, SIDE_KEY, storage } from "../lib/storage";
import { day, loadDay, loadDays } from "./history";
import { loginShown, passcode, pushSub, setPasscode, showLogin } from "./store";

export type Section = "tasks" | "todos" | "projects" | "history" | "settings";
export type SettingsItem =
  "conn" | "accounts" | "memory" | "vocabulary" | "skills" | "approvals" | "about" | "prefs" | "heartbeat" | "notify" | "system"
  | "access" | "keys";

export interface Task {
  id: number; kind: string; title: string; project: string | null; status: string; created: number; updated: number;
  activity: string | null; question: string | null; spoken: string | null; changes: string | null; error: string | null;
  cost: number; seen: number; in_place: number; branch: string | null; worktree: boolean; requests_only: boolean;
  pending_edits: number; job?: number | null; job_title?: string | null;  // the scheduled job it ran for
  stalled?: Stalled | null;  // running, but no sign of progress for a while (jarvis.tasks.stall)
  command?: CommandRun | null;  // kind "command": a shell job, run with no model (jarvis.tasks.command_jobs)
  fixes?: number | null;  // a task fixing a command task that failed: that one
}
// How a command task runs and how it went: its folder, which attempt it is (a rerun after a fix is 2, `after` saying what
// failed and what fixed it), the task fixing it, and the end of its output
export interface CommandRun {
  folder: string; attempt?: number; timeout_minutes?: number; outcome?: string; code?: number; fix_task?: number;
  after?: { task: number; fix: number; failed: string; fixed: string }; said?: string; seconds: number;
  output_tail: string; log_path: string;
}
export interface Stalled { minutes: number; since: number }
export interface LogLine { at: number; kind: string; text: string }
export interface Edit { n: number; status: string; what: string; reason: string; needs_yes: string; result: string }
export interface TaskDetail extends Task { instruction: string; summary: string | null; log: LogLine[]; edits: Edit[] }
export interface Command { tool: string; command: string; folder: string; why: string }
// What answering a request for longer would remember (jarvis.safety.remembered): why it can't be, or what each
// breadth's rule covers ("exact" is empty when the command's words can't be pinned down)
export interface Remember { never: string; exact: string; kind: string }
export type Scope = "once" | "session" | "always";
export type Breadth = "exact" | "kind";
export interface Approval {
  id: number; task: number; created: number; expires: number; commands: Command[]; state: string; summary: string;
  title?: string; spoken?: string; remember?: Remember;
}
// A remembered approval: a kind of risky command that runs without asking, for a task's session or always
export interface Rule {
  id: number; scope: "session" | "always"; session: string | null; covers: string; who: string; example: string; via: string;
  created: number; uses: number; last_used: number | null;
}
export interface Todo { id: number; text: string; done: boolean; created: number; done_at: number | null }
export interface Project {
  name: string; path: string; aliases: string[]; about: string; in_place: boolean; jarvis: boolean; exists: boolean;
  git: string; changed: number | null;
}
export interface Account { label: string; email: string; note: string; chat: boolean; default: boolean; signed_in: boolean }
// Long-term memory's lines by section, and how much of each section's limit they take (jarvis.state.memory)
export type MemorySection = "user" | "environment";
export interface Memory extends Record<MemorySection, string[]> {
  limits: Record<MemorySection, { used: number; max: number }> & { fact: number };
}
// The speech vocabulary (jarvis.state.vocabulary): words speech recognition expects, with what it's misheard them as,
// and the project names it's also given
export interface Word { word: string; heard: string[]; date: string; learned: boolean }
export interface Vocabulary { entries: Word[]; projects: string[]; max: number; prompt: boolean }
// JARVIS's skills (jarvis.skills): how-tos it loads when a request matches, some with scripts that need approving
export interface Skill {
  name: string; description: string; source: string; kind: string; state: string; enabled: boolean;
  needs_approval: boolean; added: string; created: number; updated: number; scripts?: string[]; from?: string;
  problems?: string[];
}
export interface SkillReview {
  summary: string; risks: string[]; flags: string[]; verdict: string; partial: boolean; at: number; model: string;
  current: boolean;  // it covers the skill's files as they are now
}
export interface SkillDetail extends Skill {
  skill_md: string; cut: boolean; files: { path: string; size: number; script: boolean }[]; review: SkillReview | null;
}
export interface Device { id: string; kind: "web" | "app"; device: string; added: number | null; this: boolean }
// A connection (an MCP server), as jarvis.panel shows it: which of its keys are set, never what they are
export interface Server {
  name: string; label: string; enabled: boolean; reviewed: boolean; command: string; tools: Record<string, number>;
  tool_list: { name: string; access: string }[]; secrets: { var: string; set: boolean }[];
  sign_in: boolean; signed_in: boolean; source: string; verified: boolean; version: string; research: boolean;
  problem: string;
}
export interface Service { name: string; state: string; since?: number | null; exit?: number | null }
// A preference: what Settings keeps ("" for none), what applies then, and what .env sets, which wins over both
export interface Preference { value: string; default: string; env: string }
export type PrefKey = "name" | "timezone" | "voice" | "voice_speed" | "speech_gate" | "max_tasks" | "browser" | "browser_window"
  | "speaker_check" | "speaker_threshold" | "injection_check" | "injection_model";
// A voiceprint, as jarvis.panel shows it: never the embedding. current: made by the speaker model in use
export interface Voiceprint { name: string; seconds: number; consistency: number; via: string; created: number; current: boolean }
export interface Voiceprints { owner: Voiceprint | null; model: boolean; passage: string; seconds: number }
export interface Prefs { preferences: Record<PrefKey, Preference>; voices: string[] }
// The heartbeat's sources, as jarvis.tasks.heartbeat describes them: each setting with what applies now and its default
export interface SourceSetting { key: string; label: string; default: string; value: string }
export interface HeartbeatSource {
  id: string; kind: string; title: string; about: string; on: boolean; ready: boolean; added: string; settings: SourceSetting[];
}
export interface SourceKind { name: string; title: string; about: string; settings: SourceSetting[] }
export interface Heartbeat { sources: HeartbeatSource[]; kinds: SourceKind[] }
export interface System { services: Service[]; vault: { state: string; secrets?: number; problem?: string } }
// A login, as jarvis.panel shows it: never its passcode. this: the one this device connects with
export interface Login { name: string; created: number; used: number | null; this: boolean }
// One of JARVIS's own keys: where it comes from ("" when it isn't set), never what it is. required: can't be cleared
export interface JarvisKey { name: string; label: string; hint: string; required: boolean; source: "vault" | ".env" | "file" | "" }
export interface Access { logins: Login[]; keys: JarvisKey[] }
export interface Activity { id: number; at: number; text: string; kind: "ok" | "bad" | "warn" | "run"; task: number | null }

export const ACTIVE = ["queued", "running", "waiting"];
const WIDE = matchMedia("(min-width: 900px)");

// ── Where the panel is ──────────────────────────────────────────

export const wide = signal(WIDE.matches);  // desktop: the panel is a column beside the voice screen
WIDE.addEventListener("change", () => { wide.value = WIDE.matches; });
export const roomy = signal(false);  // the desktop's panel is wide enough for a list and the open item side by side
export const sideOff = signal(storage.get(SIDE_KEY) === "off");  // desktop: the panel column is hidden
export const sheet = signal<"closed" | "full" | "half">("closed");  // phone: the slide-up sheet
export const section = signal<Section>("tasks");
export const item = signal<string | null>(null);  // the open task's id, or the settings page
export const shown = computed(() => !!passcode.value && !loginShown.value);  // the panel needs the passcode
// Whether the panel is on screen at all
export const visible = computed(() => shown.value && (wide.value ? !sideOff.value : sheet.value !== "closed"));

export function setSideOff(off: boolean) {
  sideOff.value = off;
  storage.set(SIDE_KEY, off ? "off" : null);
}

// Open a section (and an item in it): the sheet on a phone, the panel column on a desktop.
export function go(sec: Section, open: string | null = null) {
  section.value = sec;
  item.value = open;
  if (wide.peek()) setSideOff(false);
  else if (sheet.peek() === "closed") sheet.value = "full";
  load(sec);
}
export function closeSheet() { sheet.value = "closed"; }
export function back() { item.value = null; }

export function openTask(id: number) {
  go("tasks", String(id));
  flash.value = { id, at: Date.now() };
}
export const flash = signal<{ id: number; at: number } | null>(null);  // a task card to draw the eye to

export function openSchedule(id: number) { go("tasks", jobItem(id)); }

// ── Data ────────────────────────────────────────────────────────

export const tasks = signal<Task[]>([]);
export const approvals = signal<Approval[]>([]);
export const runnerDown = signal("");
export const detail = signal<TaskDetail | null>(null);
export const schedules = signal<Schedule[]>([]);  // jobs still to run, active or paused, soonest first
export const jobDetail = signal<ScheduleDetail | null>(null);
export const quiet = signal<Quiet | null>(null);  // do not disturb, and Claude's usage, as the jobs list and Preferences show them
export const todos = signal<Todo[] | null>(null);
export const projects = signal<Project[] | null>(null);
export const memory = signal<Memory | null>(null);
export const vocabulary = signal<Vocabulary | null>(null);
export const skills = signal<Skill[] | null>(null);
export const about = signal<string | null>(null);
export const accounts = signal<Account[] | null>(null);
export const devices = signal<Device[] | null>(null);
export const servers = signal<Server[] | null>(null);
export const system = signal<System | null>(null);
export const access = signal<Access | null>(null);
export const prefs = signal<Prefs | null>(null);
export const rules = signal<Rule[] | null>(null);
export const voiceprints = signal<Voiceprints | null>(null);
export const heartbeat = signal<Heartbeat | null>(null);

// What waits on the user: requests for a yes, and tasks' own questions
export const questions = computed(() => tasks.value.filter((t) => t.status === "waiting" && !t.requests_only));
export const needYou = computed(() => approvals.value.length + questions.value.length);
// Finished since the user last looked
export const unseen = computed(() => tasks.value.filter((t) => !t.seen && !ACTIVE.includes(t.status)));

// A panel action, with its failure shown as a toast; null when it failed.
export async function act<T = any>(action: string, fields: Record<string, unknown> = {}, done?: string): Promise<T | null> {
  try {
    const result = await panelCall<T>(action, fields);
    if (done) toast(done);
    return result;
  } catch (e) {
    if (e instanceof Unauthorized) { setPasscode(null); showLogin(e.message); return null; }
    toast((e as Error).message || String(e));
    return null;
  }
}

export async function loadTasks() {
  try {
    const r = await panelCall<{ tasks: Task[]; approvals: Approval[]; schedules?: Schedule[] } & Partial<Quiet>>("tasks");
    tasks.value = r.tasks;
    approvals.value = r.approvals;
    schedules.value = r.schedules ?? [];
    quiet.value = r.usage ? { hold: r.hold ?? null, usage: r.usage } : null;
    const job = jobOf(item.peek());
    if (job != null && section.peek() === "tasks") loadSchedule(job);
    runnerDown.value = "";
    const first = r.approvals.find((a) => !tucked.has(a.id));
    if (first && ask.peek() == null) popAsk(first.id);
    if (ask.peek() != null && !r.approvals.some((a) => a.id === ask.peek())) ask.value = null;
  } catch (e) {
    if (e instanceof Unauthorized) { setPasscode(null); return showLogin(e.message); }
    runnerDown.value = (e as Error).message || String(e);
  }
}

export async function loadTask(id: number) {
  try {
    const t = await panelCall<TaskDetail>("task", { id });
    if (item.peek() === String(id)) detail.value = t;
  } catch (e) {
    if (item.peek() === String(id)) toast((e as Error).message || String(e));
  }
}

export async function loadSchedule(id: number) {
  try {
    const j = await panelCall<ScheduleDetail>("schedule", { id });
    if (jobOf(item.peek()) === id) jobDetail.value = j;
  } catch (e) {
    if (jobOf(item.peek()) === id) toast((e as Error).message || String(e));
  }
}

// Pause, resume or cancel a job; the list comes back changed.
export async function changeSchedule(action: "pause" | "resume" | "cancel", job: Schedule): Promise<boolean> {
  const done = { pause: `Paused ${job.title}`, resume: `Resumed ${job.title}`, cancel: `Cancelled ${job.title}` }[action];
  const r = await act<Schedule[]>(`schedule_${action}`, { id: job.id }, done);
  if (!r) return false;
  schedules.value = r;
  if (jobOf(item.peek()) === job.id) loadSchedule(job.id);
  return true;
}

// Do not disturb: on for `minutes`, until a time ("15:00"), or until resumed (neither); off with null.
export async function setHold(to: { minutes?: number; until?: string } | null): Promise<boolean> {
  const r = await act<Quiet>(to ? "hold_on" : "hold_off", to ?? {}, to ? undefined : "Do not disturb is off");
  if (!r) return false;
  quiet.value = r;
  if (to && r.hold) toast(`Do not disturb is ${r.hold.said}`);
  return true;
}

const SETTINGS: Record<SettingsItem, () => Promise<void>> = {
  conn: async () => { servers.value = await act("servers") ?? servers.peek(); },
  accounts: async () => { accounts.value = await act("accounts") ?? accounts.peek(); },
  memory: async () => { memory.value = await act("memory") ?? memory.peek(); },
  vocabulary: async () => { vocabulary.value = await act("vocabulary") ?? vocabulary.peek(); },
  skills: async () => { skills.value = await act("skills") ?? skills.peek(); },
  approvals: async () => { rules.value = await act("approval_rules") ?? rules.peek(); },
  about: async () => { const r = await act<{ text: string }>("about"); if (r) about.value = r.text; await loadPrefs(); },
  prefs: async () => {
    await Promise.all([
      loadPrefs(),
      act<Quiet>("hold_status").then((r) => { if (r) quiet.value = r; }),
      act<Voiceprints>("voiceprint").then((r) => { if (r) voiceprints.value = r; }),
    ]);
  },
  heartbeat: async () => { heartbeat.value = await act("heartbeat_sources") ?? heartbeat.peek(); },
  notify: async () => { devices.value = await act("devices", { endpoint: pushSub.peek()?.endpoint ?? "" }) ?? devices.peek(); },
  system: async () => { system.value = await act("system") ?? system.peek(); },
  access: async () => { access.value = await act("access") ?? access.peek(); },
  keys: async () => {},
};
// What the Settings list's own lines show: the system's health, the connections, how many skills are on, the
// voice, voice ID and do not disturb, how many heartbeat sources are on, and the logins and keys
const LISTED: SettingsItem[] = ["system", "conn", "skills", "prefs", "heartbeat", "access"];

async function loadPrefs() { prefs.value = await act<Prefs>("preferences") ?? prefs.peek(); }

// What a section shows, fetched afresh when it opens.
export function load(sec: Section, open: string | null = item.peek()) {
  if (sec === "tasks") loadTasks();
  else if (sec === "todos") act<Todo[]>("todos").then((r) => { if (r) todos.value = r; });
  else if (sec === "projects") act<Project[]>("projects").then((r) => { if (r) projects.value = r; });
  else if (sec === "history") {
    loadDays();
    if (open && day.peek()?.date === open) loadDay(open);  // a day already shown, refreshed; DayView loads a new one
  }
  else {
    for (const k of LISTED) SETTINGS[k]();
    if (open && open in SETTINGS && !LISTED.includes(open as SettingsItem)) SETTINGS[open as SettingsItem]();
  }
}

// The user has looked at the tasks section: finished tasks there count as heard about.
let seenTimer: ReturnType<typeof setTimeout> | undefined;
effect(() => {
  const looking = visible.value && section.value === "tasks", ids = unseen.value.map((t) => t.id);
  clearTimeout(seenTimer);
  if (!looking || !ids.length) return;
  seenTimer = setTimeout(async () => {
    if (await act("seen", { ids })) tasks.value = tasks.peek().map((t) => (ids.includes(t.id) ? { ...t, seen: 1 } : t));
  }, 2500);
});

// ── The approval card ───────────────────────────────────────────

export const ask = signal<number | null>(null);  // the request on the card
export const askGuard = signal(false);  // its buttons wait a moment, so a tap meant for something else can't answer
const tucked = new Set<number>();  // put off with Later: they wait in Tasks, and don't pop up again
let guardTimer: ReturnType<typeof setTimeout> | undefined, tuckTimer: ReturnType<typeof setTimeout> | undefined;
const TUCK_AFTER = 60_000;  // left alone this long, the card moves into Tasks

export function popAsk(id: number) {
  if (tucked.has(id)) return;
  ask.value = id;
  askGuard.value = true;
  clearTimeout(guardTimer); guardTimer = setTimeout(() => { askGuard.value = false; }, 700);
  clearTimeout(tuckTimer); tuckTimer = setTimeout(() => { if (ask.peek() === id) tuck(); }, TUCK_AFTER);
}

export function tuck() {
  const id = ask.peek();
  if (id == null) return;
  tucked.add(id);
  ask.value = null;
  toast("Moved to Tasks. Answer it there any time.");
}

const APPROVED: Record<Scope, string> = { once: "Approved once", session: "Approved for this task", always: "Approved, and remembered" };

// A yes or no to a request. A yes can go further (`remember`): for the task's session, or always at a breadth whose
// rule the card showed beside the button.
export async function answer(id: number, yes: boolean, remember: Scope = "once", breadth: Breadth = "exact") {
  if (ask.peek() === id) ask.value = null;
  const a = approvals.peek().find((x) => x.id === id);
  const r = await act<{ outcome: string; answered: boolean; remembered?: { rules: Rule[]; never?: string } | null }>(
    "answer", { id, decision: yes ? "approve" : "deny", ...(yes && remember !== "once" ? { remember, breadth } : {}) });
  if (!r) return;
  approvals.value = approvals.peek().filter((x) => x.id !== id);
  const kept = r.remembered, task = `task ${a?.task ?? ""}`;
  let said = yes ? `${APPROVED[remember]} · ${task} carries on` : `Denied · ${task} was told no`;
  if (kept && !kept.rules.length) said = `Approved once · ${task} carries on. Not remembered: ${kept.never ?? "it always asks"}`;
  toast(r.answered ? said : r.outcome);
  loadTasks();
  if (a && item.peek() === String(a.task)) loadTask(a.task);
  if (kept?.rules.length && rules.peek()) SETTINGS.approvals();
}

export async function forgetRule(rule: Rule) {
  const r = await act<Rule[]>("approval_rule_forget", { id: rule.id }, "Withdrawn. Commands it covered ask again.");
  if (r) rules.value = r;
}

// ── Activity, toasts, the palette ───────────────────────────────

// The bell: what JARVIS did lately, kept on the Mac (jarvis.state.activity) and shared by every device. Which entries
// this device has seen is its own: the newest id it showed, in localStorage.
export const activity = signal<Activity[]>([]);
export const activityOpen = signal(false);
const seenActivity = signal(Number(storage.get(ACTIVITY_KEY)) || 0);
export const activityUnread = computed(() => (activity.value[0]?.id ?? 0) > seenActivity.value);
const MAX_ACTIVITY = 200;  // as many as the Mac lists at once (activity.MOST)

function sawActivity() {
  const newest = activity.peek()[0]?.id ?? 0;
  if (newest <= seenActivity.peek()) return;
  seenActivity.value = newest;
  storage.set(ACTIVITY_KEY, String(newest));
}
export function toggleActivity(on = !activityOpen.peek()) {
  activityOpen.value = on;
  if (on) sawActivity();
}
export async function loadActivity() {
  const r = await act<Activity[]>("activity");
  if (r) { activity.value = r; if (activityOpen.peek()) sawActivity(); }
}
function addActivity(entry: Activity) {
  if (activity.peek().some((x) => x.id === entry.id)) return;
  activity.value = [entry, ...activity.peek()].slice(0, MAX_ACTIVITY);
  if (activityOpen.peek()) sawActivity();
}

// Over everything, the panel included. An action (e.g. Open Settings) gives it a button and more time.
export interface ToastAction { label: string; run: () => void }
export const toastText = signal("");
export const toastAction = signal<ToastAction | null>(null);
let toastTimer: ReturnType<typeof setTimeout> | undefined;
export function toast(text: string, action?: ToastAction) {
  toastText.value = text;
  toastAction.value = action ?? null;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { toastText.value = ""; toastAction.value = null; }, action ? 10000 : Math.max(3200, text.length * 60));
}

export const paletteOpen = signal(false);

// ── The runner's events ─────────────────────────────────────────

let detailTimer: ReturnType<typeof setTimeout> | undefined;

function refreshDetail(id: number) {  // at most once a second, however many events arrive
  if (item.peek() !== String(id) || section.peek() !== "tasks") return;
  clearTimeout(detailTimer);
  detailTimer = setTimeout(() => loadTask(id), 1000);
}

function handle(event: any) {
  switch (event.type) {
    case "hello":
      build.served(event.build);  // a newer build: the page reloads, or will once the voice session ends
      loadTasks();
      loadActivity();
      break;
    case "activity":
      addActivity(event.entry);
      break;
    case "approval":
      loadTasks().then(() => popAsk(event.approval.id));
      break;
    case "approval_answered":
      if (ask.peek() === event.approval?.id) ask.value = null;
      loadTasks();
      if (event.approval) refreshDetail(event.approval.task);
      break;
    case "status":
      loadTasks();
      if (event.task) refreshDetail(event.task.id);
      break;
    case "heartbeat":  // also how a reminder, or a watch's finding, is said: its job's next run has moved on
      if (section.peek() === "tasks") loadTasks();
      break;
    case "progress":
      if (event.task) {
        // progress is a sign of life, so a task that seemed stuck no longer does
        tasks.value = tasks.peek().map((t) => (t.id === event.task.id ? { ...t, activity: event.task.activity, stalled: null } : t));
        refreshDetail(event.task.id);
      }
      break;
    case "error":
      runnerDown.value = event.text;
      break;
  }
}

// Follow /api/events while there's a passcode, reconnecting with a growing pause when it drops (lib/retry.ts), and at
// once when the page comes back on screen or online.
let following = false;
const waiting = napper();
const SILENT_MS = 60000;  // jarvis.web pings every 25 s: this long without a word, the stream is dead (a sleep, a network change)
const STEADY_MS = 10000;
async function follow() {
  if (following) return;
  following = true;
  let attempt = 0;
  while (storage.get(PASSCODE_KEY)) {
    const opened = Date.now(), quit = new AbortController();
    let quiet: ReturnType<typeof setTimeout> | undefined;
    const heard = () => { clearTimeout(quiet); quiet = setTimeout(() => quit.abort(), SILENT_MS); };
    try {
      heard();
      const res = await fetch("/api/events", {
        method: "POST", headers: { "content-type": "application/json" },
        body: JSON.stringify({ passcode: storage.get(PASSCODE_KEY) }), signal: quit.signal,
      });
      if (res.status === 401) { setPasscode(null); showLogin("Wrong passcode."); break; }
      if (!res.ok || !res.body) throw new Error(`events: ${res.status}`);
      const reader = res.body.pipeThrough(new TextDecoderStream()).getReader();
      let buffered = "";
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        heard();
        buffered += value;
        const lines = buffered.split("\n");
        buffered = lines.pop() ?? "";
        for (const line of lines) if (line.trim()) { try { handle(JSON.parse(line)); } catch {} }
      }
    } catch {} finally { clearTimeout(quiet); quit.abort(); }
    // A stream that held a while was a good one: start the pauses over. One that ends at once (the runner down)
    // waits longer each time.
    if (Date.now() - opened > STEADY_MS) attempt = 0;
    await waiting.nap(backoff(attempt++));
  }
  following = false;
}

effect(() => { if (passcode.value) { follow(); loadTasks(); } });
// Back on screen: whatever happened meanwhile
document.addEventListener("visibilitychange", () => {
  if (!document.hidden && passcode.peek()) { waiting.wake(); load(section.peek()); }
});
window.addEventListener("online", () => waiting.wake());

// ── Time, as the panel says it ──────────────────────────────────

export function ago(at: number | null | undefined, now = Date.now()): string {
  if (!at) return "";
  const m = Math.round((now - at * 1000) / 60000);
  if (m < 1) return "just now";
  if (m < 60) return `${m} min ago`;
  const h = Math.round(m / 60);
  return h < 24 ? `${h} h ago` : `${Math.round(h / 24)} d ago`;
}

export function elapsed(since: number, now = Date.now()): string {
  const s = Math.max(0, Math.floor(now / 1000 - since));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
  return h ? `${h}h ${String(m).padStart(2, "0")}m` : `${m}m ${String(s % 60).padStart(2, "0")}s`;
}

export function hhmm(at: number): string {
  const d = new Date(at * 1000);
  return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
}

// 11:42 today, Mon 11:42 earlier in the week
export function when(at: number): string {
  const d = new Date(at * 1000), today = new Date().toDateString() === d.toDateString();
  return today ? hhmm(at) : `${d.toLocaleDateString(undefined, { weekday: "short" })} ${hhmm(at)}`;
}

export function lapses(expires: number, now = Date.now()): string {
  const m = Math.round((expires * 1000 - now) / 60000);
  return m <= 0 ? "lapsing now" : m < 60 ? `lapses in ${m} min` : `lapses in ${Math.round(m / 60)} h`;
}
