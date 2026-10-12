// Settings: a list of pages, opening beside the list (wide) or in place (narrow).
// Secrets (MCP server keys, JARVIS's own keys) are entered here write-only so they're never spoken aloud.
// Adding a connection is by voice with approval (Connections.tsx).
import type { ComponentChildren } from "preact";
import { useEffect } from "preact/hooks";
import { useSignal } from "@preact/signals";
import { nativePlatform } from "../../lib/native";
import { recordMic, type Recording } from "../../lib/record";
import { openAppSettings, refreshAppPush, toggleNotifications } from "../../state/push";
import { showMicModes } from "../../state/session";
import { appNotify, connected, micMode, now, pushOn, setPasscode, showLogin } from "../../state/store";
import {
  about, access, accounts, act, ago, devices, forgetRule, go, item, load, memory, prefs, quiet, roomy, rules, servers, setHold, skills,
  system, toast, vocabulary, voiceprints, wide, type Access, type Account, type Device, type JarvisKey, type Login,
  type Memory as MemoryData, type MemorySection, type PrefKey, type Prefs, type Rule, type SettingsItem,
  type Vocabulary as VocabularyData, type Voiceprints, type Word,
} from "../../state/panel";
import {
  BellIcon, BrainIcon, CheckIcon, ChevronIcon, EditIcon, KeyIcon, KeysIcon, LockIcon, MachineIcon, MoonIcon, NoteIcon,
  PlugIcon, PrefsIcon, PulseIcon, ShieldIcon, SkillIcon, TrashIcon, UserIcon, WordsIcon,
} from "../icons";
import { cap, Empty, InlineForm, Led, Pill } from "./bits";
import { Markdown, MarkdownEditor } from "./Markdown";
import { ConnectionsPage } from "./Connections";
import { GoogleAccountsPage } from "./GoogleAccounts";
import { MachinesPage, machinesSummary } from "./Machines";
import { SkillsPage } from "./Skills";

export const SETTINGS_TITLES: Record<SettingsItem, string> = {
  conn: "Connections", accounts: "Google accounts", memory: "Memory", vocabulary: "Vocabulary", skills: "Skills", approvals: "Approvals", about: "About you",
  prefs: "Behaviour", dnd: "Do not disturb", safety: "Safety check", notify: "Notifications", system: "System", access: "Logins and keys",
  keys: "Keyboard shortcuts", machines: "Machines",
};

function skillsSummary(): string {
  const all = skills.value;
  if (!all) return "How-tos JARVIS follows for your workflows";
  const on = all.filter((s) => s.state === "on").length, waiting = all.filter((s) => s.needs_approval).length;
  if (!all.length) return "None yet: write or install one by voice";
  return `${on} on${waiting ? ` · ${waiting} need${waiting === 1 ? "s" : ""} approving` : ""} · ${all.length} in all`;
}

const lacking = () => (servers.value ?? []).filter((s) => s.enabled && s.problem);
const down = () => (system.value?.services ?? []).filter((s) => s.state !== "running");

function connSummary(): [string, "ok" | "warn" | null] {
  const all = servers.value;
  if (!all) return ["Tools from MCP servers, and their keys", null];
  const on = all.filter((s) => s.enabled).map((s) => s.label), bad = lacking();
  const setting = all.filter((s) => !s.reviewed).map((s) => s.label);
  if (bad.length) return [`${bad.map((s) => s.label).join(", ")} can't start`, "warn"];
  if (setting.length) return [`${setting.join(", ")} being set up`, "warn"];
  return on.length ? [`${on.join(", ")} on`, "ok"] : ["None on", null];
}

function systemSummary(): [string, "ok" | "warn" | null] {
  const s = system.value;
  if (!s) return ["Services and the vault", null];
  const stopped = down();
  if (stopped.length) return [`${stopped.map((x) => x.name).join(", ")} ${stopped.length > 1 ? "aren't" : "isn't"} running`, "warn"];
  if (s.vault.state !== "ok") return ["The vault needs attention", "warn"];
  return ["All running", "ok"];
}

function Row({ to, icon, title, sub, led, sec = "settings" }: {
  to: SettingsItem; icon: ComponentChildren; title: string; sub: string; led?: "ok" | "warn" | null; sec?: "settings" | "jarvis";
}) {
  return (
    <button class={`srow${item.value === to ? " sel" : ""}`} onClick={() => go(sec, to)}>
      {icon}<div class="grow"><div>{title}</div><div class="sub">{sub}</div></div>
      {led && <Led kind={led} />}<ChevronIcon />
    </button>
  );
}

// The JARVIS tab: what it can do and what it knows, most visited first
export const JARVIS_PAGES: SettingsItem[] = ["skills", "conn", "accounts", "memory", "vocabulary", "about"];

function accountsSummary(acc: Account[] | null): { sub: string; led: "ok" | "warn" | null } {
  if (!acc) return { sub: "Mail, calendar and Chat", led: null };
  if (!acc.length) return { sub: "None yet: add one to use mail and calendar", led: null };
  const out = acc.filter((a) => !a.signed_in).map((a) => a.label);
  if (out.length) return { sub: `${out.join(", ")} ${out.length > 1 ? "aren't" : "isn't"} signed in`, led: "warn" };
  return { sub: acc.map((a) => a.label + (a.default ? " (default)" : "")).join(" · "), led: "ok" };
}

