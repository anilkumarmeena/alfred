// Scheduled jobs as the panel says them (jarvis.tasks.schedules): what kind each is, when it runs next, and the
// panel's item for one. Jobs are set by voice; the panel only lists, pauses, resumes and cancels them.

export interface Schedule {
  id: number; kind: "reminder" | "task" | "watch" | "command"; title: string; text: string;
  folder: string | null;  // a command: the folder it runs in
  status: string;  // active or paused; done, cancelled or expired once it's over
  next_run: number | null; last_run: number | null; until: number | null; runs: number; last_result: string | null;
  task_kind: string | null; project: string | null; when: string; check: string;
  only_changes: number;  // 1: a task that stays silent unless something changed since its last run
  notepad: { at: number; text: string } | null;  // what its latest run noted for the next (job memory)
}
// Do not disturb (jarvis.tasks.hold): `until` null holds until resumed; `said` is "on until 15:00"
export interface Hold { since: number; until: number | null; said: string }
// What Claude last said about the usage limits, and whether scheduled runs wait for a reset
export interface Usage { said: string; waiting: string }
export interface Quiet { hold: Hold | null; usage: Usage }
// One of a job's runs; `hidden` is a check that found nothing, so it was never announced; `fix`, a task fixing a
// command's run that failed
export interface ScheduleRun {
  id: number; status: string; created: number; updated: number; hidden: boolean; result: string; fix?: boolean;
}
export interface ScheduleDetail extends Schedule { recent: ScheduleRun[]; runs_problem: string }

const KIND_LABEL: Record<string, string> = { reminder: "Reminder", task: "Scheduled task", watch: "Watch",
  command: "Scheduled command" };
export const kindLabel = (kind: string) => KIND_LABEL[kind] ?? kind;

// The panel's open item for a job: tasks are opened by their number, jobs as "job-3"
const JOB_ITEM = /^job-(\d+)$/;
export const jobItem = (id: number) => `job-${id}`;
export function jobOf(item: string | null): number | null {
  const m = item ? JOB_ITEM.exec(item) : null;
  return m ? Number(m[1]) : null;
}

// "now" when it's due, "16:30" today, "tomorrow 09:00", "Mon 09:00" within the week, else "12 Oct 09:00"
export function at(ts: number, now = Date.now()): string {
  if (ts * 1000 <= now) return "now";
  const d = new Date(ts * 1000), hm = d.toTimeString().slice(0, 5);
  const days = Math.round((new Date(d).setHours(0, 0, 0, 0) - new Date(now).setHours(0, 0, 0, 0)) / 86_400_000);
  if (days === 0) return hm;
  if (days === 1) return `tomorrow ${hm}`;
  if (days < 7) return `${d.toLocaleDateString("en-GB", { weekday: "short" })} ${hm}`;
  return `${d.getDate()} ${d.toLocaleDateString("en-GB", { month: "short" })} ${hm}`;
}

// What the list shows for when it runs next
export function nextRun(job: Pick<Schedule, "status" | "next_run">, now = Date.now()): string {
  if (job.status === "paused") return "paused";
  return job.next_run ? `next ${at(job.next_run, now)}` : "no run to come";
}
