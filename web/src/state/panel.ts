// Control panel state (tasks, to-dos, projects, history, settings) as signals, loaded via jarvis.panel.
// Follows runner events on /api/events: approval card pop-ups, task list refreshes, the activity bell.
import { computed, effect, signal } from "@preact/signals";
import { panelCall, Unauthorized } from "../lib/api";
import { build } from "../lib/build";
import { backoff, napper } from "../lib/retry";
import { jobItem, jobOf, type Quiet, type Schedule, type ScheduleDetail } from "../lib/schedule";
import { ACTIVITY_KEY, PASSCODE_KEY, SIDE_KEY, storage } from "../lib/storage";
import { day, loadDay, loadDays } from "./history";
import { loginShown, passcode, pushSub, setPasscode, showLogin } from "./store";

export type Section = "tasks" | "todos" | "projects" | "jarvis" | "history" | "settings";  // jarvis: what it can do and knows
export type SettingsItem =
  "conn" | "accounts" | "memory" | "vocabulary" | "skills" | "approvals" | "about" | "prefs" | "dnd" | "safety"
  | "notify" | "system" | "access" | "keys" | "machines";

export interface Task {
  id: number; kind: string; title: string; project: string | null; status: string; created: number; updated: number;
  activity: string | null; question: string | null; spoken: string | null; changes: string | null; error: string | null;
  cost: number; seen: number; in_place: number; branch: string | null; worktree: boolean; requests_only: boolean;
  pending_edits: number; job?: number | null; job_title?: string | null;  // scheduled job it ran for
  stalled?: Stalled | null;  // running with no progress for a while
  command?: CommandRun | null;  // kind "command": shell job, no model
  fixes?: number | null;  // id of the failed command task this one fixes
  machine?: string;  // the other machine it runs on (jarvis.node); absent = this computer
}
// A command task's run. attempt is 2+ for a rerun after a fix (`after` says what failed and what fixed it).
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
// What remembering an approval would cover: `never` = why it can't be; per-breadth rule text ("exact" empty if
// the command can't be pinned down)
export interface Remember { never: string; exact: string; kind: string }
export type Scope = "once" | "session" | "always";
export type Breadth = "exact" | "kind";
export interface Approval {
  id: number; task: number; created: number; expires: number; commands: Command[]; state: string; summary: string;
  title?: string; spoken?: string; remember?: Remember;
}
// Remembered approval: commands it covers run without asking, for a task's session or always
export interface Rule {
  id: number; scope: "session" | "always"; session: string | null; covers: string; who: string; example: string; via: string;
  created: number; uses: number; last_used: number | null;
}
export interface Todo { id: number; text: string; done: boolean; created: number; done_at: number | null }
export interface Project {
  name: string; path: string; aliases: string[]; about: string; in_place: boolean; jarvis: boolean; exists: boolean;
  git: string | null; changed: number | null;
  machine?: string;  // the other machine it lives on; its folder is there
}
export interface Account { label: string; email: string; note: string; chat: boolean; default: boolean; signed_in: boolean }
// The Google OAuth client, for the accounts page: kind "installed" (Desktop app) or "web"; `paste`: a sign-in started
// here ends on a page that doesn't load, whose address is pasted back; `missing`: redirect URIs a Web client lacks.
// `kind` is "" until a client is set.
export interface GoogleSetup {
  kind: "" | "installed" | "web"; apis: { name: string; url: string; chat: boolean }[]; redirects: string[]; missing: string[];
  paste: boolean;
}
// `next`: the narrower scope step a refused Chat sign-in can try instead ("" when there's none)
export interface SignIn { url: string; state: string; paste: boolean; next: string }
// One Google API after a sign-in: "off" means not enabled in the client's Cloud project (`fix` turns it on)
export interface ApiCheck { name: string; state: "ok" | "off" | "denied" | "error"; fix: string; why: string }
export interface SignInResult {
  state: "waiting" | "done" | "refused" | "failed" | "old"; label?: string; said?: string; email?: string;
  checks?: ApiCheck[]; notes?: string[]; accounts?: Account[];
}
// Long-term memory lines by section, with per-section usage
export type MemorySection = "user" | "environment";
export interface Memory extends Record<MemorySection, string[]> {
  limits: Record<MemorySection, { used: number; max: number }> & { fact: number };
}
// Speech vocabulary: expected words with their mishearings, plus project names fed to recognition
export interface Word { word: string; heard: string[]; date: string; learned: boolean }
export interface Vocabulary { entries: Word[]; projects: string[]; max: number; prompt: boolean }
// Skills: how-tos loaded when a request matches; some have scripts that need approval
export interface Skill {
  name: string; description: string; source: string; kind: string; state: string; enabled: boolean;
  needs_approval: boolean; added: string; created: number; updated: number; scripts?: string[]; from?: string;
  problems?: string[];
}
export interface SkillReview {
  summary: string; risks: string[]; flags: string[]; verdict: string; partial: boolean; at: number; model: string;
  current: boolean;  // matches the skill's current files
}
export interface SkillDetail extends Skill {
  skill_md: string; cut: boolean; files: { path: string; size: number; script: boolean }[]; review: SkillReview | null;
}
export interface Device { id: string; kind: "web" | "app"; device: string; added: number | null; this: boolean }
// An MCP server connection. Secrets report set/unset only, never values
export interface Server {
  name: string; label: string; enabled: boolean; reviewed: boolean; command: string; tools: Record<string, number>;
  tool_list: { name: string; access: string }[]; secrets: { var: string; set: boolean }[];
  sign_in: boolean; signed_in: boolean; source: string; verified: boolean; version: string; research: boolean;
  problem: string;
}
export interface Service { name: string; state: string; since?: number | null; exit?: number | null }
// A preference: Settings value ("" = none), default, and .env value (wins over both)
export interface Preference { value: string; default: string; env: string }
export type PrefKey = "name" | "timezone" | "voice" | "voice_speed" | "speech_gate" | "max_tasks" | "browser" | "browser_window"
  | "speaker_check" | "speaker_threshold" | "injection_check" | "injection_model" | "quiet_hours";