export function JarvisList() {
  const [conn, connLed] = connSummary(), acc = accounts.value;
  return (
    <div class="settings">
      <div class="grp">Can do</div>
      <Row sec="jarvis" to="skills" icon={<SkillIcon />} title="Skills" sub={skillsSummary()} />
      <Row sec="jarvis" to="conn" icon={<PlugIcon />} title="Connections" sub={conn} led={connLed} />
      <Row sec="jarvis" to="accounts" icon={<UserIcon />} title="Google accounts" {...accountsSummary(acc)} />
      <div class="grp">Knows</div>
      <Row sec="jarvis" to="memory" icon={<BrainIcon />} title="Memory" sub="What JARVIS remembers about you" />
      <Row sec="jarvis" to="vocabulary" icon={<WordsIcon />} title="Vocabulary" sub={vocabularySummary()} />
      <Row sec="jarvis" to="about" icon={<NoteIcon />} title="About you" sub="Your name, time zone, and what JARVIS should know" />
    </div>
  );
}

// How JARVIS behaves and stays safe, most visited first; every page one tap away, its row saying how it stands
export function SettingsList() {
  const [sys, sysLed] = systemSummary(), [dnd, dndLed] = dndSummary();
  const [keys, keysLed] = accessSummary(), [safe, safeLed] = safetySummary(), [mac, macLed] = machinesSummary();
  return (
    <div class="settings">
      <div class="grp">General</div>
      <Row to="dnd" icon={<MoonIcon />} title="Do not disturb" sub={dnd} led={dndLed} />
      <Row to="prefs" icon={<PrefsIcon />} title="Behaviour" sub={behaviourSummary()} />
      <Row to="notify" icon={<BellIcon />} title="Notifications" sub={pushOn.value ? "On for this device" : "Off for this device"} />
      <div class="grp">Security</div>
      <Row to="approvals" icon={<CheckIcon />} title="Approvals" sub={rulesSummary()} />
      <Row to="access" icon={<KeyIcon />} title="Logins and keys" sub={keys} led={keysLed} />
      <Row to="safety" icon={<ShieldIcon />} title="Safety check" sub={safe} led={safeLed} />
      <div class="grp">System</div>
      <Row to="system" icon={<PulseIcon />} title="System" sub={sys} led={sysLed} />
      <Row to="machines" icon={<MachineIcon />} title="Machines" sub={mac} led={macLed} />
      {wide.value && <Row to="keys" icon={<KeysIcon />} title="Keyboard shortcuts" sub="Talk, search and answer without the mouse · press ?" />}
      <button class="srow lock" onClick={() => { if (confirm("Forget the passcode on this device?")) { setPasscode(null); showLogin(); } }}>
        <LockIcon /><div class="grow"><div>Lock</div><div class="sub">Forget the passcode on this device</div></div>
      </button>
    </div>
  );
}

function Fact({ line }: { line: string }) {
  const editing = useSignal(false);
  const text = line.replace(/^- /, "").replace(/ \(\d{4}-\d{2}-\d{2}\)$/, "");
  async function change(fact: string) {
    const r = await act<MemoryData>("memory_edit", { line, fact }, fact ? "Saved" : "Forgotten");
    if (r) memory.value = r;
    return !!r;
  }
  return (
    <li class="row">
      <div class="grow">
        {editing.value ? <InlineForm placeholder="The fact" label="Save" initial={text} send={change} /> : <Markdown text={text} inline />}
      </div>
      <span class="rt">
        <button class="ico" title="Edit" aria-label="Edit" onClick={() => { editing.value = !editing.value; }}><EditIcon /></button>
        <button class="ico" title="Forget" aria-label="Forget" onClick={() => { if (confirm("Forget this?")) change(""); }}><TrashIcon /></button>
      </span>
    </li>
  );
}

// Memory sections; JARVIS stays under each limit by merging and shortening facts
const SECTIONS: { key: MemorySection; label: string; placeholder: string }[] = [
  { key: "user", label: "User", placeholder: "A fact about you: a preference, habit or goal" },
  { key: "environment", label: "Environment", placeholder: "A fact about your machine, setup, tools or projects" },
];

function Usage({ used, max }: { used: number; max: number }) {
  const share = used / max, kind = share > 1 ? "bad" : share >= 0.9 ? "warn" : "";
  return (
    <>
      <span class={`use ${kind}`}>{used.toLocaleString()} / {max.toLocaleString()}</span>
      <span class={`meter ${kind}`} role="meter" aria-label="How full" aria-valuemin={0} aria-valuemax={max} aria-valuenow={used}>
        <i style={{ width: `${Math.min(share, 1) * 100}%` }} />
      </span>
    </>
  );
}

function Memory() {
  const all = memory.value, q = useSignal(""), into = useSignal<MemorySection>("user");
  if (!all) return <Empty>Loading…</Empty>;
  const words = q.value.toLowerCase().split(/\s+/).filter(Boolean);
  const total = all.user.length + all.environment.length;
  const adding = SECTIONS.find((s) => s.key === into.value)!;
  return (
    <>
      <div class="radios" role="radiogroup" aria-label="Section">
        {SECTIONS.map((s) => (
          <button type="button" role="radio" aria-checked={into.value === s.key} class={into.value === s.key ? "on" : ""}
            onClick={() => { into.value = s.key; }}>{s.label}</button>
        ))}
      </div>
      <InlineForm placeholder={adding.placeholder} label="Remember" primary send={async (fact) => {
        const r = await act<MemoryData>("remember", { fact, section: into.value }, "Remembered");
        if (r) memory.value = r;
        return !!r;
      }} />
      {total > 6 && <input class="field search" placeholder="Search" aria-label="Search memory" value={q.value}
        onInput={(e) => { q.value = e.currentTarget.value; }} />}
      {SECTIONS.map((s) => {
        const found = all[s.key].filter((l) => words.every((w) => l.toLowerCase().includes(w)));
        return (
          <div key={s.key}>
            <div class="grp">{s.label}<Usage {...all.limits[s.key]} /></div>
            <ul class="list">{found.map((l) => <Fact key={l} line={l} />)}</ul>
            {!found.length && <Empty>{all[s.key].length ? "Nothing matches." : "Nothing remembered yet."}</Empty>}
          </div>
        );
      })}
      <p class="hint">Each section has a size limit, and no fact may be over {all.limits.fact} characters. When one fills up, JARVIS merges or shortens facts, and asks before dropping any.</p>
    </>
  );
}

