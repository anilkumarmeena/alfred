// The call bar: photo, message box, then the call's buttons (`calls`) until there's something to send, when Send takes
// their place, giving the text the bar's width. Typed messages are answered like speech; the photo goes with the next turn.
import type { ComponentChildren } from "preact";
import { useSignal } from "@preact/signals";
import { sendText } from "../state/session";
import { assistantName } from "../state/store";
import { ASK, attach, discard, photo, photoReady } from "../state/photo";
import { CameraIcon, CloseIcon, SendIcon } from "./icons";

export function Composer({ calls }: { calls?: ComponentChildren }) {
  const text = useSignal("");
  const busy = useSignal(false);  // waiting for the photo upload before sending
  const p = photo.value, name = assistantName.value;
  const empty = !text.value.trim() && !p;

  async function submit(e: Event) {
    e.preventDefault();
    const t = text.value.trim();
    if (!p) {
      if (t && sendText(t)) text.value = "";
      return;
    }
    if (busy.value) return;
    busy.value = true;
    try {
      if (!(await photoReady())) return;  // upload failed; the thumbnail shows why
      if (sendText(t || ASK, `📷 ${t || ASK}`)) text.value = "";
    } finally { busy.value = false; }
  }

  function picked(e: Event) {
    const input = e.currentTarget as HTMLInputElement;
    const file = input.files?.[0];
    input.value = "";  // so re-picking the same file fires change
    if (file) attach(file);
  }

  const hint = p?.state === "failed" ? `The photo didn't send: ${p.error}` : p ? "Remove the photo" : "";
  return (
    <form id="composer" class="composer" onSubmit={submit}>
      {p ? (
        <button type="button" class="photo" data-state={p.state} aria-label={hint} title={hint} onClick={discard}>
          <img src={p.preview} alt="" />
          <span class="x"><CloseIcon /></span>
        </button>
      ) : (
        <label class="attach" title={`Show ${name} a photo`}>
          <input type="file" accept="image/*" aria-label={`Show ${name} a photo`} onChange={picked} />
          <CameraIcon />
        </label>
      )}
      <input id="message" type="text" enterKeyHint="send" autoComplete="off"
        placeholder={p ? "Ask about the photo, or just say it" : `Type to ${name}`}
        aria-label={`Message ${name}`} value={text.value} onInput={(e) => { text.value = e.currentTarget.value; }} />
      {empty ? calls
        : <button type="submit" class="send" aria-label="Send" title="Send (Return)" disabled={busy.value}><SendIcon /></button>}
    </form>
  );
}