// Voiceprint metadata (never the embedding). current: made by the speaker model in use
export interface Voiceprint { name: string; seconds: number; consistency: number; via: string; created: number; current: boolean }
export interface Voiceprints { owner: Voiceprint | null; model: boolean; passage: string; seconds: number }
export interface Prefs { preferences: Record<PrefKey, Preference>; voices: string[] }
export interface System { services: Service[]; vault: { state: string; secrets?: number; problem?: string } }
// A login (never its passcode). this: the one this device uses; owner: the owner's, which alone pairs machines
export interface Login { name: string; created: number; used: number | null; this: boolean; owner: boolean }
// A JARVIS key: its source ("" = unset), never its value. required: can't be cleared
export interface JarvisKey { name: string; label: string; hint: string; required: boolean; source: "vault" | ".env" | "file" | "" }
export interface Access { logins: Login[]; keys: JarvisKey[] }
export interface Activity { id: number; at: number; text: string; kind: "ok" | "bad" | "warn" | "run"; task: number | null }
// Another computer JARVIS sends heavy work to. online: it answered a ping just now; pending: awaiting confirmation
export interface Machine {
  id: number; name: string; aliases: string[]; status: "pending" | "paired"; fingerprint: string; host: string; os: string;
  cores: number; ram_gb: number; tools: string[]; version: string; up_to_date: boolean; last_seen: number | null;
  online: boolean; usage: string; free?: number; max_tasks?: number; running?: number; load?: number | null;
  updating?: boolean; update_problem?: string; takes_work?: boolean;
}
// Per job kind: "here", "auto" (let JARVIS choose) or a machine's id. can_pair: this login is the owner's
export interface Machines {
  machines: Machine[]; placement: Record<string, string>; kinds: Record<string, string>; commit: string; fingerprint: string;
  owner: string; can_pair: boolean;
}
export interface PairingCode { code: string; expires: number; main_fingerprint: string }