// ── Vocabulary: expected words and their mishearings ──

function vocabularySummary() {
  const v = vocabulary.value;
  if (!v) return "Names and words speech recognition should expect";
  const n = v.entries.length + v.projects.length;
  return `${n} word${n === 1 ? "" : "s"} · ${v.entries.length ? v.entries.slice(0, 3).map((e) => e.word).join(", ") : "your projects"}`;
}

const heardList = (text: string) => text.split(",").map((h) => h.trim()).filter(Boolean);

// Parse "word, misheard, ...": adds an entry, or with `old` rewrites that one
function WordForm({ old, initial, done }: { old?: string; initial?: Word; done?: () => void }) {
  const word = useSignal(initial?.word ?? ""), heard = useSignal(initial?.heard.join(", ") ?? ""), busy = useSignal(false);
  async function submit(e: Event) {
    e.preventDefault();
    if (!word.value.trim() || busy.value) return;
    busy.value = true;
    const r = await act<VocabularyData>(old ? "vocabulary_save" : "vocabulary_add",
      { old, word: word.value.trim(), heard: heardList(heard.value) }, old ? "Saved" : "Added");
    busy.value = false;
    if (!r) return;
    vocabulary.value = r;
    word.value = heard.value = "";
    done?.();
  }
  return (
    <form class="inline wrap" onSubmit={submit}>
      <input class="field" placeholder="Word or name, spelt right" aria-label="Word" value={word.value}
        onInput={(e) => { word.value = e.currentTarget.value; }} />
      <input class="field" placeholder="Misheard as (optional, commas)" aria-label="Misheard as" value={heard.value}
        onInput={(e) => { heard.value = e.currentTarget.value; }} />
      <button class="btn sm primary" disabled={busy.value || !word.value.trim()}>{old ? "Save" : "Add"}</button>
    </form>
  );
}

function WordRow({ w }: { w: Word }) {
  const editing = useSignal(false);
  async function remove() {
    if (!confirm(`Remove ${w.word} from the vocabulary?`)) return;
    const r = await act<VocabularyData>("vocabulary_remove", { word: w.word }, "Removed");
    if (r) vocabulary.value = r;
  }
  return (
    <li class="row">
      <div class="grow">
        {editing.value ? <WordForm old={w.word} initial={w} done={() => { editing.value = false; }} /> : <>
          <div><b>{w.word}</b> {w.learned && <Pill kind="muted">learned</Pill>}</div>
          <div class="sub">{w.heard.length ? `Misheard as ${w.heard.join(", ")}` : "No mishearings known"}{w.date ? ` · ${w.date}` : ""}</div>
        </>}
      </div>
      <span class="rt">
        <button class="ico" title="Edit" aria-label="Edit" onClick={() => { editing.value = !editing.value; }}><EditIcon /></button>
        <button class="ico" title="Remove" aria-label="Remove" onClick={remove}><TrashIcon /></button>
      </span>
    </li>
  );
}

function VocabularyPage() {
  const v = vocabulary.value, q = useSignal("");
  if (!v) return <Empty>Loading…</Empty>;
  const words = q.value.toLowerCase().split(/\s+/).filter(Boolean);
  const found = v.entries.filter((e) => words.every((w) => [e.word, ...e.heard].join(" ").toLowerCase().includes(w)));
  return (
    <>
      <WordForm />
      {v.entries.length > 8 && <input class="field search" placeholder="Search" aria-label="Search vocabulary" value={q.value}
        onInput={(e) => { q.value = e.currentTarget.value; }} />}
      <div class="grp">Words<span class="use">{v.entries.length} / {v.max}</span></div>
      <ul class="list">{found.map((w) => <WordRow key={w.word} w={w} />)}</ul>
      {!found.length && <Empty>{v.entries.length ? "Nothing matches." : "No words yet."}</Empty>}
      {v.projects.length > 0 && <>
        <div class="grp">From your projects</div>
        <ul class="list">{v.projects.map((p) => <li key={p} class="row"><div class="grow">{p}</div></li>)}</ul>
      </>}
      <p class="hint">Speech recognition is told to expect these{v.prompt ? "" : " (its prompt is off: JARVIS_STT_PROMPT)"}, and what
        it's misheard them as is put right, whole words only. JARVIS adds words it notices it misheard after each
        conversation, marked learned; when the list is full, the oldest learned word makes room. Changes apply from the
        next conversation.</p>
    </>
  );
}

// ── Remembered approvals (created from the approval card; withdrawn here) ──

function rulesSummary() {
  const all = rules.value;
  if (!all) return "Commands JARVIS runs without asking";
  return all.length ? `${all.length} remembered` : "None: every risky command asks";
}

const VIA: Record<string, string> = { voice: "by voice", panel: "in the panel", assistant: "by telling JARVIS", cli: "on JARVIS's computer" };

function RuleRow({ r }: { r: Rule }) {
  return (
    <li class="row">
      <div class="grow">
        <div>{cap(r.covers)} {r.scope === "always" ? <Pill kind="running">always</Pill> : <Pill kind="muted">session</Pill>}</div>
        <div class="sub">{cap(r.who)} · made {VIA[r.via] ?? r.via} {ago(r.created, now.value)} from <code>{r.example}</code></div>
        <div class="sub">{r.uses ? `Used ${r.uses} time${r.uses === 1 ? "" : "s"}, last ${ago(r.last_used ?? r.created, now.value)}` : "Not used yet"}</div>
      </div>
      <button class="btn sm" onClick={() => { if (confirm("Withdraw this? Commands it covers will ask again.")) forgetRule(r); }}>Withdraw</button>
    </li>
  );
}

