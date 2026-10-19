// Approval buttons: deny, once, for this task, or always. "Always" first previews the rule (this exact command or
// its kind) and saves only from the confirm button. Some commands can never be remembered.
import { useSignal } from "@preact/signals";
import { answer, type Approval, type Breadth } from "../../state/panel";
import { assistantName } from "../../state/store";
import { CheckIcon } from "../icons";
import { cap } from "./bits";

export function Answer({ a, small = false, keys = false }: { a: Approval; small?: boolean; keys?: boolean }) {
  const r = a.remember, sm = small ? " sm" : "", row = small ? "acts" : "two";
  const always = useSignal(false), breadth = useSignal<Breadth>(r?.exact ? "exact" : "kind");
  const can = !!r && !r.never;
  if (always.value && r) {
    const choices: [Breadth, string, string][] = [["exact", "This command", r.exact], ["kind", "Any like it", r.kind]];
    return (
      <div class="remember">
        <div class="lab">Always allow, for background tasks and {assistantName}'s own shell, in any folder:</div>
        <div class="radios col" role="radiogroup" aria-label="What to allow">
          {choices.filter(([, , covers]) => covers).map(([b, label, covers]) => (
            <button key={b} type="button" role="radio" aria-checked={breadth.value === b} class={breadth.value === b ? "on" : ""}
              onClick={() => { breadth.value = b; }}><b>{label}</b> · {covers}</button>
          ))}
        </div>
        <div class="hint">It runs without asking until you withdraw it in Settings › Approvals.</div>
        <div class="acts">
          <button class={`btn${sm}`} onClick={() => { always.value = false; }}>Back</button>
          <button class={`btn ok${sm}`} onClick={() => answer(a.id, true, "always", breadth.value)}><CheckIcon />Approve and remember</button>
        </div>
      </div>
    );
  }
  return (
    <>
      <div class={row}>
        <button class={`btn danger${sm}`} onClick={() => answer(a.id, false)}>Deny{keys && <> <kbd>N</kbd></>}</button>
        <button class={`btn ok${sm}`} onClick={() => answer(a.id, true)}><CheckIcon />Approve once{keys && <> <kbd>Y</kbd></>}</button>
      </div>
      {can && (
        <div class={row}>
          <button class={`btn${sm}`} title="Commands like this from this task run without asking, for up to 12 hours"
            onClick={() => answer(a.id, true, "session")}>For this task</button>
          <button class={`btn${sm}`} title="Choose what to always allow" onClick={() => { always.value = true; }}>Always…</button>
        </div>
      )}
      {r?.never && <div class="why">{cap(r.never)}.</div>}
    </>
  );
}
