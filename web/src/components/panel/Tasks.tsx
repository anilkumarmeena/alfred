// Tasks section: scheduled jobs, items waiting on the user, running and recent tasks; plus task detail (live log,
// suggested edits, actions) and job detail (recent runs).
import { useEffect, useRef } from "preact/hooks";
import { useSignal } from "@preact/signals";
import { jobOf, kindLabel, nextRun, type Schedule } from "../../lib/schedule";
import { now } from "../../state/store";
import {
  ACTIVE, act, ago, approvals, changeSchedule, detail, elapsed, flash, hhmm, item, jobDetail, lapses, loadSchedule,
  loadTask, loadTasks, needYou, openSchedule, openTask, questions, quiet, roomy, runnerDown, schedules, tasks, when, type Approval,
  type Task, type TaskDetail,
} from "../../state/panel";
import { BellIcon, ChevronIcon, EyeIcon, PauseIcon, PlayIcon, RepeatIcon, RetryIcon, StopIcon, TerminalIcon, TrashIcon } from "../icons";
import { Answer } from "./Answer";
import { cap, Empty, InlineForm, Led, Pill, StatusPill } from "./bits";
import { Markdown } from "./Markdown";

const where = (t: Task) => [t.project, t.kind === "code_change" ? (t.in_place ? "in place" : "worktree") : t.kind.replace("_", " "),
  t.machine && `on ${t.machine}`].filter(Boolean).join(" · ");

function Request({ a, named = true }: { a: Approval; named?: boolean }) {
  return (
    <div class="req">
      {named && <div class="inline"><Pill kind="waiting">Needs a yes</Pill><b class="reqtitle">Task {a.task} · {a.title}</b></div>}
      {a.commands.map((c) => (
        <div key={c.command}>
          <code class="cmd">{c.command}</code>
          <div class="why">{cap(c.why)} · in {c.folder.split("/").slice(-2).join("/")}</div>
        </div>
      ))}
      <Answer key={a.id} a={a} small />
      <div class="exp">{lapses(a.expires, now.value)}</div>
    </div>
  );
}

const reply = (id: number) => async (message: string) => {
  const r = await act("reply", { id, message }, `Sent to task ${id}`);
  if (r) { loadTasks(); if (item.peek() === String(id)) loadTask(id); }
  return !!r;
};

// Failed command task whose fix needs the user's approval
function FixQuestion({ t }: { t: Task }) {
  const busy = useSignal(false);
  async function answer(fix: boolean) {
    busy.value = true;
    const r = fix ? await act("reply", { id: t.id, message: "Yes, fix it." }, `Task ${t.id} is being fixed`)
      : await act("stop", { id: t.id }, `Left task ${t.id} as it failed`);
    busy.value = false;
    if (r) { loadTasks(); if (item.peek() === String(t.id)) loadTask(t.id); }
  }
  return (
    <div class="qbox">
      <div class="inline"><Pill kind="waiting">Fix it?</Pill><b class="reqtitle">Task {t.id} · {t.title}</b></div>
      <Markdown text={t.question ?? ""} class="qtext" />
      <div class="acts">
        <button class="btn ok sm" disabled={busy.value} onClick={() => answer(true)}>Fix it</button>
        <button class="btn sm" disabled={busy.value} onClick={() => answer(false)}>Leave it</button>
      </div>
    </div>
  );
}

function Question({ t }: { t: Task }) {
  if (t.kind === "command") return <FixQuestion t={t} />;
  return (
    <div class="qbox">
      <div class="inline"><Pill kind="waiting">Question</Pill><b class="reqtitle">Task {t.id} · {t.title}</b></div>
      <Markdown text={t.question ?? ""} class="qtext" />
      <InlineForm placeholder={`Answer task ${t.id}…`} label="Send" send={reply(t.id)} />
    </div>
  );
}

// Stalled running task; JARVIS never stops it on its own
function Stuck({ t }: { t: Task }) {
  return (
    <div class="note amber">
      <div>No sign of progress for {t.stalled!.minutes} minutes, since {hhmm(t.stalled!.since)}: it may be stuck.
        Nudge it with a message below, or stop it.</div>
    </div>
  );
}

// A run of a scheduled job: the job's name on a row of its own
const JobMark = ({ name }: { name: string }) => <div class="jobmark" title={`Scheduled: ${name}`}><RepeatIcon /><span>{name}</span></div>;