function Approvals() {
  const all = rules.value;
  if (!all) return <Empty>Loading…</Empty>;
  return (
    <>
      <ul class="list">{all.map((r) => <RuleRow key={r.id} r={r} />)}</ul>
      {!all.length && <Empty>Nothing remembered: every risky command asks.</Empty>}
      <p class="hint">Made when you answer a request "for this task" or "always", here or by voice. Some always ask, every
        time: deleting files, git push, sending data off the computer, changing system settings, anything touching secrets.
        You can also ask JARVIS what it always allows, or to withdraw one.</p>
    </>
  );
}

// ── Preferences (Behaviour, Safety check): stored server-side; an .env value wins and is read-only here ──

const AT_RESTART = "Saved. JARVIS uses it once its services restart.";
const NEXT_CALL = "Saved. JARVIS uses it from the next conversation.";
const pref = (k: PrefKey) => prefs.value?.preferences[k];
const shown = (k: PrefKey) => { const p = pref(k); return p ? p.env || p.value || p.default : ""; };

async function savePref(key: PrefKey, value: string, done: string) {
  const r = await act<Prefs>("preference_save", { key, value }, done);
  if (r) prefs.value = r;
  return !!r;
}

const ACCENT: Record<string, string> = { a: "American", b: "British" };
const voiceName = (v: string) => `${cap(v.slice(3))} · ${ACCENT[v.charAt(0)] ?? ""} ${v.charAt(1) === "m" ? "male" : "female"}`;

// Summary lines for the list
function behaviourSummary(): string {
  const v = shown("voice");
  if (!v) return "Voice, voice ID and background work";
  const id = !voiceprints.value ? "" : !voiceprints.value.owner ? " · answers any voice"
    : shown("speaker_check") === "off" ? " · voice ID off" : " · knows your voice";
  return `${cap(v.slice(3))} · ${shown("voice_speed")}×${id}`;
}

function dndSummary(): [string, "warn" | null] {
  const q = quiet.value;
  if (!q) return ["Hold reminders, jobs and alerts", null];
  return q.hold ? [cap(q.hold.said), "warn"] : ["Off", null];
}

function safetySummary(): [string, "warn" | null] {
  if (!pref("injection_check")) return ["Checks what JARVIS reads for hidden instructions", null];
  if (shown("injection_check") === "off") return ["Off: nothing is checked", "warn"];
  const by = INJECTION_MODEL.find(([m]) => m === shown("injection_model"))?.[1].split(" ")[0] ?? "a fast model";
  return [`On · checked by ${by}`, null];
}

const FromEnv = ({ label, value }: { label: string; value: string }) => (
  <><label class="lbl">{label}</label><div class="field locked">{value}</div>
    <p class="hint">Set in .env on JARVIS's computer, which wins: take it out there to change it here.</p></>
);

// Free-text preference, saved on button press. Empty resets to the default (shown as placeholder).
function PrefField({ k, label, type = "text", list, done = AT_RESTART }: { k: PrefKey; label: string; type?: string; list?: string; done?: string }) {
  const p = pref(k)!, text = useSignal(p.value);
  useEffect(() => { text.value = p.value; }, [p.value]);
  if (p.env) return <FromEnv label={label} value={p.env} />;
  return (
    <>
      <label class="lbl">{label}</label>
      <form class="inline" onSubmit={(e) => { e.preventDefault(); savePref(k, text.value, done); }}>
        <input class="field" type={type} list={list} value={text.value} placeholder={p.default} aria-label={label}
          onInput={(e) => { text.value = e.currentTarget.value; }} />
        <button class="btn primary sm" disabled={text.value.trim() === p.value}>Save</button>
      </form>
    </>
  );
}

// Saved on selection
function PrefChoice({ k, label, options }: { k: PrefKey; label: string; options: [string, string][] }) {
  const p = pref(k)!, now = p.value || p.default;
  if (p.env) return <FromEnv label={label} value={p.env} />;
  return (
    <>
      <label class="lbl">{label}</label>
      <div class="radios" role="radiogroup" aria-label={label}>
        {options.map(([value, text]) => (
          <button key={value} type="button" role="radio" aria-checked={value === now} class={value === now ? "on" : ""}
            onClick={() => { if (value !== now) savePref(k, value === p.default ? "" : value, NEXT_CALL); }}>{text}</button>
        ))}
      </div>
    </>
  );
}

const zones = (): string[] => (Intl as unknown as { supportedValuesOf?: (k: string) => string[] }).supportedValuesOf?.("timeZone") ?? [];

function About() {
  const text = useSignal<string | null>(null);
  useEffect(() => { if (about.value != null && text.peek() == null) text.value = about.value; }, [about.value]);
  if (about.value == null || text.value == null || !prefs.value) return <Empty>Loading…</Empty>;
  const dirty = text.value.trim() !== about.value;
  return (
    <>
      <PrefField k="name" label="What JARVIS calls you" />
      <PrefField k="timezone" label="Time zone" list="zones" />
      <datalist id="zones">{zones().map((z) => <option key={z} value={z} />)}</datalist>
      <p class="hint">Left empty, taken from JARVIS's computer: {pref("name")!.default} and {pref("timezone")!.default}.</p>
      <MarkdownEditor label="What JARVIS should know" value={text.value} onInput={(v) => { text.value = v; }}
        placeholder="Your job, your own projects, what to keep apart. One point per line. Markdown works." />
      <div class="acts end">
        <button class="btn primary sm" disabled={!dirty} onClick={async () => {
          const r = await act<{ text: string }>("about_save", { text: text.value }, "Saved. JARVIS uses it once its services restart.");
          if (r) { about.value = r.text; text.value = r.text; }
        }}>Save</button>
      </div>
      <p class="hint">JARVIS reads this when its services start: make services-restart on JARVIS's computer.</p>
    </>
  );
}

