// Desktop only. Sidebar: wordmark and link, the call's buttons while away from Home, the sections, then what waits on
// the user and what's running. Rail (Home, wide windows): open to-dos to tick off or add, and recent conversations.
import { useEffect } from "preact/hooks";
import { useSignal } from "@preact/signals";
import { dayLabel, tally } from "../../lib/history";
import { hangup, pressMute } from "../../state/session";
import { days, loadDays } from "../../state/history";
import {
  approvals, elapsed, go, home, load, openTask, questions, section, setHome, tasks, todoAct, todos, visible, type Section, type Todo,
} from "../../state/panel";
import { connected, muted, now } from "../../state/store";
import { CheckIcon, HangupIcon, HistoryIcon, HomeIcon, MicIcon, PlusIcon } from "../icons";
import { StatusPill } from "../Header";
import { Empty, InlineForm } from "./bits";
import { Markdown } from "./Markdown";
import { Badge, SECTIONS } from "./Panel";
import { checkTodo } from "./Todos";

const NAV: [Section, string, () => preact.JSX.Element][] = [...SECTIONS, ["history", "History", HistoryIcon]];

// Mute and End, for a call that carries on while a section page has the room (Home has the call bar).
function CallButtons() {
  if (!connected.value) return null;
  return (
    <span class="scall">
      <button class="cb" aria-pressed={muted.value} aria-label={muted.value ? "Unmute" : "Mute"} title={muted.value ? "Unmute (M)" : "Mute (M)"}
        onClick={pressMute}><MicIcon /></button>
      <button class="cb end" aria-label="End" title="End the call" onClick={hangup}><HangupIcon /></button>
    </span>
  );
}

function Waiting() {
  const asks = approvals.value, qs = questions.value;
  if (!asks.length && !qs.length) return null;
  return (
    <section>
      <p class="sh">Waiting for you</p>
      {asks.map((a) => (
        <button key={`a${a.id}`} class="smini amber" onClick={() => openTask(a.task)}>
          <b>Task {a.task} wants to run</b>
          <small class="mono">{a.commands[0]?.command}</small>
        </button>
      ))}
      {qs.map((t) => (
        <button key={`q${t.id}`} class="smini amber" onClick={() => openTask(t.id)}>
          <b>Task {t.id} asks</b>
          <small><Markdown text={t.question ?? t.title} inline links={false} /></small>
        </button>
      ))}
    </section>
  );
}

function Running() {
  const running = tasks.value.filter((t) => t.status === "running" || t.status === "queued");
  return (
    <section>
      <p class="sh">Running</p>
      {running.map((t) => (
        <button key={t.id} class="smini" onClick={() => openTask(t.id)}>
          <b><span class={`dot ${t.stalled ? "warn" : "run"}`} />{t.title}</b>
          <small class={t.stalled ? "warn" : undefined}>
            {t.stalled ? `Seems stuck · ${t.stalled.minutes} min` : t.activity || t.status} · <span class="el">{elapsed(t.created, now.value)}</span>
          </small>
        </button>
      ))}
      {!running.length && <p class="empty">Nothing running.</p>}
    </section>
  );
}

export function Sidebar() {
  useEffect(() => { if (!todos.peek()) load("todos"); }, []);  // for the count, and the rail
  const open = todos.value?.filter((t) => !t.done).length;
  const at = (sec: Section) => visible.value && section.value === sec;
  return (
    <aside class="side" aria-label="Sections">
      <div class="sbrand">
        <span class="word">J.A.R.V.I.S.</span>
        <div class="slink"><StatusPill /><CallButtons /></div>
      </div>
      <nav class="snav">
        <button aria-current={home.value ? "page" : undefined} title="The call (\)" onClick={() => setHome(true)}><HomeIcon />Home</button>
        {NAV.map(([sec, label, Icon]) => (
          <button key={sec} aria-current={at(sec) ? "page" : undefined} onClick={() => go(sec)}>
            <Icon />{label}<Badge sec={sec} />
            {sec === "todos" && open ? <span class="cnt">{open}</span> : null}
          </button>
        ))}
      </nav>
      <Waiting />
      <Running />
    </aside>
  );
}

// ── Rail ────────────────────────────────────────────────────────

function RailTodo({ t }: { t: Todo }) {
  const ticking = useSignal(false);
  async function tick() {
    if (ticking.value) return;
    ticking.value = true;
    await checkTodo(t);
    ticking.value = false;
  }
  return (
    <li class="row">
      <button class={`check${ticking.value ? " on" : ""}`} aria-label="Tick off" onClick={tick}><CheckIcon /></button>
      <div class="grow"><Markdown text={t.text} inline /></div>
    </li>
  );
}

export function Rail() {
  useEffect(() => { if (home.value) loadDays(); }, [home.value]);
  const open = todos.value?.filter((t) => !t.done), today = days.value?.today ?? "";
  return (
    <aside class="rail" aria-label="At a glance">
      <section class="rcard">
        <h4><span>To-dos</span><button onClick={() => go("todos")}>All</button></h4>
        {!open ? <Empty>Loading…</Empty> : <ul class="list">
          {open.slice(0, 6).map((t) => <RailTodo key={t.id} t={t} />)}
          {!open.length && <li class="row none">Nothing open.</li>}
        </ul>}
        {open && open.length > 6 && <button class="rmore" onClick={() => go("todos")}>{open.length - 6} more</button>}
        <InlineForm placeholder="Add a to-do" label="Add" send={async (text) => !!(await todoAct("todo_add", { text }))}><PlusIcon /></InlineForm>
      </section>
      <section class="rcard">
        <h4><span>History</span><button onClick={() => go("history")}>All</button></h4>
        {days.value?.days.slice(0, 4).map((d) => (
          <button key={d.date} class="ritem" onClick={() => go("history", d.date)}>
            <b>{dayLabel(d.date, today)}</b><small>{tally(d.exchanges, d.count)}</small>
          </button>
        )) ?? <Empty>Loading…</Empty>}
        {days.value && !days.value.days.length && <Empty>No conversations yet.</Empty>}
      </section>
    </aside>
  );
}