function Card({ t }: { t: Task }) {
  const ref = useRef<HTMLButtonElement>(null);
  const sel = item.value === String(t.id);
  // Opened from chat or a notification: scroll into view and highlight
  useEffect(() => {
    const f = flash.value;
    if (f?.id !== t.id || !ref.current) return;
    ref.current.scrollIntoView({ block: "nearest", behavior: "smooth" });
    ref.current.classList.remove("ring"); void ref.current.offsetWidth; ref.current.classList.add("ring");
  }, [flash.value]);
  const active = ACTIVE.includes(t.status), waiting = t.status === "waiting";
  const first = waiting && t.requests_only ? approvals.value.find((a) => a.task === t.id)?.commands[0]?.command : null;
  return (
    <button ref={ref} class={`task${sel ? " sel" : ""}`} onClick={() => openTask(t.id)}>
      <div class="t1">
        {active ? <Led kind={waiting || t.stalled ? "warn" : "run"} /> : !t.seen && <span class="new" />}
        <span class="id">#{t.id}</span><span class="title">{t.title}</span>
        {(waiting || !active) && <StatusPill status={t.status} requestsOnly={t.requests_only} />}
        {active && !waiting && <ChevronIcon />}
      </div>
      {t.job_title && <JobMark name={t.job_title} />}
      <div class="meta">
        {where(t)} · {active ? <span class="el">{elapsed(t.created, now.value)}</span> : ago(t.updated, now.value)}
        {t.pending_edits ? ` · ${t.pending_edits} suggested edit${t.pending_edits > 1 ? "s" : ""}` : ""}
      </div>
      {active && <div class={`doing${t.stalled ? " stuck" : ""}`}>{first ? `Waiting for your yes to ${first}`
        : t.stalled ? `Seems stuck: no progress for ${t.stalled.minutes} min · last: ${t.activity || t.status}`
        : <Markdown text={(waiting ? t.question : t.activity) || t.status} inline links={false} />}</div>}
      {t.status === "failed" && t.error && <div class="ferr">{t.error}</div>}
    </button>
  );
}

// ── Scheduled jobs (created by voice; managed here) ──

const CANCEL_ASK = (j: Schedule) => `Cancel “${j.title}”? It won't run again; a run already under way carries on.`;

const JobPill = ({ j }: { j: Schedule }) => <Pill kind={j.status === "active" ? "running" : "muted"}>{j.status}</Pill>;

// Labelled on the job's page; icons on its row in the list
function JobButtons({ j, icons = false }: { j: Schedule; icons?: boolean }) {
  const busy = useSignal(false);
  const toggle = j.status === "paused" ? "resume" : "pause";
  async function change(action: "pause" | "resume" | "cancel") {
    if (action === "cancel" && !confirm(CANCEL_ASK(j))) return;
    busy.value = true;
    await changeSchedule(action, j);
    busy.value = false;
  }
  if (icons) return (
    <>
      <button class="ico" disabled={busy.value} aria-label={`${cap(toggle)} ${j.title}`} title={cap(toggle)}
        onClick={() => change(toggle)}>{toggle === "pause" ? <PauseIcon /> : <PlayIcon />}</button>
      <button class="ico danger" disabled={busy.value} aria-label={`Cancel ${j.title}`} title="Cancel"
        onClick={() => change("cancel")}><TrashIcon /></button>
    </>
  );
  return (
    <>
      <button class="btn sm" disabled={busy.value} aria-label={`${cap(toggle)} ${j.title}`}
        onClick={() => change(toggle)}>{cap(toggle)}</button>
      <button class="btn danger sm" disabled={busy.value} aria-label={`Cancel ${j.title}`}
        onClick={() => change("cancel")}>Cancel</button>
    </>
  );
}

const KIND_ICON: Record<string, () => preact.JSX.Element> = { reminder: BellIcon, task: RepeatIcon, watch: EyeIcon, command: TerminalIcon };

// Title with its state as a light (green on, grey paused); kind and next run below; one-tap pause and cancel
function JobRow({ j }: { j: Schedule }) {
  const sel = jobOf(item.value) === j.id, Kind = KIND_ICON[j.kind] ?? RepeatIcon;
  return (
    <div class={`sched${sel ? " sel" : ""}${j.status === "paused" ? " paused" : ""}`}>
      <button class="open" onClick={() => openSchedule(j.id)}>
        <span class="t1"><Led kind={j.status === "active" ? "ok" : "off"} /><span class="title">{j.title}</span></span>
        <span class="meta"><Kind />{kindLabel(j.kind)} · {nextRun(j, now.value)}</span>
      </button>
      <JobButtons j={j} icons />
    </div>
  );
}