// Choices plus the current value if it isn't one of them (set via .env or on JARVIS's computer)
const withShown = (k: PrefKey, options: [string, string][], label = (v: string) => v): [string, string][] =>
  options.some(([v]) => v === shown(k)) ? options : [...options, [shown(k), label(shown(k))]];

const SPEEDS: [string, string][] = ["0.9", "1", "1.05", "1.1", "1.2"].map((s) => [s, `${s}×`]);
const GATE: [string, string][] = [["enforce", "Drop"], ["shadow", "Only log"], ["off", "Off"]];
const GATE_HINT: Record<string, string> = {
  enforce: "Clips with no real speech in them, like Whisper's phantom \"Thank you.\" on silence, never reach JARVIS.",
  shadow: "Clips it would drop are only logged, and saved as audio on JARVIS's computer, to check what it would catch.",
  off: "Everything Whisper hears reaches JARVIS.",
};

const BROWSER: [string, string][] = [["on", "On"], ["off", "Off"]];
const BROWSER_WINDOW: [string, string][] = [["headed", "Shown"], ["headless", "Hidden"]];
const INJECTION_MODEL: [string, string][] = [["claude-haiku-4-5-20251001", "Haiku (fastest)"], ["claude-sonnet-5", "Sonnet (more careful)"]];

// Do not disturb (same hold as "pause everything" by voice). Switch: until turned off. Buttons: for an hour or
// until a time; they also change the end while on.
function DoNotDisturb() {
  const q = quiet.value, busy = useSignal(false), until = useSignal("");
  if (!q) return <Empty>Loading…</Empty>;
  const h = q.hold;
  const set = async (to: Parameters<typeof setHold>[0]) => { busy.value = true; await setHold(to); busy.value = false; };
  return (
    <>
      <div class="row flat">
        <div class="grow"><div>Do not disturb</div><div class="sub">{h ? cap(h.said) : "Off"}</div></div>
        <button class="switch" role="switch" aria-checked={!!h} aria-label="Do not disturb" disabled={busy.value}
          onClick={() => set(h ? null : {})} />
      </div>
      <div class="holdbar">
        <span>{h ? "Change the end:" : "Or only for:"}</span>
        <button class="btn sm" disabled={busy.value} onClick={() => set({ minutes: 60 })}>1 hour</button>
        <form class="inline" onSubmit={(e) => { e.preventDefault(); if (until.value) set({ until: until.value }); }}>
          <input class="field" type="time" aria-label="Until" value={until.value}
            onInput={(e) => { until.value = e.currentTarget.value; }} />
          <button class="btn sm" disabled={busy.value || !until.value}>Until then</button>
        </form>
      </div>
      <p class="hint">While it's on, no reminder, scheduled task, watch or command runs, JARVIS keeps its own notices
        back and finished tasks don't notify; requests for a yes still come through. Held reminders and jobs run when it ends. The nightly quiet hours
        stay as they are.</p>
    </>
  );
}

const MODES: Record<string, string> = { standard: "Standard", voiceIsolation: "Voice Isolation", wideSpectrum: "Wide Spectrum" };
const modeName = (m: string) => MODES[m] ?? "Other";
const iosApp = nativePlatform() === "ios";

// iOS app: Voice Isolation Mic Mode. Apps can't set it, so the switch opens the system picker (iOS remembers the
// choice per app). Session only: the picker needs a live mic.
function MicModeRow() {
  if (!iosApp) return null;
  const m = micMode.value;
  let sub = "Set during a conversation";
  if (m) {
    sub = `Mic: ${modeName(m.active)}`;
    if (m.preferred !== m.active) sub += ` (${modeName(m.preferred)} not available here)`;
  }
  return (
    <div class="row flat">
      <div class="grow"><div>Voice Isolation</div><div class="sub">{sub}</div></div>
      <button class="switch" role="switch" aria-checked={m?.active === "voiceIsolation"} aria-label="Voice Isolation"
        title="Opens iOS's Mic Mode picker" disabled={!m} onClick={showMicModes} />
    </div>
  );
}

function VoicePrefs({ all }: { all: Prefs }) {
  const v = all.preferences.voice, now = v.value || v.default;
  const group = (accent: string) => all.voices.filter((x) => x.charAt(0) === accent);
  return (
    <>
      {v.env ? <FromEnv label="JARVIS's voice" value={v.env} /> : <>
        <label class="lbl">JARVIS's voice</label>
        <select class="field" value={now} aria-label="JARVIS's voice"
          onChange={(e) => { const x = e.currentTarget.value; savePref("voice", x === v.default ? "" : x, NEXT_CALL); }}>
          {Object.entries(ACCENT).reverse().map(([a, name]) => group(a).length ? (
            <optgroup key={a} label={name}>{group(a).map((x) => <option key={x} value={x}>{voiceName(x)}{x === v.default ? " (default)" : ""}</option>)}</optgroup>
          ) : null)}
        </select>
      </>}
      <PrefChoice k="voice_speed" label="Speed" options={withShown("voice_speed", SPEEDS, (s) => `${s}×`)} />
      <PrefChoice k="speech_gate" label="Phantom filter" options={GATE} />
      <p class="hint">{GATE_HINT[shown("speech_gate")] ?? ""} Changes apply from the next conversation.</p>
    </>
  );
}

function BehaviourPage() {
  const all = prefs.value;
  return (
    <>
      <div class="grp">Voice & speech</div>
      <MicModeRow />
      {all ? <VoicePrefs all={all} /> : <Empty>Loading…</Empty>}
      <VoiceId />
      {all && <>
        <div class="grp">Background work</div>
        <PrefField k="max_tasks" label="Background tasks at once" type="number" />
        <p class="hint">The rest wait their turn. Applies once the task runner restarts.</p>
        <PrefChoice k="browser" label="Web browser" options={BROWSER} />
        <PrefChoice k="browser_window" label="Browser window" options={BROWSER_WINDOW} />
        <p class="hint">JARVIS's own Chrome, on a profile of its own, for reading pages you're logged in to: log in there yourself.
          It only reads pages for now. Changes apply from the next conversation.</p>
      </>}
    </>
  );
}

