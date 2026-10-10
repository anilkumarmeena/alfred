// Footer: message box, Mute, Connect/End (CSS picks by body[data-live]) and the speaker switch.
// Before a session, Mute and speaker set how the next one starts.
import { activate, hangup, pressMute, toggleSpeaker } from "../state/session";
import { canSilence, connected, joinMuted, muted, speakerOff } from "../state/store";
import { Composer } from "./Composer";
import { HangupIcon, MicIcon, PowerIcon, SpeakerIcon } from "./icons";

export function Controls() {
  const micOff = connected.value ? muted.value : joinMuted.value;
  return (
    <footer>
      <Composer />
      <div class="controls">
        <button id="mute" class="ctl" aria-pressed={micOff} title={connected.value ? "Mute (M)" : "Start with the mic off (M)"} onClick={pressMute}>
          <span class="round"><MicIcon /></span>
          <span id="muteText" class="label">{micOff ? "Unmute" : "Mute"}</span>
        </button>
        <button id="start" class="pill" onClick={activate}>
          <PowerIcon />
          Connect
        </button>
        <button id="hangup" class="ctl" title="End (Esc)" onClick={hangup}>
          <span class="round"><HangupIcon /></span>
          <span class="label">End</span>
        </button>
        {canSilence.value && (
          <button id="speaker" class="ctl" aria-pressed={speakerOff.value} onClick={toggleSpeaker}
            title={speakerOff.value ? "Speaker off: replies in text only. Turn it on (S)" : "Turn the speaker off: replies in text only (S)"}>
            <span class="round"><SpeakerIcon /></span>
            <span class="label">{speakerOff.value ? "Text only" : "Speaker"}</span>
          </button>
        )}
      </div>
    </footer>
  );
}
