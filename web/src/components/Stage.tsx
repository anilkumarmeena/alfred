// Centre stage: the core, status readout, and prompts that need user action.
import { useLayoutEffect, useRef } from "preact/hooks";
import { mountFold } from "../lib/fold";
import { enablePush } from "../state/push";
import { unlockAudio } from "../state/session";
import { audioBlocked, error, notice, pushKey, pushLater, pushOn, readout } from "../state/store";
import { Core } from "./Core";
import { FocusCard } from "./FocusCard";
import { Login } from "./Login";
import { BellIcon, SoundIcon } from "./icons";

export function Stage() {
  const { label, hint } = readout.value;
  const stage = useRef<HTMLElement>(null);
  useLayoutEffect(() => mountFold(stage.current!), []);  // before first paint, so the transcript starts below it
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
      <FocusCard />
      <div id="error" role="alert">{error.value || null}</div>
      <div id="notice" role="status">{notice.value || null}</div>
      <Login />
    </section>
  );
}