function SafetyPage() {
  if (!prefs.value) return <Empty>Loading…</Empty>;
  return (
    <>
      <PrefChoice k="injection_check" label="Hidden instruction check" options={BROWSER} />
      <PrefChoice k="injection_model" label="Checked by" options={INJECTION_MODEL} />
      <p class="hint">Before JARVIS acts on an email, page, document, file or other tool result, a fast model checks it for
        instructions hidden there for the AI. Those that try to make JARVIS act are held back and you're told; milder ones
        come with a warning. Changes apply from the next conversation or task.</p>
    </>
  );
}

// ── Voice ID: speaker check and voiceprint enrolment ──

const SPEAKER: [string, string][] = [["on", "On"], ["shadow", "Only log"], ["off", "Off"]];
const SPEAKER_HINT: Record<string, string> = {
  on: "Speech that isn't your voice, like a TV or people nearby, never reaches JARVIS, and it stays silent.",
  shadow: "Voices it would ignore are only logged on JARVIS's computer (make voiceprint ARGS=log), to check before it ignores any.",
  off: "JARVIS answers any voice it hears.",
};
const STRICTNESS: [string, string][] = [["0.25", "Lenient"], ["0.35", "Normal"], ["0.45", "Strict"]];
const VIA_ENROLL: Record<string, string> = { voice: "by voice", panel: "here", cli: "on JARVIS's computer" };

function VoiceId() {
  const v = voiceprints.value, rec = useSignal<Recording | null>(null), secs = useSignal(0), lvl = useSignal(0);
  const busy = useSignal(false);
  useEffect(() => () => { rec.peek()?.cancel(); }, []);  // cancel recording on unmount
  useEffect(() => {
    const r = rec.value;
    if (!r) return;
    const timer = setInterval(() => {
      secs.value = r.seconds(); lvl.value = r.level();
      if (v && secs.value >= v.seconds) finish();
    }, 100);
    return () => clearInterval(timer);
  }, [rec.value]);
  if (!v || !prefs.value) return null;
  const owner = v.owner;

  async function start() {
    try {
      secs.value = 0;
      rec.value = await recordMic();
    } catch (e) {
      toast((e as Error).message);
    }
  }
  async function finish() {
    const r = rec.peek();
    if (!r || busy.peek()) return;
    busy.value = true;
    rec.value = null;
    const audio = r.stop();
    const got = await act<Voiceprints>("voiceprint_enroll", { audio }, "Saved. JARVIS knows your voice from the next conversation.");
    if (got) voiceprints.value = got;
    busy.value = false;
  }
  function cancel() { rec.peek()?.cancel(); rec.value = null; }
  async function remove() {
    if (!confirm("Forget your voiceprint? JARVIS will answer any voice again.")) return;
    const got = await act<Voiceprints>("voiceprint_remove", {}, "Voiceprint forgotten");
    if (got) voiceprints.value = got;
  }

  return (
    <>
      <div class="grp">Voice ID</div>
      <div class="row flat">
        <div class="grow">
          <div>{owner ? "Knows your voice" : "Not set up"} {owner && !owner.current && <Pill kind="waiting">enroll again</Pill>}</div>
          <div class="sub">{owner ? `Enrolled ${VIA_ENROLL[owner.via] ?? owner.via} ${ago(owner.created, now.value)} from ${Math.round(owner.seconds)} s of speech`
            : "Without a voiceprint JARVIS answers any voice, as it does now."}</div>
        </div>
        {owner && !rec.value && <button class="btn sm" onClick={remove}>Forget</button>}
      </div>
      {rec.value || busy.value ? (
        <div class="note">
          <div class="grow">
            <div>{busy.value ? "Learning your voice…" : `Recording: ${Math.floor(secs.value)} of ${v.seconds} s`}</div>
            <p class="hint">{v.passage}</p>
          </div>
          {!busy.value && <>
            <span class="meter" role="meter" aria-label="Mic level" aria-valuemin={0} aria-valuemax={1} aria-valuenow={lvl.value}>
              <i style={{ width: `${lvl.value * 100}%` }} />
            </span>
            <button class="btn sm primary" disabled={secs.value < 15} onClick={finish}>Done</button>
            <button class="btn sm" onClick={cancel}>Cancel</button>
          </>}
        </div>
      ) : (
        <div class="acts">
          <button class="btn sm primary" disabled={!v.model} onClick={start}>{owner ? "Enroll again" : "Enroll my voice"}</button>
        </div>
      )}
      {!v.model && <p class="hint warn">The speaker model isn't on JARVIS's computer yet: run make models there.</p>}
      <p class="hint">{connected.value && !rec.value ? "Hang up first: the conversation has the mic. " : ""}Read the text that
        appears aloud for {v.seconds} seconds, somewhere quiet. Or, in a conversation, say "learn my voice" and talk for half
        a minute: that hears you through the very mic and connection JARVIS listens on.</p>
      {owner && <>
        <PrefChoice k="speaker_check" label="Other voices" options={SPEAKER} />
        <p class="hint">{SPEAKER_HINT[shown("speaker_check")] ?? ""}</p>
        <PrefChoice k="speaker_threshold" label="How sure it must be" options={withShown("speaker_threshold", STRICTNESS)} />
        <p class="hint">Stricter ignores more of other people, and may miss you when you're far from the mic or your voice is
          hoarse. Short replies like "yes" need less, more so right after JARVIS has spoken. Changes apply from the next
          conversation.</p>
      </>}
    </>
  );
}