export const ACTIVE = ["queued", "running", "waiting"];
const WIDE = matchMedia("(min-width: 900px)");

// ── Layout ──────────────────────────────────────────────────────

export const wide = signal(WIDE.matches);  // desktop: panel is a column beside the voice screen
WIDE.addEventListener("change", () => { wide.value = WIDE.matches; });
export const roomy = signal(false);  // desktop panel fits list and open item side by side
export const home = signal(storage.get(SIDE_KEY) !== "on");  // desktop: the call (Home) rather than a section page
export const sheet = signal<"closed" | "full" | "half">("closed");  // phone: slide-up sheet
export const section = signal<Section>("tasks");
export const item = signal<string | null>(null);  // open task id, job item or settings page
export const shown = computed(() => !!passcode.value && !loginShown.value);
export const visible = computed(() => shown.value && (wide.value ? !home.value : sheet.value !== "closed"));

// Desktop: Home (the call) or the open section's page. Kept across reloads.
export function setHome(on: boolean) {
  home.value = on;
  storage.set(SIDE_KEY, on ? null : "on");
}

// Open a section (and optionally an item), revealing the sheet or the page.
export function go(sec: Section, open: string | null = null) {
  section.value = sec;
  item.value = open;
  if (wide.peek()) setHome(false);
  else if (sheet.peek() === "closed") sheet.value = "full";
  load(sec);
}
export function closeSheet() { sheet.value = "closed"; }
export function back() { item.value = null; }

export function openTask(id: number) {
  go("tasks", String(id));
  flash.value = { id, at: Date.now() };
}
export const flash = signal<{ id: number; at: number } | null>(null);  // task card to highlight

export function openSchedule(id: number) { go("tasks", jobItem(id)); }

// ── Data ────────────────────────────────────────────────────────

export const tasks = signal<Task[]>([]);
export const approvals = signal<Approval[]>([]);
export const runnerDown = signal("");
export const detail = signal<TaskDetail | null>(null);
export const schedules = signal<Schedule[]>([]);  // pending jobs (active or paused), soonest first
export const jobDetail = signal<ScheduleDetail | null>(null);
export const quiet = signal<Quiet | null>(null);  // do-not-disturb and Claude usage
export const todos = signal<Todo[] | null>(null);
export const projects = signal<Project[] | null>(null);
export const memory = signal<Memory | null>(null);
export const vocabulary = signal<Vocabulary | null>(null);
export const skills = signal<Skill[] | null>(null);
export const about = signal<string | null>(null);
export const accounts = signal<Account[] | null>(null);
export const googleSetup = signal<GoogleSetup | null>(null);
export async function loadGoogleSetup() {
  const r = await act<GoogleSetup>("google_setup", { origin: location.origin });
  if (r) googleSetup.value = r;
}
export const devices = signal<Device[] | null>(null);
export const servers = signal<Server[] | null>(null);
export const system = signal<System | null>(null);
export const access = signal<Access | null>(null);
export const prefs = signal<Prefs | null>(null);
export const rules = signal<Rule[] | null>(null);
export const voiceprints = signal<Voiceprints | null>(null);
export const machines = signal<Machines | null>(null);

// Waiting on the user: approval requests and task questions
export const questions = computed(() => tasks.value.filter((t) => t.status === "waiting" && !t.requests_only));
export const needYou = computed(() => approvals.value.length + questions.value.length);
// Finished and not yet seen
export const unseen = computed(() => tasks.value.filter((t) => !t.seen && !ACTIVE.includes(t.status)));

// Run a panel action; on failure toast the error and return null.
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

// To-dos come from this device's own actions or from the event stream (voice, tasks, other devices). Open items the
// stream brings that this device didn't add are fresh for a while: the list highlights them.
const FRESH_MS = 10000;
export const freshTodos = signal<ReadonlySet<number>>(new Set());
let ownTodoCalls = 0;  // in flight: the stream may bring their result first