export function Scheduled() {
  const jobs = schedules.value, usage = quiet.value?.usage;
  return (
    <>
      <div class="grp">Scheduled</div>
      {usage?.waiting && <div class="note amber">{usage.said} {usage.waiting}</div>}
      {jobs.length
        ? <div class="scheds">{jobs.map((j) => <JobRow key={j.id} j={j} />)}</div>
        : <Empty>Nothing scheduled. Ask JARVIS for a reminder, a task on a schedule, or to watch for something.</Empty>}
    </>
  );
}

const SAYS: Record<string, string> = { reminder: "Says", task: "Instruction", watch: "Watching for", command: "Command" };

export function ScheduleView({ id }: { id: number }) {
  const j = jobDetail.value?.id === id ? jobDetail.value : null;
  useEffect(() => {
    jobDetail.value = null;
    loadSchedule(id);
  }, [id]);
  if (!j) return <Empty>Loading the scheduled job…</Empty>;
  return (
    <>
      <p class="big">{j.title}</p>
      <dl class="kv">
        <dt>Type</dt><dd>{kindLabel(j.kind)}{j.kind === "task" && j.task_kind ? ` · ${j.task_kind.replace("_", " ")}` : ""}{j.project ? ` · ${j.project}` : ""}</dd>
        <dt>When</dt><dd>{j.when}</dd>
        <dt>Status</dt><dd><JobPill j={j} />{j.status === "active" ? ` ${nextRun(j, now.value)}` : ""}</dd>
        {j.check && <><dt>Checks</dt><dd>{j.check}</dd></>}
        {j.folder && <><dt>Runs in</dt><dd>{j.folder}</dd></>}
        <dt>Runs</dt><dd>{j.runs}{j.last_run ? ` · last ${when(j.last_run)}` : ""}</dd>
        {j.kind === "task" && <><dt>Speaks</dt><dd>{j.only_changes ? "only when there's something new" : "after every run"}</dd></>}
        {j.kind === "command" && <><dt>Speaks</dt><dd>{j.only_changes ? "only when a run prints something" : "after every run"}</dd></>}
      </dl>
      <label class="lbl">{SAYS[j.kind] ?? "What"}</label>
      <Markdown text={j.text} class="summary" />
      {j.last_result && <><label class="lbl">Latest result</label><Markdown text={j.last_result} class="summary" /></>}
      {j.notepad && <>
        <label class="lbl">Notepad for the next run · {when(j.notepad.at)}</label>
        <div class="summary">{j.notepad.text}</div>
      </>}
      {(j.task_kind || j.kind === "command") && <>
        <label class="lbl">Recent runs</label>
        {j.runs_problem && <div class="note amber">{j.runs_problem}</div>}
        {j.recent.length ? j.recent.map((r) => (
          <button key={r.id} class="task run" onClick={() => openTask(r.id)}>
            <div class="t1">
              <span class="id">#{r.id}</span><span class="title">{when(r.created)}</span>
              {r.fix && <Pill kind="muted">fix</Pill>}
              {r.hidden && r.status === "done" ? <Pill kind="muted">nothing new</Pill> : <StatusPill status={r.status} />}
            </div>
            {r.result && <div class="meta"><Markdown text={r.result} inline links={false} /></div>}
          </button>
        )) : !j.runs_problem && <Empty>No runs yet.</Empty>}
      </>}
      {(j.status === "active" || j.status === "paused") && <div class="acts end"><JobButtons j={j} /></div>}
    </>
  );
}

// ── Tasks ───────────────────────────────────────────────────────

// Most urgent first: what waits on the user, then what's running, then what's to come and what's done
export function TaskList() {
  const all = tasks.value, running = all.filter((t) => ACTIVE.includes(t.status)), recent = all.filter((t) => !ACTIVE.includes(t.status));
  const need = needYou.value, busy = all.filter((t) => t.status === "running" || t.status === "queued").length;
  return (
    <>
      <p class="tsum">{busy} running{need ? <> · <b>{need} need{need === 1 ? "s" : ""} you</b></> : ""}</p>
      {runnerDown.value && <div class="note amber">{runnerDown.value}</div>}
      {need > 0 && <div class="grp">Waiting for you</div>}
      {approvals.value.map((a) => <Request key={a.id} a={a} />)}
      {questions.value.map((t) => <Question key={t.id} t={t} />)}
      <div class="grp">Running</div>
      {running.length ? running.map((t) => <Card key={t.id} t={t} />) : <Empty>Nothing running. Ask JARVIS to start a task.</Empty>}
      <Scheduled />
      <div class="grp">Recent</div>
      {recent.length ? recent.map((t) => <Card key={t.id} t={t} />) : <Empty>No tasks in the last day.</Empty>}
    </>
  );
}

