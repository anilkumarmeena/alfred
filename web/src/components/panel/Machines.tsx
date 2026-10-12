// Machines: other computers JARVIS sends heavy work to. Pairing starts here with a one-time code, which the machine
// runs (make node-pair); it connects once its fingerprint is confirmed here. Never by voice.
import { useSignal } from "@preact/signals";
import { act, ago, machines, type Machine, type Machines, type PairingCode } from "../../state/panel";
import { now } from "../../state/store";
import { EditIcon, TrashIcon } from "../icons";
import { cap, Empty, Led, Pill } from "./bits";

export function machinesSummary(): [string, "ok" | "warn" | null] {
  const m = machines.value;
  if (!m) return ["Other computers JARVIS sends heavy work to", null];
  const pending = m.machines.filter((x) => x.status === "pending"), paired = m.machines.filter((x) => x.status === "paired");
  if (pending.length) return [`${pending.map((x) => x.name).join(", ")} waiting for you to confirm`, "warn"];
  if (!paired.length) return ["None yet: add one to send it renders, builds and tasks", null];
  const on = paired.filter((x) => x.online);
  return [on.length ? `${on.map((x) => x.name).join(", ")} online` : `${paired.length} paired, none online`, on.length ? "ok" : null];
}

function standing(x: Machine): string {
  if (!x.online) return x.last_seen ? `Offline · last seen ${ago(x.last_seen, now.value)}` : "Not connected yet";
  const bits = [`Online · ${x.free ?? 0} of ${x.max_tasks ?? 0} slots free`];
  if (x.updating) bits.push("updating once idle");
  else if (!x.up_to_date) bits.push("on other code");
  if (x.takes_work === false) bits.push("takes no new work until it updates");
  return bits.join(" · ");
}

function MachineRow({ x }: { x: Machine }) {
  const editing = useSignal(false), name = useSignal(x.name), aliases = useSignal(x.aliases.join(", "));
  const set = (r: Machines | null) => { if (r) machines.value = r; return !!r; };
  async function remove() {
    if (!confirm(`Forget ${x.name}? Nothing more is sent there, and it must be paired again to come back.`)) return;
    set(await act<Machines>("machine_remove", { id: x.id }, `Removed ${x.name}`));
  }
  async function save(e: Event) {
    e.preventDefault();
    if (set(await act<Machines>("machine_save", { id: x.id, name: name.value, aliases: aliases.value }, "Saved"))) editing.value = false;
  }
  return (
    <li class="row">
      <Led kind={x.status === "pending" ? "warn" : x.online ? "ok" : "off"} />
      <div class="grow">
        <div><b>{x.name}</b> {x.status === "pending" && <Pill kind="waiting">confirm</Pill>}</div>
        {x.status === "pending" ? <>
          <div class="sub">Fingerprint <code>{x.fingerprint}</code>: confirm only if the machine shows the same.</div>
          <div class="acts">
            <button class="btn sm primary" onClick={async () => set(await act<Machines>("machine_confirm", { id: x.id }, `Paired ${x.name}`))}>Confirm</button>
            <button class="btn sm" onClick={remove}>Turn away</button>
          </div>
        </> : <>
          <div class="sub">{standing(x)}</div>
          <div class="sub">{[x.os, x.cores && `${x.cores} cores`, x.ram_gb && `${x.ram_gb} GB`].filter(Boolean).join(" · ")}
            {x.tools.length ? ` · ${x.tools.join(", ")}` : ""}</div>
          {x.usage && <div class="sub">Its own Claude account: {x.usage}</div>}
          {x.update_problem && <div class="sub warn">{cap(x.update_problem)}</div>}
        </>}
        {editing.value && (
          <form class="inline wrap" onSubmit={save}>
            <input class="field" aria-label="Name" value={name.value} onInput={(e) => { name.value = e.currentTarget.value; }} />
            <input class="field" placeholder="Other names, commas" aria-label="Other names" value={aliases.value}
              onInput={(e) => { aliases.value = e.currentTarget.value; }} />
            <button class="btn sm primary" disabled={!name.value.trim()}>Save</button>
          </form>
        )}
      </div>
      {x.status === "paired" && <span class="rt">
        <button class="ico" title="Rename" aria-label="Rename" onClick={() => { editing.value = !editing.value; }}><EditIcon /></button>
        <button class="ico" title="Forget" aria-label="Forget" onClick={remove}><TrashIcon /></button>
      </span>}
    </li>
  );
}

