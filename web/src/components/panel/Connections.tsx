// Connections (MCP servers): toggle, sign in (via JARVIS's browser on its computer), set keys, remove.
// Keys are write-only (pasted, never spoken; shown only as set/unset). Adding servers is by voice, with approval.
import { useSignal } from "@preact/signals";
import { badAddress, hostOf } from "../../lib/address";
import { act, servers, toast, type Server } from "../../state/panel";
import { ChevronIcon, TrashIcon } from "../icons";
import { cap, Empty, Pill } from "./bits";

const ACCESS: Record<string, [string, string]> = {
  read: ["reads", "done"], own: ["your own inbox", "done"], write: ["asks first", "waiting"], blocked: ["off", "muted"],
};

function state(s: Server): [string, string] {
  if (!s.reviewed) return ["being set up", "waiting"];
  if (!s.enabled) return ["off", "muted"];
  return s.problem ? ["can't start", "waiting"] : ["on", "done"];
}

function from(s: Server): string {
  if (!s.source) return "Set up by hand";
  return s.verified ? `Official MCP registry · ${s.source}${s.version ? ` ${s.version}` : ""}` : `Unverified · found at ${s.source}`;
}

function KeyRow({ s, k }: { s: Server; k: { var: string; set: boolean } }) {
  const open = useSignal(false), value = useSignal(""), busy = useSignal(false);
  async function save(e: Event) {
    e.preventDefault();
    if (!value.value.trim() || busy.value) return;
    busy.value = true;
    const r = await act<Server[]>("server_secret", { name: s.name, var: k.var, value: value.value }, "Kept, encrypted");
    busy.value = false;
    if (r) { servers.value = r; value.value = ""; open.value = false; }
  }
  return (
    <div class="key">
      <span class="var">{k.var}</span>
      {k.set ? <span class="st"><span class="dots">••••••</span> set</span> : <span class="st warn">not set</span>}
      {open.value ? (
        <form class="inline grow" onSubmit={save}>
          <input class="field" type="password" autocomplete="off" placeholder={`Paste ${k.var}`} aria-label={k.var}
            value={value.value} onInput={(e) => { value.value = e.currentTarget.value; }} />
          <button class="btn sm primary" disabled={busy.value || !value.value.trim()}>Save</button>
          <button type="button" class="btn sm" onClick={() => { open.value = false; value.value = ""; }}>Cancel</button>
        </form>
      ) : <button class="btn sm" onClick={() => { open.value = true; }}>{k.set ? "Replace" : "Enter key"}</button>}
    </div>
  );
}

// The server it reaches (a self-hosted GitLab, GitHub Enterprise). Moving it to another host forgets its keys.
function AddressRow({ s, a }: { s: Server; a: { var: string; value: string } }) {
  const open = useSignal(false), value = useSignal(a.value), busy = useSignal(false);
  const url = value.value.trim(), why = badAddress(url);
  async function save(e: Event) {
    e.preventDefault();
    if (why || busy.value) return;
    const keys = s.secrets.filter((k) => k.set).map((k) => k.var);
    if (keys.length && hostOf(url) !== hostOf(a.value)
      && !confirm(`Its ${keys.join(", ")} ${keys.length > 1 ? "were" : "was"} for the old server, so ${keys.length > 1 ? "they're" : "it's"} `
        + "forgotten: paste the new server's key afterwards.\n\nChange the server?")) return;
    busy.value = true;
    const r = await act<{ forgot: string[]; servers: Server[] }>("server_address", { name: s.name, var: a.var, value: url });
    busy.value = false;
    if (!r) return;
    servers.value = r.servers;
    open.value = false;
    toast(r.forgot.length ? `Changed. Paste its ${r.forgot.join(", ")} again.` : "Changed");
  }
  return (
    <div class="key">
      <span class="var">{a.var}</span>
      <span class="st">{a.value || "default server"}</span>
      {open.value ? (
        <form class="inline grow" onSubmit={save}>
          <input class="field" type="url" autocomplete="off" placeholder="https://… (empty for the default)" aria-label={a.var}
            value={value.value} onInput={(e) => { value.value = e.currentTarget.value; }} />
          <button class="btn sm primary" disabled={busy.value || !!why || url === a.value}>Save</button>
          <button type="button" class="btn sm" onClick={() => { open.value = false; value.value = a.value; }}>Cancel</button>
        </form>
      ) : <button class="btn sm" onClick={() => { value.value = a.value; open.value = true; }}>Change server</button>}
      {open.value && why && <span class="sub warn">{why}</span>}
    </div>
  );
}

