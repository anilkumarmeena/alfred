// Overlays: approval card, activity list, ⌘K palette, toast.
import { useEffect, useRef } from "preact/hooks";
import { useSignal } from "@preact/signals";
import { pressMute, toggleSpeaker } from "../../state/session";
import {
  activity, activityOpen, act, answer, approvals, ask, askGuard, closeSheet, go, lapses, openTask, paletteOpen,
  setSideOff, sheet, sideOff, tasks, toast, toastAction, toastText, todos, toggleActivity, tuck, when, wide, type Section, type SettingsItem,
} from "../../state/panel";
import { now } from "../../state/store";
import {
  BellIcon, BrainIcon, CheckIcon, CloseIcon, HistoryIcon, KeysIcon, MicIcon, PanelIcon, PlugIcon, PlusIcon, PrefsIcon, ProjectsIcon, PulseIcon, SettingsIcon,
  SpeakerIcon, TasksIcon, TodosIcon, UserIcon, WordsIcon,
} from "../icons";
import { Answer } from "./Answer";
import { cap } from "./bits";
import { Markdown } from "./Markdown";

export function ApprovalCard() {
  const a = approvals.value.find((x) => x.id === ask.value);
  if (!a) return null;
  return (
    <div class="ask" onClick={(e) => { if (e.target === e.currentTarget) tuck(); }}>
      <div class={`askcard${askGuard.value ? " guard" : ""}`} role="alertdialog" aria-labelledby="askTitle">
        <div class="lab"><span class="dot warn" />Task {a.task} wants to run
          <button class="ico x" aria-label="Later" onClick={tuck}><CloseIcon /></button></div>
        <h3 id="askTitle">{a.title}</h3>
        {a.commands.map((c) => (
          <div key={c.command}><code class="cmd">{c.command}</code>
            <div class="why">{cap(c.why)} · in {c.folder.split("/").slice(-2).join("/")}</div></div>
        ))}
        <div class="meta">JARVIS also asks aloud when you're talking. {cap(lapses(a.expires, now.value))}.</div>
        <Answer key={a.id} a={a} keys />
        <button class="later" onClick={tuck}>Later · keep it in Tasks</button>
      </div>
    </div>
  );
}

export function Activity() {
  const ref = useRef<HTMLDivElement>(null);
  // Close on outside tap
  useEffect(() => {
    if (!activityOpen.value) return;
    const away = (e: Event) => { if (!(e.target as Element).closest("#activity, #activityBtn")) toggleActivity(false); };
    document.addEventListener("pointerdown", away);
    return () => document.removeEventListener("pointerdown", away);
  }, [activityOpen.value]);
  if (!activityOpen.value) return null;
  return (
    <div class="pop" id="activity" ref={ref}>
      <div class="pophead"><h3>Activity</h3><button class="ico" aria-label="Close" onClick={() => toggleActivity(false)}><CloseIcon /></button></div>
      <ul>
        {activity.value.map((x) => (
          <li key={x.id} class={x.task != null ? "linked" : ""}
            onClick={x.task != null ? () => { toggleActivity(false); openTask(x.task!); } : undefined}>
            <span class={`dot ${x.kind}`} /><div><Markdown text={x.text} inline links={x.task == null} /></div><time>{when(x.at)}</time>
          </li>
        ))}
        {!activity.value.length && <li class="none">What JARVIS changes for you, and how its tasks go, shows up here.</li>}
      </ul>
    </div>
  );
}

interface Command { icon: () => preact.JSX.Element; label: string; run: () => void; hint?: string }

const goTo = (sec: Section, page: SettingsItem | null = null) => () => go(sec, page);

function commands(): Command[] {
  const fromTasks: Command[] = tasks.peek().map((t) => ({ icon: TasksIcon, label: `Task ${t.id} · ${t.title}`, run: () => openTask(t.id), hint: t.status }));
  return [
    ...fromTasks,
    { icon: TasksIcon, label: "Go to Tasks", run: goTo("tasks") },
    { icon: TodosIcon, label: "Go to To-dos", run: goTo("todos") },
    { icon: ProjectsIcon, label: "Go to Projects", run: goTo("projects") },
    { icon: HistoryIcon, label: "Go to History · past conversations", run: goTo("history") },
    { icon: SettingsIcon, label: "Go to Settings", run: goTo("settings") },
    { icon: PrefsIcon, label: "Preferences · do not disturb, voice, background work", run: goTo("settings", "prefs") },
    { icon: PlugIcon, label: "Connections", run: goTo("settings", "conn") },
    { icon: BrainIcon, label: "Memory", run: goTo("settings", "memory") },
    { icon: WordsIcon, label: "Vocabulary", run: goTo("settings", "vocabulary") },
    { icon: CheckIcon, label: "Remembered approvals", run: goTo("settings", "approvals") },
    { icon: UserIcon, label: "Google accounts", run: goTo("settings", "accounts") },
    { icon: PulseIcon, label: "System", run: goTo("settings", "system") },
    { icon: BellIcon, label: "Activity", run: () => toggleActivity(true) },
    { icon: KeysIcon, label: "Keyboard shortcuts", run: goTo("settings", "keys"), hint: "?" },
    { icon: MicIcon, label: "Mute or unmute", run: pressMute, hint: "M" },
    { icon: SpeakerIcon, label: "Speaker off or on · replies in text only", run: toggleSpeaker, hint: "S" },
    ...(wide.peek() ? [{ icon: PanelIcon, label: "Show or hide the panel", run: () => setSideOff(!sideOff.peek()), hint: "\\" }] : []),
  ];
}

