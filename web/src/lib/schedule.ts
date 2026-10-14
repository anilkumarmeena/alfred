// Scheduled job types and formatting for the panel. Jobs are created by voice; the panel only lists, pauses,
// resumes and cancels them.

export interface Schedule {
  id: number; kind: "reminder" | "task" | "watch" | "command" | "resume" | "todo"; title: string; text: string;
  folder: string | null;  // command jobs: working folder
  status: string;  // active | paused | done | cancelled | expired
  next_run: number | null; last_run: number | null; until: number | null; runs: number; last_result: string | null;
  task_kind: string | null; project: string | null; when: string; check: string;
  does: string;  // resume and todo jobs: what each run takes on
  only_changes: number;  // 1: silent unless something changed since the last run
  notepad: { at: number; text: string } | null;  // note from the latest run for the next (job memory)
}
// Do not disturb: `until` null = until resumed; `said` e.g. "on until 15:00"
export interface Hold { since: number; until: number | null; said: string }
// Claude's last-reported usage limits, and whether scheduled runs wait for a reset
export interface Usage { said: string; waiting: string }
export interface Quiet { hold: Hold | null; usage: Usage }
// A job run. hidden: a check that found nothing (never announced). fix: task fixing a failed command run
export interface ScheduleRun {
  id: number; status: string; created: number; updated: number; hidden: boolean; result: string; fix?: boolean;
}
export interface ScheduleDetail extends Schedule { recent: ScheduleRun[]; runs_problem: string }

const KIND_LABEL: Record<string, string> = { reminder: "Reminder", task: "Scheduled task", watch: "Watch",
  command: "Scheduled command", resume: "Scheduled resume", todo: "Scheduled to-dos" };
export const kindLabel = (kind: string) => KIND_LABEL[kind] ?? kind;

// Panel item id for a job ("job-3"); tasks use their bare number
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

// Next-run label for the list
export function nextRun(job: Pick<Schedule, "status" | "next_run">, now = Date.now()): string {
  if (job.status === "paused") return "paused";
  return job.next_run ? `next ${at(job.next_run, now)}` : "no run to come";
}
