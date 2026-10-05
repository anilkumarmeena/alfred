// To-dos: the same list JARVIS keeps by voice. Add, tick off, edit and delete; the done ones fold away.
import { useSignal } from "@preact/signals";
import { act, todos, type Todo } from "../../state/panel";
import { CheckIcon, ChevronIcon, EditIcon, PlusIcon, TrashIcon } from "../icons";
import { Empty, InlineForm } from "./bits";
import { Markdown } from "./Markdown";

const set = (r: Todo[] | null) => { if (r) todos.value = r; return !!r; };

function Row({ t }: { t: Todo }) {
  const editing = useSignal(false), text = useSignal(t.text);
  async function save(e: Event) {
    e.preventDefault();
    if (text.value.trim() && text.value.trim() !== t.text) set(await act("todo_edit", { id: t.id, text: text.value }));
    editing.value = false;
  }
  return (
    <li class={`row${t.done ? " done" : ""}`}>
      <button class={`check${t.done ? " on" : ""}`} aria-label={t.done ? "Put back" : "Tick off"}
        onClick={async () => set(await act("todo_check", { id: t.id, done: !t.done }))}><CheckIcon /></button>
      {editing.value
        ? <form class="grow inline" onSubmit={save}>
            <input class="field" value={text.value} aria-label="To-do" ref={(el) => el?.focus()}
              onInput={(e) => { text.value = e.currentTarget.value; }} onBlur={save}
              onKeyDown={(e) => { if (e.key === "Escape") { e.stopPropagation(); text.value = t.text; editing.value = false; } }} />
          </form>
        : <div class="grow"><div><Markdown text={t.text} inline /></div></div>}
      {!editing.value && <span class="rt">
        {!t.done && <button class="ico" title="Edit" aria-label="Edit" onClick={() => { text.value = t.text; editing.value = true; }}><EditIcon /></button>}
        <button class="ico" title="Delete" aria-label="Delete" onClick={async () => set(await act("todo_delete", { id: t.id }))}><TrashIcon /></button>
      </span>}
    </li>
  );
}

export function TodosSection() {
  const all = todos.value, folded = useSignal(true);
  if (!all) return <Empty>Loading…</Empty>;
  const open = all.filter((t) => !t.done), done = all.filter((t) => t.done);
  return (
    <div class="todos">
      <div class="todo-add">
        <InlineForm placeholder="Add a to-do" label="Add" primary send={async (text) => set(await act("todo_add", { text }))}><PlusIcon /></InlineForm>
      </div>
      <ul class="list todo-open">
        {open.map((t) => <Row key={t.id} t={t} />)}
        {!open.length && <Empty>Nothing open.</Empty>}
      </ul>
      {done.length > 0 && <>
        <button class="grp fold" aria-expanded={!folded.value} onClick={() => { folded.value = !folded.value; }}>
          Done · {done.length} <ChevronIcon />
        </button>
        <ul class="list todo-done" data-folded={folded.value ? "" : undefined}>{done.map((t) => <Row key={t.id} t={t} />)}</ul>
      </>}
    </div>
  );
}
