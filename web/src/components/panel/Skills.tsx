// Skills: view, enable/disable, delete. A skill with scripts can be enabled only after approving the review of its
// current files. Installing and authoring is by voice.
import { useEffect } from "preact/hooks";
import { useSignal } from "@preact/signals";
import { act, skills, type Skill, type SkillDetail } from "../../state/panel";
import { ChevronIcon, TrashIcon } from "../icons";
import { cap, Empty, Pill } from "./bits";
import { Markdown } from "./Markdown";

const SOURCE_PILL: Record<string, string> = { installed: "running", written: "done", learned: "waiting", manual: "muted" };
const STATE_PILL: Record<string, string> = { on: "done", off: "muted", broken: "failed" };
const VERDICT_PILL: Record<string, string> = { low: "done", medium: "waiting", high: "failed" };

// SKILL.md: frontmatter raw, body rendered
function split(md: string): [string, string] {
  const m = /^---\s*\n([\s\S]*?)\n---\s*(?:\n|$)([\s\S]*)$/.exec(md);
  return m ? [m[1] ?? "", m[2] ?? ""] : ["", md];
}

async function reload() {
  const r = await act<Skill[]>("skills");
  if (r) skills.value = r;
}

function Flags({ label, items }: { label: string; items: string[] }) {
  if (!items.length) return null;
  return <><label class="lbl">{label}</label><ul class="skill-flags">{items.map((x) => <li key={x}>{x}</li>)}</ul></>;
}

function Review({ d, set }: { d: SkillDetail; set: (r: SkillDetail) => void }) {
  const busy = useSignal(false);
  const r = d.review;
  async function review() {
    busy.value = true;
    const got = await act<SkillDetail>("skill_review", { name: d.name });
    busy.value = false;
    if (got) set(got);
  }
  async function approve() {
    if (!confirm(`Approve ${d.name}'s scripts as reviewed, and switch it on?`)) return;
    const got = await act<SkillDetail>("skill_switch", { name: d.name, on: true, approve: true }, "Approved and switched on");
    if (got) { set(got); reload(); }
  }
  return (
    <>
      <div class="grp">Scripts review</div>
      {r ? <>
        <p><Pill kind={VERDICT_PILL[r.verdict] ?? "failed"}>{r.verdict} risk</Pill>{!r.current && <> <Pill kind="waiting">files changed since</Pill></>}</p>
        <Markdown text={r.summary} />
        <Flags label="Risks" items={r.risks} />
        <Flags label="A scan of the files found it" items={r.flags} />
        {r.partial && <p class="hint warn">Some files were too long to read in full.</p>}
      </> : <p class="hint">Its scripts haven't been reviewed yet.</p>}
      <div class="acts">
        {(!r || !r.current) && <button class="btn sm" disabled={busy.value} onClick={review}>{busy.value ? "Reviewing… up to a minute" : "Review the scripts"}</button>}
        {r?.current && d.needs_approval && <button class="btn primary sm" onClick={approve}>Approve and switch on</button>}
      </div>
    </>
  );
}

function Detail({ name }: { name: string }) {
  const d = useSignal<SkillDetail | null>(null);
  useEffect(() => { act<SkillDetail>("skill", { name }).then((r) => { if (r) d.value = r; }); }, [name]);
  const s = d.value;
  if (!s) return <Empty>Loading…</Empty>;
  const [front, body] = split(s.skill_md);
  return (
    <div class="skill-detail">
      {s.from && <div class="sub">From {s.from}</div>}
      {s.problems?.map((p) => <p key={p} class="hint warn">{cap(p)}.</p>)}
      {s.scripts?.length ? <Review d={s} set={(r) => { d.value = r; }} /> : null}
      <div class="grp">Files</div>
      <ul class="skill-files">
        {s.files.map((f) => <li key={f.path}><code>{f.path}</code> <span class="sub">{f.size.toLocaleString()} bytes</span>{f.script && <> <Pill kind="waiting">script</Pill></>}</li>)}
      </ul>
      <div class="grp">SKILL.md</div>
      {front && <div class="md"><pre>{front}</pre></div>}
      <Markdown text={body} />
      {s.cut && <p class="hint">Only the start is shown.</p>}
      <div class="acts end">
        <button class="btn danger sm" onClick={async () => {
          if (!confirm(`Delete the skill ${s.name}? Its folder and files go for good.`)) return;
          const r = await act<Skill[]>("skill_delete", { name: s.name }, "Deleted");
          if (r) skills.value = r;
        }}><TrashIcon /> Delete</button>
      </div>
    </div>
  );
}

function Row({ s }: { s: Skill }) {
  const open = useSignal(false);
  async function toggle() {
    if (!s.enabled && s.needs_approval) { open.value = true; return; }  // review first
    const r = await act<SkillDetail>("skill_switch", { name: s.name, on: !s.enabled }, s.enabled ? "Switched off" : "Switched on");
    if (r) reload();
  }
  return (
    <>
      <li class="row">
        <button class="grow skill-name" aria-expanded={open.value} onClick={() => { open.value = !open.value; }}>
          <div><b>{s.name}</b> <Pill kind={STATE_PILL[s.state] ?? "waiting"}>{s.state}</Pill></div>
          <div class="sub">{s.description}</div>
          <div class="sub"><Pill kind={SOURCE_PILL[s.kind] ?? "muted"}>{s.source}</Pill> added {s.added}{s.scripts?.length ? ` · ${s.scripts.length} script${s.scripts.length > 1 ? "s" : ""}` : ""}</div>
        </button>
        <button class="switch" role="switch" aria-checked={s.enabled && s.state === "on"} aria-label={`${s.name} on`}
          disabled={s.state === "broken"} title={!s.enabled && s.needs_approval ? "Its scripts need approving first" : ""} onClick={toggle} />
        <ChevronIcon />
      </li>
      {open.value && <li class="row flat"><Detail name={s.name} /></li>}
    </>
  );
}

export function SkillsPage() {
  const all = skills.value;
  if (!all) return <Empty>Loading…</Empty>;
  return (
    <>
      <ul class="list">{all.map((s) => <Row key={s.name} s={s} />)}</ul>
      {!all.length && <Empty>No skills yet.</Empty>}
      <p class="hint">Skills are added by voice: "let's make that a skill", or "install the skill at github.com/…". One with
        scripts stays off until its review is read and approved. Changes apply from the next thing you say, and to tasks
        from their next start.</p>
    </>
  );
}
