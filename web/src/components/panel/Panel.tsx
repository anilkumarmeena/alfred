// The panel: a column beside the voice screen on a desktop, with tabs; on a phone, a sheet that slides up over it
// from the row of buttons along the bottom (the dock), and can be dragged between full height, half and closed.
import { useEffect, useRef } from "preact/hooks";
import { jobOf } from "../../lib/schedule";
import { readout } from "../../state/store";
import {
  back, closeSheet, go, item, needYou, roomy, section, sheet, tasks, todos, unseen, wide, type Section, type SettingsItem,
} from "../../state/panel";
import { dayLabel } from "../../lib/history";
import { days } from "../../state/history";
import { BackIcon, CloseIcon, HistoryIcon, ProjectsIcon, SettingsIcon, TasksIcon, TodosIcon } from "../icons";
import { HistorySection } from "./History";
import { ProjectsSection } from "./Projects";
import { SETTINGS_TITLES, SettingsSection } from "./Settings";
import { TasksSection } from "./Tasks";
import { TodosSection } from "./Todos";

const SECTIONS: [Section, string, () => preact.JSX.Element][] = [
  ["tasks", "Tasks", TasksIcon], ["todos", "To-dos", TodosIcon], ["projects", "Projects", ProjectsIcon], ["history", "History", HistoryIcon],
  ["settings", "Settings", SettingsIcon],
];
const ROOMY = 760;  // px of panel width for a list and the open item side by side

// The badge on a section's button: an amber count when something waits on the user, a dot for news.
export function Badge({ sec }: { sec: Section }) {
  if (sec !== "tasks") return null;
  if (needYou.value) return <i class="bdg n">{needYou.value}</i>;
  return unseen.value.length ? <i class="bdg dot" /> : null;
}

function Tabs() {
  const running = tasks.value.filter((t) => t.status === "running" || t.status === "queued").length;
  const open = todos.value?.filter((t) => !t.done).length;
  const kpi = section.value === "tasks"
    ? <>{running} running{needYou.value ? <> · <b>{needYou.value} need{needYou.value === 1 ? "s" : ""} you</b></> : ""}</>
    : section.value === "todos" && open != null ? <>{open} open</> : null;
  return (
    <nav class="ptabs" aria-label="Sections">
      {SECTIONS.map(([sec, label, Icon]) => (
        <button key={sec} class={section.value === sec ? "on" : ""} aria-current={section.value === sec ? "page" : undefined}
          onClick={() => go(sec)}><Icon />{label}<Badge sec={sec} /></button>
      ))}
      {kpi && <span class="pkpi">{kpi}</span>}
    </nav>
  );
}

function title(): string {
  const sec = section.value, open = item.value;
  if (open && sec === "tasks") return jobOf(open) != null ? "Scheduled" : `Task ${open}`;
  if (open && sec === "settings") return SETTINGS_TITLES[open as SettingsItem] ?? "Settings";
  if (open && sec === "history") return dayLabel(open, days.value?.today ?? "");
  return SECTIONS.find(([s]) => s === sec)![1];
}

// Phone: drag the handle or the title between full, half and closed; a tap on the handle switches full and half.
function useDrag(panel: { current: HTMLElement | null }) {
  return (e: PointerEvent) => {
    const el = panel.current, target = e.currentTarget as HTMLElement;
    if (wide.peek() || !el || (e.target as Element).closest("button:not(.grabzone)")) return;
    const y0 = e.clientY, top0 = el.offsetTop, t0 = Date.now();
    let moved = false;
    el.classList.add("dragging");
    target.setPointerCapture(e.pointerId);
    const move = (m: PointerEvent) => {
      const dy = m.clientY - y0;
      if (Math.abs(dy) > 4) moved = true;
      el.style.top = `${Math.max(0, top0 + dy)}px`;
    };
    const up = (u: PointerEvent) => {
      target.removeEventListener("pointermove", move);
      target.removeEventListener("pointerup", up);
      target.removeEventListener("pointercancel", up);
      const h = innerHeight, top = el.offsetTop, fast = (u.clientY - y0) / Math.max(1, Date.now() - t0);
      el.classList.remove("dragging");
      el.style.top = "";
      if (!moved) { if (target.classList.contains("grabzone")) sheet.value = sheet.peek() === "half" ? "full" : "half"; return; }
      if (top > h * 0.74 || fast > 1.1) closeSheet();
      else if (top > h * 0.32 || fast > 0.5) sheet.value = "half";
      else sheet.value = "full";
    };
    target.addEventListener("pointermove", move);
    target.addEventListener("pointerup", up);
    target.addEventListener("pointercancel", up);
  };
}

export function Panel() {
  const ref = useRef<HTMLElement>(null);
  const drag = useDrag(ref);
  // Wide enough for a list and the open item side by side?
  useEffect(() => {
    const el = ref.current!;
    const watch = new ResizeObserver(() => { roomy.value = wide.peek() && el.clientWidth >= ROOMY; });
    watch.observe(el);
    return () => watch.disconnect();
  }, []);
  const sec = section.value, drilled = !!item.value && !roomy.value;
  const Body = { tasks: TasksSection, todos: TodosSection, projects: ProjectsSection, history: HistorySection, settings: SettingsSection }[sec];
  return (
    <aside id="panel" class={`panel${drilled ? " drill" : ""}`} ref={ref} aria-label="Panel" data-sheet={sheet.value}>
      <div class="grabzone" onPointerDown={drag} title="Drag, or tap for half or full height"><div class="grab" /></div>
      <Tabs />
      <div class="phead" onPointerDown={drag}>
        {drilled && <button class="ico back" aria-label="Back" onClick={back}><BackIcon /></button>}
        <h2>{title()}</h2>
        <span class="vstrip"><span class="dot run" />{readout.value.label}</span>
        <button class="ico close" aria-label="Close" onClick={closeSheet}><CloseIcon /></button>
      </div>
      <div class={`pbody${roomy.value && (sec === "tasks" || sec === "history" || sec === "settings") ? " splitting" : ""}`} key={`${sec}:${drilled ? item.value : ""}`}>
        <Body />
      </div>
    </aside>
  );
}

export function Dock() {
  return (
    <nav class="dock" aria-label="Sections">
      {SECTIONS.map(([sec, label, Icon]) => {
        const on = sheet.value !== "closed" && section.value === sec;
        return (
          <button key={sec} class={on ? "on" : ""} aria-pressed={on}
            onClick={() => (on ? closeSheet() : go(sec))}><Icon />{label}<Badge sec={sec} /></button>
        );
      })}
    </nav>
  );
}
