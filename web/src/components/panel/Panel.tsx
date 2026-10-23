// The panel: the open section's page on desktop (picked in the sidebar); on phones a sheet raised from the bottom
// dock, draggable full/half/closed.
import { useEffect, useRef } from "preact/hooks";
import { jobOf } from "../../lib/schedule";
import { assistantName, readout } from "../../state/store";
import {
  back, closeSheet, go, item, needYou, roomy, section, sheet, todos, unseen, wide, type Section, type SettingsItem,
} from "../../state/panel";
import { dayLabel } from "../../lib/history";
import { days } from "../../state/history";
import { BackIcon, CloseIcon, AlfredIcon, ProjectsIcon, TasksIcon, TodosIcon } from "../icons";
import { HistorySection } from "./History";
import { ProjectsSection } from "./Projects";
import { AlfredSection, SETTINGS_TITLES, SettingsSection } from "./Settings";
import { TaskCount, TasksSection } from "./Tasks";
import { TodosSection } from "./Todos";

// Dock; History and Settings open from their buttons in the top bar (History from the sidebar on desktop).
// The assistant's own section goes by its name, so this is read during render.
export const sections = (): [Section, string, () => preact.JSX.Element][] => [
  ["tasks", "Tasks", TasksIcon], ["todos", "To-dos", TodosIcon], ["projects", "Projects", ProjectsIcon], ["alfred", assistantName.value, AlfredIcon],
];
const ROOMY = 760;  // px: panel width that fits list and open item side by side

// Section badge: amber count when something waits on the user, a dot for news.
export function Badge({ sec }: { sec: Section }) {
  if (sec !== "tasks") return null;
  if (needYou.value) return <i class="bdg n">{needYou.value}</i>;
  return unseen.value.length ? <i class="bdg dot" /> : null;
}

// Desktop page header's count ("2 running · 1 needs you"); phones show it at the top of the section instead.
function Kpi() {
  const open = todos.value?.filter((t) => !t.done).length;
  const kpi = section.value === "tasks" ? <TaskCount />
    : section.value === "todos" && open != null ? <>{open} open</> : null;
  return kpi && <span class="pkpi">{kpi}</span>;
}

function title(open: string | null): string {
  const sec = section.value;
  if (open && sec === "tasks") return jobOf(open) != null ? "Scheduled" : `Task ${open}`;
  if (sec === "settings" || sec === "alfred") return (open && SETTINGS_TITLES[open as SettingsItem]) || (sec === "alfred" ? assistantName.value : "Settings");
  if (sec === "history") return open ? dayLabel(open, days.value?.today ?? "") : "History";
  return sections().find(([s]) => s === sec)![1];
}

// Phone: drag handle or title between full, half, closed; tapping the handle toggles full/half.
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
  useEffect(() => {
    const el = ref.current!;
    const watch = new ResizeObserver(() => { roomy.value = wide.peek() && el.clientWidth >= ROOMY; });
    watch.observe(el);
    return () => watch.disconnect();
  }, []);
  const sec = section.value, drilled = !!item.value && !roomy.value;
  const Body = { tasks: TasksSection, todos: TodosSection, projects: ProjectsSection, alfred: AlfredSection, history: HistorySection, settings: SettingsSection }[sec];
  return (
    <aside id="panel" class={`panel${drilled ? " drill" : ""}`} ref={ref} aria-label="Panel" data-sheet={sheet.value}>
      <div class="grabzone" onPointerDown={drag} title="Drag, or tap for half or full height"><div class="grab" /></div>
      <div class="phead" onPointerDown={drag}>
        {drilled && <button class="ico back" aria-label="Back" onClick={back}><BackIcon /></button>}
        <h2>{title(roomy.value ? null : item.value)}</h2>
        {wide.value && <Kpi />}
        <span class="vstrip"><span class="dot run" />{readout.value.label}</span>
        <button class="ico close" aria-label="Close" onClick={closeSheet}><CloseIcon /></button>
      </div>
      <div class={`pbody${roomy.value && (sec === "tasks" || sec === "history" || sec === "settings" || sec === "alfred") ? " splitting" : ""}`} key={`${sec}:${drilled ? item.value : ""}`}>
        <Body />
      </div>
    </aside>
  );
}

export function Dock() {
  return (
    <nav class="dock" aria-label="Sections">
      {sections().map(([sec, label, Icon]) => {
        const on = sheet.value !== "closed" && section.value === sec;
        return (
          <button key={sec} class={on ? "on" : ""} aria-pressed={on}
            onClick={() => (on ? closeSheet() : go(sec))}><Icon />{label}<Badge sec={sec} /></button>
        );
      })}
    </nav>
  );
}