const DEVICE_NAMES: Record<string, string> = { iphone: "iPhone", android: "Android" };

function DeviceRow({ d }: { d: Device }) {
  return (
    <li class="row">
      <div class="grow"><div>{DEVICE_NAMES[d.device] ?? d.device}{d.this && <> <Pill kind="running">this device</Pill></>}</div>
        <div class="sub">{d.kind === "app" ? "JARVIS app" : "Web app"}{d.added ? ` · added ${ago(d.added, now.value)}` : ""}</div></div>
      <button class="btn sm" onClick={async () => {
        if (!confirm("Stop sending notifications to this device?")) return;
        const r = await act<Device[]>("device_remove", { id: d.id }, "Removed");
        if (r) devices.value = r;
      }}>Remove</button>
    </li>
  );
}

function Notifications() {
  useEffect(() => { refreshAppPush(); }, []);  // iOS app: permission may have changed since
  return (
    <>
      <div class="row flat">
        <div class="grow"><div>This device</div><div class="sub">Task results, requests for a yes, and alerts</div></div>
        <button class="switch" role="switch" aria-checked={pushOn.value} aria-label="Notifications on this device"
          onClick={async () => { await toggleNotifications(); load("settings", "notify"); }} />
      </div>
      {appNotify.value === "denied" && (
        <div class="row flat">
          <div class="grow"><div>Blocked in iOS Settings</div><div class="sub">Allow notifications for JARVIS there, then come back</div></div>
          <button class="btn sm" onClick={openAppSettings}>Open Settings</button>
        </div>
      )}
      <div class="grp">Devices</div>
      <ul class="list">{(devices.value ?? []).map((d) => <DeviceRow key={d.id} d={d} />)}</ul>
      {devices.value && !devices.value.length && <Empty>No devices get notifications yet.</Empty>}
    </>
  );
}