function AddMachine({ fingerprint }: { fingerprint: string }) {
  const code = useSignal<PairingCode | null>(null);
  const expired = code.value && code.value.expires * 1000 < now.value;
  if (!code.value || expired) {
    return <div class="acts"><button class="btn sm primary" onClick={async () => {
      code.value = await act<PairingCode>("machine_code");
    }}>{expired ? "New code" : "Add a machine"}</button></div>;
  }
  const minutes = Math.max(1, Math.round((code.value.expires * 1000 - now.value) / 60000));
  return (
    <div class="note">
      <div class="grow">
        <div>On the other computer, in its JARVIS folder, run:</div>
        <pre class="tlog">make node-pair CODE={code.value.code} MAIN={location.origin}</pre>
        <p class="hint">The code works once, for {minutes} more minute{minutes === 1 ? "" : "s"}. That computer then shows two
          fingerprints: this computer's should read <code>{code.value.main_fingerprint || fingerprint}</code>. The new
          machine appears above for you to confirm.</p>
      </div>
      <button class="btn sm" onClick={() => { code.value = null; }}>Done</button>
    </div>
  );
}

function Placement({ m, kind, label }: { m: Machines; kind: string; label: string }) {
  const options: [string, string][] = [["here", "This computer"], ["auto", "Let JARVIS choose"],
    ...m.machines.filter((x) => x.status === "paired").map((x): [string, string] => [String(x.id), x.name])];
  const chosen = m.placement[kind] ?? "here";
  return (
    <>
      <label class="lbl">{label}</label>
      <div class="radios" role="radiogroup" aria-label={label}>
        {options.map(([value, text]) => (
          <button key={value} type="button" role="radio" aria-checked={value === chosen} class={value === chosen ? "on" : ""}
            onClick={async () => {
              if (value === chosen) return;
              const r = await act<Machines>("placement_save", { kind, value }, `${label}: ${text}`);
              if (r) machines.value = r;
            }}>{text}</button>
        ))}
      </div>
    </>
  );
}

export function MachinesPage() {
  const m = machines.value;
  if (!m) return <Empty>Loading…</Empty>;
  const paired = m.machines.some((x) => x.status === "paired");
  return (
    <>
      <ul class="list">{m.machines.map((x) => <MachineRow key={x.id} x={x} />)}</ul>
      {!m.machines.length && <Empty>No machines yet. Pair one to send it renders, builds and Claude tasks.</Empty>}
      <AddMachine fingerprint={m.fingerprint} />
      <p class="hint">A machine runs JARVIS in node mode, signed in to its own Claude account. Work sent there runs behind
        the same walls as here: risky commands wait for your yes, asked from this computer, and keys and accounts never
        leave it. It updates itself to this computer's code when it's idle.</p>
      {paired && <>
        <div class="grp">Where jobs run</div>
        {Object.entries(m.kinds).map(([kind, label]) => <Placement key={kind} m={m} kind={kind} label={label} />)}
        <label class="lbl">Research</label>
        <div class="field locked">This computer</div>
        <p class="hint">JARVIS always says where a job runs. If the machine chosen doesn't answer, or its Claude account has
          hit its limit, the job runs here instead. Research, and tasks that read your mail or accounts, always run here;
          a project that lives on a machine always runs there. You can also say where when you ask ("run this one here").</p>
      </>}
    </>
  );
}