export function setTodos(next: Todo[]) {
  const before = todos.peek();
  todos.value = next;
  if (!before || ownTodoCalls) return;
  const known = new Set(before.map((t) => t.id)), added = next.filter((t) => !t.done && !known.has(t.id)).map((t) => t.id);
  if (!added.length) return;
  freshTodos.value = new Set([...freshTodos.peek(), ...added]);
  setTimeout(() => unfresh(...added), FRESH_MS);
}
export function unfresh(...ids: number[]) {
  const now = freshTodos.peek();
  if (ids.some((id) => now.has(id))) freshTodos.value = new Set([...now].filter((id) => !ids.includes(id)));
}
// A to-do action from this device; the list it returns is shown, never highlighted
export async function todoAct(action: string, fields: Record<string, unknown> = {}, done?: string): Promise<Todo[] | null> {
  ownTodoCalls++;
  try {
    const r = await act<Todo[]>(action, fields, done);
    if (r) todos.value = r;
    return r;
  } finally { ownTodoCalls--; }
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

// Pause, resume or cancel a job; the server returns the updated list.
export async function changeSchedule(action: "pause" | "resume" | "cancel", job: Schedule): Promise<boolean> {
  const done = { pause: `Paused ${job.title}`, resume: `Resumed ${job.title}`, cancel: `Cancelled ${job.title}` }[action];
  const r = await act<Schedule[]>(`schedule_${action}`, { id: job.id }, done);
  if (!r) return false;
  schedules.value = r;
  if (jobOf(item.peek()) === job.id) loadSchedule(job.id);
  return true;
}

// Do not disturb: for `minutes`, `until` a time ("15:00"), or indefinitely (neither); null turns it off.
export async function setHold(to: { minutes?: number; until?: string } | null): Promise<boolean> {
  const r = await act<Quiet>(to ? "hold_on" : "hold_off", to ?? {}, to ? undefined : "Do not disturb is off");
  if (!r) return false;
  quiet.value = r;
  if (to && r.hold) toast(`Do not disturb is ${r.hold.said}`);
  return true;
}

const SETTINGS: Record<SettingsItem, () => Promise<void>> = {
  conn: async () => { servers.value = await act("servers") ?? servers.peek(); },
  accounts: async () => {
    await Promise.all([act<Account[]>("accounts").then((r) => { if (r) accounts.value = r; }), loadGoogleSetup()]);
  },
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
  dnd: async () => {
    await Promise.all([loadPrefs(), act<Quiet>("hold_status").then((r) => { if (r) quiet.value = r; })]);
  },
  safety: loadPrefs,
  notify: async () => { devices.value = await act("devices", { endpoint: pushSub.peek()?.endpoint ?? "" }) ?? devices.peek(); },
  system: async () => { system.value = await act("system") ?? system.peek(); },
  access: async () => { access.value = await act("access") ?? access.peek(); },
  keys: async () => {},
  machines: async () => { machines.value = await act("machines") ?? machines.peek(); },
};
// Loaded whenever Settings or JARVIS opens, for the summaries on their lists
const LISTED: SettingsItem[] = ["system", "conn", "skills", "prefs", "access", "machines"];

async function loadPrefs() { prefs.value = await act<Prefs>("preferences") ?? prefs.peek(); }

// Fetch a section's data afresh.
export function load(sec: Section, open: string | null = item.peek()) {
  if (sec === "tasks") loadTasks();
  else if (sec === "todos") act<Todo[]>("todos").then((r) => { if (r) setTodos(r); });
  else if (sec === "projects") act<Project[]>("projects").then((r) => { if (r) projects.value = r; });
  else if (sec === "history") {
    loadDays();
    if (open && day.peek()?.date === open) loadDay(open);  // refresh the shown day; DayView loads new ones
  }
  else {
    for (const k of LISTED) SETTINGS[k]();
    if (open && open in SETTINGS && !LISTED.includes(open as SettingsItem)) SETTINGS[open as SettingsItem]();
  }
}

// Viewing the tasks section marks finished tasks as seen.
let seenTimer: ReturnType<typeof setTimeout> | undefined;
effect(() => {
  const looking = visible.value && section.value === "tasks", ids = unseen.value.map((t) => t.id);
  clearTimeout(seenTimer);
  if (!looking || !ids.length) return;
  seenTimer = setTimeout(async () => {
    if (await act("seen", { ids })) tasks.value = tasks.peek().map((t) => (ids.includes(t.id) ? { ...t, seen: 1 } : t));
  }, 2500);
});

// ── Approval card ───────────────────────────────────────────────

export const ask = signal<number | null>(null);  // approval id on the card
export const askGuard = signal(false);  // briefly disables buttons so a stray tap can't answer
const tucked = new Set<number>();  // deferred with Later: stay in Tasks, never pop up again
let guardTimer: ReturnType<typeof setTimeout> | undefined, tuckTimer: ReturnType<typeof setTimeout> | undefined;
const TUCK_AFTER = 60_000;  // unanswered card moves into Tasks after this

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

// Approve or deny. A yes may be remembered for the task's session or always, at the given breadth.
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

// ── Activity, toasts, palette ───────────────────────────────────

// Activity bell: shared across devices, stored on JARVIS's computer. Read state is per device (newest seen id in localStorage).
export const activity = signal<Activity[]>([]);
export const activityOpen = signal(false);
const seenActivity = signal(Number(storage.get(ACTIVITY_KEY)) || 0);
export const activityUnread = computed(() => (activity.value[0]?.id ?? 0) > seenActivity.value);
const MAX_ACTIVITY = 200;  // matches the server's list limit

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

// Toast over everything. An action adds a button and a longer timeout.
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

// ── Runner events ───────────────────────────────────────────────

let detailTimer: ReturnType<typeof setTimeout> | undefined;

function refreshDetail(id: number) {  // debounced to 1 s
  if (item.peek() !== String(id) || section.peek() !== "tasks") return;
  clearTimeout(detailTimer);
  detailTimer = setTimeout(() => loadTask(id), 1000);
}

function handle(event: any) {
  switch (event.type) {
    case "hello":
      build.served(event.build);  // newer build: reload now or after the voice session
      loadTasks();
      loadActivity();
      if (todos.peek()) load("todos");  // changes while the stream was down
      break;
    case "todos":
      if (todos.peek()) setTodos(event.todos);  // else loaded when To-dos opens
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
    case "alert":  // notices, reminders and watch findings: a job's next run may have moved
      if (section.peek() === "tasks") loadTasks();
      break;
    case "progress":
      if (event.task) {
        // progress clears a stall
        tasks.value = tasks.peek().map((t) => (t.id === event.task.id ? { ...t, activity: event.task.activity, stalled: null } : t));
        refreshDetail(event.task.id);
      }
      break;
    case "error":
      runnerDown.value = event.text;
      break;
  }
}

// Follow /api/events while there's a passcode. Reconnect with backoff, or immediately on return to screen or network.
let following = false;
const waiting = napper();
const SILENT_MS = 60000;  // server pings every 25 s; silence this long = dead stream (sleep, network change)
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
    // A stream that held resets the backoff; one that fails at once (runner down) backs off further.
    if (Date.now() - opened > STEADY_MS) attempt = 0;
    await waiting.nap(backoff(attempt++));
  }
  following = false;
}

effect(() => { if (passcode.value) { follow(); loadTasks(); } });
// Back on screen: catch up
document.addEventListener("visibilitychange", () => {
  if (!document.hidden && passcode.peek()) { waiting.wake(); load(section.peek()); }
});
window.addEventListener("online", () => waiting.wake());

// ── Time formatting ─────────────────────────────────────────────

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

// "11:42" today, "Mon 11:42" otherwise
export function when(at: number): string {
  const d = new Date(at * 1000), today = new Date().toDateString() === d.toDateString();
  return today ? hhmm(at) : `${d.toLocaleDateString(undefined, { weekday: "short" })} ${hhmm(at)}`;
}

export function lapses(expires: number, now = Date.now()): string {
  const m = Math.round((expires * 1000 - now) / 60000);
  return m <= 0 ? "lapsing now" : m < 60 ? `lapses in ${m} min` : `lapses in ${Math.round(m / 60)} h`;
}