const LOG_KIND: Record<string, string> = { approval: "q", question: "q", failed: "bad", nudge: "you", reply: "you" };

// Tail of a command task's output, live while running
function Output({ text }: { text: string }) {
  const box = useRef<HTMLPreElement>(null);
  useEffect(() => { if (box.current) box.current.scrollTop = box.current.scrollHeight; }, [text]);
  return <pre class="tlog" ref={box}>{text || "(nothing printed yet)"}</pre>;
}

const TaskLink = ({ id }: { id: number }) => <button class="linkish" onClick={() => openTask(id)}>task #{id}</button>;

// Command task details and its links to a fix task or previous run
function CommandFacts({ t }: { t: TaskDetail }) {
  const c = t.command!;
  return (
    <>
      <dt>Runs in</dt><dd>{c.folder}</dd>
      {c.timeout_minutes && <><dt>Time limit</dt><dd>{c.timeout_minutes} min</dd></>}
      {c.after && <><dt>Rerun</dt><dd>after <TaskLink id={c.after.fix} /> fixed <TaskLink id={c.after.task} /></dd></>}
      {c.fix_task && <><dt>Fixed by</dt><dd><TaskLink id={c.fix_task} /></dd></>}
    </>
  );
}

function Log({ lines }: { lines: TaskDetail["log"] }) {
  const box = useRef<HTMLPreElement>(null);
  useEffect(() => { if (box.current) box.current.scrollTop = box.current.scrollHeight; }, [lines.length]);
  return (
    <pre class="tlog" ref={box}>
      {lines.map((l, i) => (
        <div key={i} class={LOG_KIND[l.kind] ?? (l.kind === "step" ? "step" : "")}><span class="k">{hhmm(l.at)}</span> {l.text}</div>
      ))}
    </pre>
  );
}

function Edits({ t }: { t: TaskDetail }) {
  const pending = t.edits.filter((e) => e.status === "pending");
  async function change(action: "apply" | "dismiss", numbers: number[]) {
    const r = await act<{ outcome: string[] }>("edits", { id: t.id, action, edits: numbers });
    if (r) { loadTask(t.id); loadTasks(); }
  }
  return (
    <>
      <div class="grp">Suggested edits
        {pending.length > 1 && <button class="act" onClick={() => change("apply", pending.map((e) => e.n))}>Apply all {pending.length}</button>}
      </div>
      {t.edits.map((e) => (
        <div key={e.n} class={`edit${e.status !== "pending" ? " settled" : ""}`}>
          <div>{e.needs_yes && e.status === "pending" && <Pill kind="waiting">needs a yes</Pill>} {e.what}</div>
          {e.reason && <div class="sub">{e.reason}</div>}
          {e.status === "pending"
            ? <div class="acts"><button class="btn ok sm" onClick={() => change("apply", [e.n])}>Apply</button>
                <button class="btn sm" onClick={() => change("dismiss", [e.n])}>Dismiss</button></div>
            : <div class="sub">{e.status}{e.result && e.status !== "dismissed" ? `: ${e.result}` : ""}</div>}
        </div>
      ))}
    </>
  );
}

