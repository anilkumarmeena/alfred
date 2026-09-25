// The middle of the screen: the core, what JARVIS is doing, and anything that needs a tap or a word.
import { useLayoutEffect, useRef } from "preact/hooks";
import { mountFold } from "../lib/fold";
import { enablePush } from "../state/push";
import { restoreAfterCall, unlockAudio } from "../state/session";
import { afterCall, audioBlocked, error, notice, pushKey, pushLater, pushOn, readout } from "../state/store";
import { Core } from "./Core";
import { FocusCard } from "./FocusCard";
import { Login } from "./Login";
import { BellIcon, SoundIcon } from "./icons";

// A phone call turned the mic and the speaker off (the iPhone app): now it's over, offer what was on before it.
function AfterCall() {
  const was = afterCall.value;
  if (!was) return null;
  const what = [!was.muted && "mic", !was.speakerOff && "speaker"].filter(Boolean).join(" and ");
  return (
    <div id="afterCall" role="alert">
      <p>Call over. Turn the {what} back on?</p>
      <button class="pill" onClick={() => restoreAfterCall(true)}>Turn back on</button>
      <button class="pill" onClick={() => restoreAfterCall(false)}>Keep off</button>
    </div>
  );
}

export function Stage() {
  const { label, hint } = readout.value;
  const stage = useRef<HTMLElement>(null);
  useLayoutEffect(() => mountFold(stage.current!), []);  // before the first paint, so the transcript starts below it
  return (
    <section class="stage" ref={stage}>
      <Core />
      <div class="readout" aria-live="polite">
        <div id="status">{label}</div>
        <div id="hint">{hint}</div>
      </div>
      <button id="unlockAudio" class="pill warn" hidden={!audioBlocked.value} onClick={unlockAudio}>
        <SoundIcon />
        Tap to enable sound
      </button>
      <button id="notify" class="pill" hidden={!pushKey.value || pushOn.value || pushLater.value} onClick={enablePush}>
        <BellIcon />
        Enable notifications
      </button>
      <AfterCall />
      <FocusCard />
      <div id="error" role="alert">{error.value || null}</div>
      <div id="notice" role="status">{notice.value || null}</div>
      <Login />
    </section>
  );
}
