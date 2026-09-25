// Typing instead of talking: JARVIS answers a typed message the same way, spoken and captioned. The camera button
// picks a photo to show it (state/photo.ts), which goes with the next thing typed or said.
import { useSignal } from "@preact/signals";
import { sendText } from "../state/session";
import { ASK, attach, discard, photo, photoReady } from "../state/photo";
import { CameraIcon, CloseIcon, SendIcon } from "./icons";

export function Composer() {
  const text = useSignal("");
  const busy = useSignal(false);  // waiting for the photo to reach the Mac before sending
  const p = photo.value;
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
      if (!(await photoReady())) return;  // it didn't get there: the thumbnail says why
      if (sendText(t || ASK, `📷 ${t || ASK}`)) text.value = "";
    } finally { busy.value = false; }
  }

  function picked(e: Event) {
    const input = e.currentTarget as HTMLInputElement;
    const file = input.files?.[0];
    input.value = "";  // so picking the same photo again still counts
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
        <label class="attach" title="Show JARVIS a photo">
          <input type="file" accept="image/*" aria-label="Show JARVIS a photo" onChange={picked} />
          <CameraIcon />
        </label>
      )}
      <input id="message" type="text" enterKeyHint="send" autoComplete="off"
        placeholder={p ? "Ask about the photo, or just say it" : "Type to JARVIS"}
        aria-label="Message JARVIS" value={text.value} onInput={(e) => { text.value = e.currentTarget.value; }} />
      <button type="submit" class="send" aria-label="Send" title="Send (Return)" disabled={empty || busy.value}><SendIcon /></button>
    </form>
  );
}