function SystemPage() {
  const s = system.value;
  if (!s) return <Empty>Loading…</Empty>;
  return (
    <>
      <ul class="list">
        {s.services.map((x) => (
          <li key={x.name} class="row"><Led kind={x.state === "running" ? "ok" : "bad"} />
            <div class="grow">{x.name}<div class="sub">{x.state === "running" && x.since ? `running · up ${ago(x.since, now.value).replace(" ago", "")}`
              : x.state === "stopped" && x.exit != null ? `stopped · exit ${x.exit}` : x.state}</div></div></li>
        ))}
        {!s.services.length && <li class="row"><div class="grow sub">launchd isn't available here.</div></li>}
        <li class="row"><Led kind={s.vault.state === "ok" ? "ok" : "bad"} />
          <div class="grow">Vault<div class="sub">{s.vault.state === "ok" ? `key matches · ${s.vault.secrets} secret${s.vault.secrets === 1 ? "" : "s"} kept` : s.vault.problem}</div></div></li>
      </ul>
      <div class="acts"><button class="btn sm" onClick={() => load("settings", "system")}>Check again</button></div>
      <p class="hint">A service that isn't running: make services-restart on JARVIS's computer, then make logs to see why.</p>
    </>
  );
}

// ── Logins (one passcode each) and JARVIS keys (write-only) ──

function accessSummary(): [string, "warn" | null] {
  const a = access.value;
  if (!a) return ["Who can connect, and JARVIS's own keys", null];
  const missing = a.keys.filter((k) => k.required && !k.source).map((k) => k.label);
  if (missing.length) return [`${missing.join(", ")} not set`, "warn"];
  const n = a.logins.length;
  return [`${n ? `${n} login${n === 1 ? "" : "s"}` : "The first-setup passcode"} · ${a.keys.filter((k) => k.source).length} of ${a.keys.length} keys set`, null];
}

// Create a login (no `name`) or rotate `name`'s passcode. The current device stays signed in after a change.
function PasscodeForm({ name, done }: { name?: string; done?: () => void }) {
  const who = useSignal(""), code = useSignal(""), busy = useSignal(false);
  async function submit(e: Event) {
    e.preventDefault();
    if (busy.value) return;
    busy.value = true;
    const r = await act<Access>(name ? "login_passcode" : "login_add", { name: name ?? who.value.trim(), passcode_new: code.value },
      name ? "New passcode saved" : "Login added");
    busy.value = false;
    if (!r) return;
    access.value = r;
    if (r.logins.some((l) => l.this && l.name === name)) setPasscode(code.value);
    who.value = code.value = "";
    done?.();
  }
  return (
    <form class="inline wrap" onSubmit={submit}>
      {!name && <input class="field" placeholder="Name" aria-label="Name" value={who.value} onInput={(e) => { who.value = e.currentTarget.value; }} />}
      <input class="field" type="password" autocomplete="new-password" placeholder={name ? "New passcode" : "Their passcode, 6 or more characters"}
        aria-label="Passcode" value={code.value} onInput={(e) => { code.value = e.currentTarget.value; }} />
      <button class="btn sm primary" disabled={busy.value || code.value.length < 6 || (!name && !who.value.trim())}>{name ? "Save" : "Add"}</button>
      {done && <button type="button" class="btn sm" onClick={done}>Cancel</button>}
    </form>
  );
}

function LoginRow({ l }: { l: Login }) {
  const editing = useSignal(false);
  async function remove() {
    if (!confirm(l.this ? "Remove your own login? This device will ask for a passcode again."
      : `Remove ${l.name}'s login? Their devices will ask for a passcode again.`)) return;
    const r = await act<Access>("login_remove", { name: l.name }, "Removed");
    if (r) access.value = r;
  }
  return (
    <li class="row">
      <div class="grow">
        <div><b>{l.name}</b> {l.owner && <Pill kind="done">owner</Pill>} {l.this && <Pill kind="running">this device</Pill>}</div>
        <div class="sub">Added {ago(l.created, now.value)} · {l.used ? `last call ${ago(l.used, now.value)}` : "no call yet"}</div>
        {editing.value && <PasscodeForm name={l.name} done={() => { editing.value = false; }} />}
      </div>
      <span class="rt">
        <button class="ico" title="New passcode" aria-label="New passcode" onClick={() => { editing.value = !editing.value; }}><EditIcon /></button>
        <button class="ico" title="Remove" aria-label="Remove" onClick={remove}><TrashIcon /></button>
      </span>
    </li>
  );
}

const FROM: Record<string, string> = { vault: "in the vault", ".env": "in .env until JARVIS restarts", file: "in its file until JARVIS restarts" };

function KeyRow({ k }: { k: JarvisKey }) {
  const open = useSignal(false), value = useSignal(""), busy = useSignal(false);
  async function save(e: Event) {
    e.preventDefault();
    if (!value.value.trim() || busy.value) return;
    busy.value = true;
    const r = await act<Access>("key_set", { name: k.name, value: value.value }, AT_RESTART);
    busy.value = false;
    if (r) { access.value = r; value.value = ""; open.value = false; }
  }
  async function forget() {
    if (!confirm(`Forget the ${k.label}?`)) return;
    const r = await act<Access>("key_clear", { name: k.name }, "Forgotten");
    if (r) access.value = r;
  }
  return (
    <div class="key">
      <span class="var">{k.label}</span>
      {k.source ? <span class="st"><span class="dots">••••••</span> {FROM[k.source]}</span> : <span class="st warn">not set</span>}
      {open.value ? (
        <form class="inline grow" onSubmit={save}>
          <input class="field" type="password" autocomplete="off" placeholder={`Paste the ${k.label}`} aria-label={k.label}
            value={value.value} onInput={(e) => { value.value = e.currentTarget.value; }} />
          <button class="btn sm primary" disabled={busy.value || !value.value.trim()}>Save</button>
          <button type="button" class="btn sm" onClick={() => { open.value = false; value.value = ""; }}>Cancel</button>
        </form>
      ) : <>
        <button class="btn sm" onClick={() => { open.value = true; }}>{k.source ? "Replace" : "Enter"}</button>
        {k.source === "vault" && !k.required && <button class="btn sm" onClick={forget}>Forget</button>}
      </>}
      <div class="sub">{k.hint}</div>
    </div>
  );
}

function AccessPage() {
  const a = access.value;
  if (!a) return <Empty>Loading…</Empty>;
  return (
    <>
      <div class="grp">Logins</div>
      <ul class="list">{a.logins.map((l) => <LoginRow key={l.name} l={l} />)}</ul>
      {!a.logins.length && <Empty>None yet: the passcode in .env lets you in, and becomes your login when JARVIS restarts.</Empty>}
      <PasscodeForm />
      <p class="hint">Each person has a passcode of their own, and it's all they enter: it says who's connecting, so no two
        logins share one. Passcodes are kept only as hashes on JARVIS's computer. Everyone with a login can open this panel.</p>
      <div class="grp">JARVIS's keys</div>
      {a.keys.map((k) => <KeyRow key={k.name} k={k} />)}
      <p class="hint">Kept encrypted in the vault, and never shown back: paste them here, never out loud. JARVIS uses a new
        one once its services restart: make services-restart on JARVIS's computer.</p>
    </>
  );
}

const KEYS: [string, [string, string][]][] = [
  ["Voice", [["Connect, or mute and unmute", "Space"], ["Mute or unmute", "M"], ["Speaker off or on: replies in text only", "S"], ["End the conversation", "Esc"]]],
  ["Requests for a yes", [["Approve once", "Y"], ["Deny", "N"], ["Later, keep it in Tasks", "Esc"]]],
  ["Getting around", [["Search, jump to a task, add a to-do", "⌘K"], ["Switch between Home and the last section", "\\"], ["Close what's open", "Esc"], ["This list", "?"]]],
];

function Keys() {
  return (
    <>
      {KEYS.map(([group, keys]) => (
        <div key={group}>
          <div class="grp">{group}</div>
          <ul class="list keys">{keys.map(([what, key]) => <li key={what} class="row"><div class="grow">{what}</div><kbd>{key}</kbd></li>)}</ul>
        </div>
      ))}
      <p class="hint">Shortcuts work anywhere except while you're typing in a box.</p>
    </>
  );
}

const PAGES: Record<SettingsItem, () => preact.JSX.Element> = {
  conn: ConnectionsPage, accounts: GoogleAccountsPage, memory: Memory, vocabulary: VocabularyPage, skills: SkillsPage, approvals: Approvals, about: About, prefs: BehaviourPage, dnd: DoNotDisturb, safety: SafetyPage,
  notify: Notifications, system: SystemPage, access: AccessPage, keys: Keys, machines: MachinesPage,
};

export function SettingsPage({ page }: { page: SettingsItem }) {
  const Page = PAGES[page];
  return <div class="spage">{roomy.value && <h2 class="ptitle">{SETTINGS_TITLES[page]}</h2>}<Page /></div>;
}

// A list of pages, the open one beside it (roomy) or in its place
function Pages({ List, first }: { List: () => preact.JSX.Element; first: SettingsItem }) {
  const page = item.value as SettingsItem | null;
  useEffect(() => { act<Account[]>("accounts").then((r) => { if (r) accounts.value = r; }); }, []);
  if (roomy.value) {
    return (
      <div class="split">
        <div class="col lead"><List /></div>
        <div class="col"><SettingsPage page={page && page in PAGES ? page : first} /></div>
      </div>
    );
  }
  return page && page in PAGES ? <SettingsPage page={page} /> : <List />;
}

export const SettingsSection = () => <Pages List={SettingsList} first="prefs" />;
export const JarvisSection = () => <Pages List={JarvisList} first="skills" />;
