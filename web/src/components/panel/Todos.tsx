// To-dos (shared with voice): add, tick, edit, delete; done items collapse. Open and done each sit in a card.
// Ticks and deletes can be undone from the toast; what Alfred adds elsewhere shows up live, highlighted for a while.
import { useSignal } from "@preact/signals";
import { assistantName, now } from "../../state/store";
import { ago, freshTodos, todoAct, todos, toast, unfresh, type Todo } from "../../state/panel";
import { CheckIcon, ChevronIcon, EditIcon, PlusIcon, TrashIcon } from "../icons";
import { Empty, InlineForm } from "./bits";
import { Markdown } from "./Markdown";

const TICK_MS = 350;  // a ticked to-do shows its tick before it moves

// Tick a to-do off (or put it back), with Undo on the toast. Also used by the desktop rail.
export async function checkTodo(t: Todo) {
  const [r] = await Promise.all([todoAct("todo_check", { id: t.id, done: !t.done }), new Promise((ok) => setTimeout(ok, TICK_MS))]);
  if (r) toast(t.done ? "Put back on the list" : "Ticked off", { label: "Undo", run: () => todoAct("todo_check", { id: t.id, done: t.done }) });
}

function Row({ t }: { t: Todo }) {
  const editing = useSignal(false), text = useSignal(t.text), ticking = useSignal(false);
  const done = ticking.value ? !t.done : t.done, fresh = freshTodos.value.has(t.id);
  async function tick() {
    if (ticking.value) return;
    ticking.value = true;
    await checkTodo(t);
    ticking.value = false;
  }
  async function remove() {
    if (await todoAct("todo_delete", { id: t.id })) toast("Deleted", { label: "Undo", run: () => todoAct("todo_restore", { ...t }) });
  }
  function edit() { text.value = t.text; editing.value = true; }
  async function save(e: Event) {
    e.preventDefault();
    if (text.value.trim() && text.value.trim() !== t.text) await todoAct("todo_edit", { id: t.id, text: text.value });
    editing.value = false;
  }
  return (
    <li class={`row${done ? " done" : ""}${fresh ? " fresh" : ""}`} onPointerDown={fresh ? () => unfresh(t.id) : undefined}>
      <button class={`check${done ? " on" : ""}`} aria-label={t.done ? "Put back" : "Tick off"} onClick={tick}><CheckIcon /></button>
      {editing.value
        ? <form class="grow inline" onSubmit={save}>
            <input class="field" value={text.value} aria-label="To-do" ref={(el) => el?.focus()}
              onInput={(e) => { text.value = e.currentTarget.value; }} onBlur={save}
              onKeyDown={(e) => { if (e.key === "Escape") { e.stopPropagation(); text.value = t.text; editing.value = false; } }} />
          </form>
        : <div class="grow">
            {/* Tapping an open to-do's text edits it, as the pencil does; links in it still open */}
            <div class={t.done ? undefined : "tap"} onClick={t.done ? undefined : (e) => { if (!(e.target as Element).closest("a")) edit(); }}>
              {fresh && <span class="new" title="Just added" />}<Markdown text={t.text} inline />
            </div>
            <div class="sub when">{t.done ? `done ${ago(t.done_at, now.value)}` : `added ${ago(t.created, now.value)}`}</div>
          </div>}
      {!editing.value && <span class="rt">
        {!t.done && <button class="ico" title="Edit" aria-label="Edit" onClick={edit}><EditIcon /></button>}
        <button class="ico" title="Delete" aria-label="Delete" onClick={remove}><TrashIcon /></button>
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
      <p class="tsum">{open.length} open{done.length ? ` · ${done.length} done` : ""}</p>
      <div class="todo-add">
        <InlineForm placeholder="Add a to-do" label="Add" primary send={async (text) => !!(await todoAct("todo_add", { text }))}><PlusIcon /></InlineForm>
      </div>
      <ul class="list todo-open">
        {open.map((t) => <Row key={t.id} t={t} />)}
        {!open.length && <li class="row none">Nothing open. Add one above, or ask {assistantName} to.</li>}
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