export function Palette() {
  const q = useSignal(""), sel = useSignal(0), input = useRef<HTMLInputElement>(null);
  useEffect(() => { if (paletteOpen.value) { q.value = ""; sel.value = 0; setTimeout(() => input.current?.focus(), 0); } }, [paletteOpen.value]);
  if (!paletteOpen.value) return null;
  const text = q.value.trim(), words = text.toLowerCase().split(/\s+/).filter(Boolean);
  const items = commands().filter((c) => words.every((w) => c.label.toLowerCase().includes(w)));
  if (text) items.push({ icon: PlusIcon, label: `Add to-do “${text}”`, hint: "↵", run: async () => {
    const r = await act("todo_add", { text }, "Added to To-dos");
    if (r) todos.value = r;
  } });
  const at = Math.min(sel.value, items.length - 1);
  const run = (i: number) => { paletteOpen.value = false; items[i]?.run(); };
  return (
    <div class="pal" onClick={(e) => { if (e.target === e.currentTarget) paletteOpen.value = false; }}>
      <div class="palbox" role="dialog" aria-label="Jump to anything">
        <input ref={input} value={q.value} placeholder="Jump to a task, a section, or type a to-do…" autoComplete="off"
          onInput={(e) => { q.value = e.currentTarget.value; sel.value = 0; }}
          onKeyDown={(e) => {
            if (e.key === "ArrowDown" || e.key === "ArrowUp") {
              e.preventDefault();
              sel.value = (at + (e.key === "ArrowDown" ? 1 : -1) + items.length) % items.length;
            } else if (e.key === "Enter") { e.preventDefault(); run(at); }
            else if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); paletteOpen.value = false; }
          }} />
        <ul>
          {items.map((c, i) => (
            <li key={c.label} class={i === at ? "on" : ""} onClick={() => run(i)} onMouseMove={() => { sel.value = i; }}>
              <c.icon /><span>{c.label}</span>{c.hint && <span class="k">{c.hint}</span>}
            </li>
          ))}
        </ul>
        <div class="foot"><span><kbd>↑</kbd> <kbd>↓</kbd> move</span><span><kbd>↵</kbd> open</span><span><kbd>esc</kbd> close</span></div>
      </div>
    </div>
  );
}

export function Toast() {
  if (!toastText.value) return null;
  const a = toastAction.value;
  return (
    <div class="toast" role="status">{toastText.value}
      {a && <button class="btn primary sm" onClick={() => { toast(""); a.run(); }}>{a.label}</button>}
    </div>
  );
}

// Panel keyboard shortcuts, checked before the voice screen's (main.tsx). True if handled.
export function panelKey(e: KeyboardEvent): boolean {
  const typing = !!(e.target as Element).closest?.("input, textarea");
  if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
    e.preventDefault();
    paletteOpen.value = !paletteOpen.peek();
    return true;
  }
  if (e.key === "Escape") {
    if (paletteOpen.peek()) { paletteOpen.value = false; return true; }
    if (ask.peek() != null) { tuck(); return true; }
    if (activityOpen.peek()) { toggleActivity(false); return true; }
    if (!wide.peek() && sheet.peek() !== "closed") { closeSheet(); return true; }
    return false;
  }
  if (typing || e.metaKey || e.ctrlKey || e.altKey) return false;
  const id = ask.peek();
  if (id != null && !askGuard.peek() && (e.key === "y" || e.key === "n")) { answer(id, e.key === "y"); return true; }
  if (!wide.peek()) return false;
  if (e.key === "?") { go("settings", "keys"); return true; }
  if (e.key === "\\") { setSideOff(!sideOff.peek()); return true; }
  return false;
}

