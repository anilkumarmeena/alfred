// Projects: folders code tasks work in. Edit aliases, description, and in-place vs branch mode. The path changes
// only on JARVIS's computer (make projects).
import { useSignal } from "@preact/signals";
import { act, projects, type Project } from "../../state/panel";
import { BranchIcon, ChevronIcon, PlusIcon, ProjectsIcon } from "../icons";
import { Empty, Led } from "./bits";

const set = (r: Project[] | null) => { if (r) projects.value = r; return !!r; };

function state(p: Project): [string, "ok" | "warn" | "bad"] {
  if (p.machine) return [`lives on ${p.machine}`, "ok"];
  if (!p.exists) return ["folder missing", "bad"];
  if (p.git === "none") return ["not a git repo", "bad"];
  if (p.git === "no commits") return ["no commits yet", "warn"];
  if (p.changed) return [`${p.changed} file${p.changed > 1 ? "s" : ""} changed`, "warn"];
  return ["clean", "ok"];
}

const short = (path: string) => path.replace(/^\/(Users|home)\/[^/]+/, "~");

function Mode({ inPlace, set: choose, fixed }: { inPlace: boolean; set: (v: boolean) => void; fixed?: boolean }) {
  return (
    <div class="radios" role="radiogroup" aria-label="Mode">
      <button type="button" class={inPlace ? "on" : ""} disabled={fixed} onClick={() => choose(true)}>In place</button>
      <button type="button" class={!inPlace ? "on" : ""} disabled={fixed} onClick={() => choose(false)}>Worktree</button>
    </div>
  );
}

function Card({ p }: { p: Project }) {
  const open = useSignal(false), aliases = useSignal(p.aliases.join(", ")), about = useSignal(p.about), inPlace = useSignal(p.in_place);
  const [said, led] = state(p);
  const dirty = aliases.value !== p.aliases.join(", ") || about.value !== p.about || inPlace.value !== p.in_place;
  async function save() {
    set(await act("project_save", { name: p.name, aliases: aliases.value, about: about.value, in_place: inPlace.value }, `Saved ${p.name}`));
  }
  async function remove() {
    if (confirm(`Take ${p.name} off the project list? Its folder stays as it is.`)) set(await act("project_remove", { name: p.name }, `Removed ${p.name}`));
  }
  // Closing with unsaved edits asks first, then puts the fields back
  function toggle() {
    if (open.value && dirty) {
      if (!confirm(`Discard your changes to ${p.name}?`)) return;
      aliases.value = p.aliases.join(", "); about.value = p.about; inPlace.value = p.in_place;
    }
    open.value = !open.value;
  }
  return (
    <div class="proj">
      <button class="phead-btn" aria-expanded={open.value} onClick={toggle}>
        <Led kind={led} />
        <div class="grow">
          <div class="pname">{p.name}</div>
          {p.about && <div class="sub">{p.about}</div>}
          <div class="pmeta" title={p.path}>{p.in_place ? <ProjectsIcon /> : <BranchIcon />}{p.in_place ? "In place" : "Worktree"} · {short(p.path)}{p.machine ? ` on ${p.machine}` : ""}</div>
          {led !== "ok" && <div class={`pstate ${led}`}>{said}</div>}
        </div>
        <ChevronIcon />
      </button>
      {open.value && <div class="pedit">
        {p.git === "none" && p.exists && <div class="note amber">Tasks that work on a branch need git here.
          <button class="btn sm" onClick={async () => set(await act("project_git", { name: p.name }, `Set up git for ${p.name}`))}>Set up git</button></div>}
        <label class="lbl">Folder</label>
        <div class="field mono ro" title={p.path}>{p.path}</div>
        <p class="hint">To move it, on JARVIS's computer: <code>uv run python -m jarvis.tasks.projects set {p.name} --path …</code></p>
        <label class="lbl">Said as</label>
        <input class="field" value={aliases.value} placeholder="other names, comma separated" onInput={(e) => { aliases.value = e.currentTarget.value; }} />
        <label class="lbl">About</label>
        <input class="field" value={about.value} onInput={(e) => { about.value = e.currentTarget.value; }} />
        <label class="lbl">Mode</label>
        <Mode inPlace={inPlace.value} fixed={p.jarvis || (!p.machine && p.git !== "repo")} set={(v) => { inPlace.value = v; }} />
        <p class="hint">{p.jarvis ? "JARVIS always uses a worktree for itself."
          : p.machine ? `Its folder is on ${p.machine}, and its tasks and jobs run there.`
          : p.git !== "repo" ? "Worktrees need git history, so tasks here work in place."
          : "In place: tasks edit the folder and leave changes uncommitted. Worktree: each gets its own branch to merge."}</p>
        <div class="acts">
          <button class="btn primary sm" disabled={!dirty} onClick={save}>Save</button>
          <button class="btn sm" onClick={remove}>Remove from list</button>
        </div>
      </div>}
    </div>
  );
}

function AddProject({ done }: { done: () => void }) {
  const name = useSignal(""), path = useSignal(""), about = useSignal(""), inPlace = useSignal(true);
  async function add(e: Event) {
    e.preventDefault();
    if (set(await act("project_add", { name: name.value, path: path.value, about: about.value, in_place: inPlace.value }, `Added ${name.value}`))) done();
  }
  return (
    <form class="proj add" onSubmit={add}>
      <label class="lbl">Name</label>
      <input class="field" value={name.value} required onInput={(e) => { name.value = e.currentTarget.value; }} />
      <label class="lbl">Folder</label>
      <input class="field mono" value={path.value} placeholder="Blank: a new folder in your projects folder (made if it's missing)" onInput={(e) => { path.value = e.currentTarget.value; }} />
      <label class="lbl">About</label>
      <input class="field" value={about.value} onInput={(e) => { about.value = e.currentTarget.value; }} />
      <label class="lbl">Mode</label>
      <Mode inPlace={inPlace.value} set={(v) => { inPlace.value = v; }} />
      <div class="acts"><button class="btn primary sm" disabled={!name.value.trim()}>Add project</button>
        <button type="button" class="btn sm" onClick={done}>Cancel</button></div>
    </form>
  );
}

export function ProjectsSection() {
  const all = projects.value, adding = useSignal(false);
  if (!all) return <Empty>Loading…</Empty>;
  return (
    <div class="projects">
      {!all.length && <Empty>No projects yet. Add one, or tell JARVIS about a folder.</Empty>}
      {all.length > 0 && <div class="plist">{all.map((p) => <Card key={p.name} p={p} />)}</div>}
      {adding.value ? <AddProject done={() => { adding.value = false; }} />
        : <button class="btn wide addproj" onClick={() => { adding.value = true; }}><PlusIcon />Add project</button>}
    </div>
  );
}