function Card({ s }: { s: Server }) {
  const tools = useSignal(false), signing = useSignal(false);
  const [said, kind] = state(s);
  async function toggle() {
    const r = await act<Server[]>("server_switch", { name: s.name, on: !s.enabled }, s.enabled ? "Switched off" : "Switched on");
    if (r) servers.value = r;
  }
  async function signIn() {
    signing.value = true;
    const r = await act<{ said: string; servers: Server[] }>("server_sign_in", { name: s.name });
    signing.value = false;
    if (r) servers.value = r.servers;
  }
  async function remove() {
    const r = await act<{ impact: string }>("server_impact", { name: s.name });
    if (!r || !confirm(`${r.impact}\n\nRemove it?`)) return;
    const left = await act<Server[]>("server_remove", { name: s.name }, "Removed");
    if (left) servers.value = left;
  }
  const counts = Object.entries(s.tools).map(([k, n]) => `${n} ${ACCESS[k]?.[0] ?? k}`).join(" · ");
  return (
    <div class="srv">
      <div class="srv-head">
        <h3>{s.label} <Pill kind={kind}>{said}</Pill>{!s.verified && s.source && <Pill kind="waiting">unverified</Pill>}</h3>
        <button class="switch" role="switch" aria-checked={s.enabled} aria-label={`${s.label} on`} disabled={!s.reviewed}
          title={s.reviewed ? "" : "Its tools need sorting first: ask JARVIS to review them"} onClick={toggle} />
      </div>
      <div class="sub">{from(s)}</div>
      <div class="cmdline" title={s.command}>{s.command}</div>
      {s.addresses.map((a) => <AddressRow key={a.var} s={s} a={a} />)}
      {s.secrets.map((k) => <KeyRow key={k.var} s={s} k={k} />)}
      {s.sign_in && (
        <div class="key">
          <span class="var">Sign-in</span>
          {s.signed_in ? <span class="st">signed in</span> : <span class="st warn">not signed in</span>}
          <button class="btn sm" disabled={signing.value} onClick={signIn}>
            {signing.value ? "Sign in in JARVIS's browser on its computer…" : s.signed_in ? "Sign in again" : "Sign in"}
          </button>
        </div>
      )}
      {s.problem && <p class="hint warn">{cap(s.problem)}.</p>}
      {s.tool_list.length > 0 && (
        <button class="srv-tools" aria-expanded={tools.value} onClick={() => { tools.value = !tools.value; }}>
          <ChevronIcon /> {s.tool_list.length} tools{counts ? `: ${counts}` : ""}{s.research ? " · research tasks get its reads" : ""}
        </button>
      )}
      {tools.value && (
        <ul class="srv-list">
          {s.tool_list.map((t) => <li key={t.name}><code>{t.name}</code> <Pill kind={ACCESS[t.access]?.[1] ?? "muted"}>{ACCESS[t.access]?.[0] ?? t.access}</Pill></li>)}
        </ul>
      )}
      <div class="acts end"><button class="btn danger sm" onClick={remove}><TrashIcon /> Remove</button></div>
    </div>
  );
}

export function ConnectionsPage() {
  const all = servers.value;
  if (!all) return <Empty>Loading…</Empty>;
  return (
    <>
      {all.map((s) => <Card key={s.name} s={s} />)}
      {!all.length && <Empty>No connections yet.</Empty>}
      <div class="note"><div>New tools are connected by voice: "connect Notion" finds it in the official MCP registry, and
        it's set up after your yes. Paste keys here, never out loud, and change the server one reaches (a company's own
        GitLab, say). Tools that change things always ask first. Changes apply from the next conversation or task.</div></div>
    </>
  );
}
