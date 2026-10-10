// Heartbeat sources: toggle and configure each (per user, re-read every beat). Multi-instance kinds (e.g. a Gmail
// search) can be added here, by voice or by a skill; only those can be removed.
import { useSignal } from "@preact/signals";
import { act, heartbeat, type Heartbeat, type HeartbeatSource, type SourceKind, type SourceSetting } from "../../state/panel";
import { EditIcon, TrashIcon } from "../icons";
import { Empty, Pill } from "./bits";

async function save(action: string, fields: Record<string, unknown>, done: string) {
  const r = await act<Heartbeat>(action, fields, done);
  if (r) heartbeat.value = r;
  return !!r;
}

export function heartbeatSummary(): string {
  const all = heartbeat.value?.sources;
  if (!all) return "What it checks to flag what's new";
  const on = all.filter((s) => s.on && s.ready);
  if (!on.length) return "All off: nothing is flagged";
  return `${on.length} on · ${on.slice(0, 4).map((s) => s.title).join(", ")}${on.length > 4 ? "…" : ""}`;
}

// Source settings form, saved together. Editing: empty resets to default (the placeholder). Adding (`adding`):
// needs a title and every setting.
function SourceForm({ settings, adding = false, send }: {
  settings: SourceSetting[]; adding?: boolean; send: (values: Record<string, string>, title: string) => Promise<boolean>;
}) {
  const values = useSignal<Record<string, string>>(
    Object.fromEntries(settings.map((x) => [x.key, adding || x.value === x.default ? "" : x.value])));
  const title = useSignal(""), busy = useSignal(false);
  const ready = !adding || (!!title.value.trim() && settings.every((x) => values.value[x.key]?.trim()));
  async function submit(e: Event) {
    e.preventDefault();
    if (!ready || busy.value) return;
    busy.value = true;
    if (await send(values.value, title.value.trim()) && adding) {
      title.value = "";
      values.value = Object.fromEntries(settings.map((x) => [x.key, ""]));
    }
    busy.value = false;
  }
  return (
    <form class="inline wrap" onSubmit={submit}>
      {adding && <input class="field" placeholder="Title: what it flags" aria-label="Title" value={title.value}
        onInput={(e) => { title.value = e.currentTarget.value; }} />}
      {settings.map((x) => (
        <input key={x.key} class="field" placeholder={adding ? x.label : x.default} aria-label={x.label} value={values.value[x.key]}
          onInput={(e) => { values.value = { ...values.value, [x.key]: e.currentTarget.value }; }} />
      ))}
      <button class="btn sm primary" disabled={busy.value || !ready}>{adding ? "Add" : "Save"}</button>
    </form>
  );
}

function SourceRow({ s }: { s: HeartbeatSource }) {
  const editing = useSignal(false);
  return (
    <li class="row">
      <div class="grow">
        <div><b>{s.title}</b> {!s.ready && <Pill kind="waiting">not set up</Pill>}</div>
        <div class="sub">{s.about}{s.added ? ` · added ${s.added}` : ""}</div>
        {!editing.value && s.settings.map((x) => <div key={x.key} class="sub">{x.label}: <code>{x.value}</code></div>)}
        {editing.value && <SourceForm settings={s.settings} send={async (settings) => {
          const done = await save("heartbeat_source_save", { id: s.id, settings }, "Saved. It applies from the next check.");
          if (done) editing.value = false;
          return done;
        }} />}
      </div>
      <span class="rt">
        {s.settings.length > 0 && <button class="ico" title="Change its settings" aria-label="Edit"
          onClick={() => { editing.value = !editing.value; }}><EditIcon /></button>}
        {s.added && <button class="ico" title="Remove" aria-label="Remove" onClick={() => {
          if (confirm(`Stop checking ${s.title} for good?`)) save("heartbeat_source_remove", { id: s.id }, "Removed");
        }}><TrashIcon /></button>}
        <button class="switch" role="switch" aria-checked={s.on} aria-label={`${s.title} on`}
          onClick={() => save("heartbeat_source_save", { id: s.id, on: !s.on }, s.on ? "Switched off" : "Switched on")} />
      </span>
    </li>
  );
}

function AddKind({ k }: { k: SourceKind }) {
  return (
    <>
      <div class="grp">Another {k.title.toLowerCase()} source</div>
      <SourceForm settings={k.settings} adding
        send={(settings, title) => save("heartbeat_source_add", { kind: k.name, title, settings }, `Added ${title}`)} />
      <p class="hint">{k.about}. Its first check only notes what's there already; after that, each new one is flagged.</p>
    </>
  );
}

export function HeartbeatPage() {
  const all = heartbeat.value;
  if (!all) return <Empty>Loading…</Empty>;
  return (
    <>
      <ul class="list">{all.sources.map((s) => <SourceRow key={s.id} s={s} />)}</ul>
      {all.kinds.map((k) => <AddKind key={k.name} k={k} />)}
      <p class="hint">Every few minutes, outside quiet hours and do not disturb, JARVIS checks the sources that are on and
        flags anything new: a notification, a push to your phone, and a word at the next pause. Changes apply from the next
        check. You can also ask JARVIS to flag another Gmail search, or to stop flagging something; a skill can add a source
        too.</p>
    </>
  );
}