export function TaskView({ id }: { id: number }) {
  const t = detail.value?.id === id ? detail.value : null;
  const busy = useSignal("");
  useEffect(() => {
    detail.value = null;
    loadTask(id);
  }, [id]);
  // Poll while active: events don't announce every step
  const active = !!t && ACTIVE.includes(t.status);
  useEffect(() => {
    if (!active) return;
    const timer = setInterval(() => loadTask(id), 4000);
    return () => clearInterval(timer);
  }, [id, active]);

  if (!t) return <Empty>Loading task {id}…</Empty>;
  const finished = !ACTIVE.includes(t.status) && t.status !== "discarded";
  const branchy = t.kind === "code_change" && !t.in_place && !!t.branch;
  const mine = approvals.value.filter((a) => a.task === id);
  const shellJob = t.kind === "command" && !!t.command;  // no model or messages; show its output

  async function run(action: string, done: string, ask?: string) {
    if (ask && !confirm(ask)) return;
    busy.value = action;
    const r = await act(action, { id }, done);
    busy.value = "";
    if (r) { loadTask(id); loadTasks(); }
  }

  // A rerun is a new task: open that one
  async function again() {
    busy.value = "retry";
    const r = await act<Task>("retry", { id }, `Running it again`);
    busy.value = "";
    if (r) { loadTasks(); openTask(r.id); }
  }

  return (
    <>
      <p class="big">{t.title}</p>
      <dl class="kv">
        <dt>Status</dt><dd><StatusPill status={t.status} requestsOnly={t.requests_only} />{t.stalled && <> <Pill kind="waiting">seems stuck</Pill></>}</dd>
        <dt>Project</dt><dd>{where(t) || "research"}{branchy ? `, branch ${t.branch}` : ""}</dd>
        <dt>Started</dt><dd>{hhmm(t.created)} · {ACTIVE.includes(t.status) ? elapsed(t.created, now.value) : `took ${elapsed(t.created, t.updated * 1000)}`}{t.cost ? ` · $${t.cost.toFixed(2)}` : ""}</dd>
        {t.changes && <><dt>Changes</dt><dd>{t.changes}</dd></>}
        {t.job != null && <><dt>Scheduled</dt><dd><button class="linkish" onClick={() => openSchedule(t.job!)}>
          {t.job_title ?? `job ${t.job}`}</button></dd></>}
        {shellJob && <CommandFacts t={t} />}
        {t.fixes != null && <><dt>Fixes</dt><dd><TaskLink id={t.fixes} /></dd></>}
      </dl>
      {mine.map((a) => <Request key={a.id} a={a} named={false} />)}
      {t.stalled && <Stuck t={t} />}
      {t.status === "waiting" && !t.requests_only && <Question t={t} />}
      {t.error && <div class="ferr">{t.error}</div>}
      {(t.summary || t.spoken) && !ACTIVE.includes(t.status) && <Markdown text={(t.summary || t.spoken)!} class="summary" />}
      {t.edits.length > 0 && <Edits t={t} />}
      {shellJob && <>
        <label class="lbl">Command</label>
        <code class="cmd">{t.instruction}</code>
        <label class="lbl">{t.status === "running" ? <><Led kind="run" />Live output</> : "Output"}</label>
        <Output text={t.command!.output_tail} />
      </>}
      <label class="lbl">{ACTIVE.includes(t.status) ? <><Led kind="run" />Live log</> : "Log"}</label>
      <Log lines={t.log} />
      {t.status !== "discarded" && !shellJob && <>
        <label class="lbl">{ACTIVE.includes(t.status) ? "Reply or nudge" : "Follow up"}</label>
        <InlineForm placeholder={ACTIVE.includes(t.status) ? "e.g. “keep it to four buttons”" : "e.g. “now add tests”"} label="Send" send={reply(id)} />
      </>}
      <div class="acts end tacts">
        {ACTIVE.includes(t.status) && <button class="btn sm" disabled={!!busy.value} onClick={() => run("stop", `Stopped task ${id}`)}><StopIcon />Stop</button>}
        {shellJob ? finished && <button class="btn sm" disabled={!!busy.value} onClick={again}><RetryIcon />Run again</button>
          : (t.status === "failed" || t.status === "stopped") && <button class="btn sm" disabled={!!busy.value} onClick={() => run("retry", `Task ${id} is running again`)}><RetryIcon />Retry</button>}
        {branchy && finished && <button class="btn primary sm" disabled={!!busy.value} onClick={() => run("merge", `Merged task ${id}`)}>{busy.value === "merge" ? "Merging…" : "Merge"}</button>}
        {finished && t.kind === "code_change" && <button class="btn danger sm" disabled={!!busy.value}
          onClick={() => run("discard", `Discarded task ${id}`, `Discard task ${id}'s work without merging it? This can't be undone.`)}>Discard</button>}
      </div>
    </>
  );
}

export function TasksSection() {
  const job = jobOf(item.value), open = item.value && job == null ? Number(item.value) : null;
  const view = job != null ? <ScheduleView id={job} /> : open ? <TaskView id={open} /> : null;
  if (roomy.value) {
    return (
      <div class="split">
        <div class="col lead"><TaskList /></div>
        <div class="col">{view ?? <Empty>Pick a task to see its log and what it did, or a scheduled job to see its runs.</Empty>}</div>
      </div>
    );
  }
  return view ?? <TaskList />;
}
