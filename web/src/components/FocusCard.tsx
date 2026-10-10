// Card for a tapped notification's task, alert or file, with a "talk about it" button. Audio files play inline.
import { activate } from "../state/session";
import { focus } from "../state/store";
import { CloseIcon } from "./icons";

export function FocusCard() {
  const f = focus.value;
  if (!f) return null;
  return (
    <section id="focus" aria-live="polite">
      <div class="top"><span id="focusLabel">{f.label}</span>
        <button id="focusClose" class="mini" aria-label="Dismiss" onClick={() => { focus.value = null; }}><CloseIcon /></button>
      </div>
      <p id="focusBody">{f.body}</p>
      {f.file?.audio && <audio id="focusAudio" controls preload="metadata" src={f.file.src} />}
      {f.file && <a id="focusFile" href={f.file.src} target="_blank" rel="noopener">{f.file.audio ? "Open to save" : "Open"} {f.file.name}</a>}
      <button id="focusTalk" class="pill" onClick={activate}>{f.talk}</button>
    </section>
  );
}
