// Footer: the call bar (Composer) with Speaker, Mute and Connect or End (CSS picks by body[data-live]).
// Before a session, Mute and Speaker set how the next one starts.
import { activate, hangup, pressMute, toggleSpeaker } from "../state/session";
import { canSilence, connected, joinMuted, muted, speakerOff } from "../state/store";
import { Composer } from "./Composer";
import { HangupIcon, MicIcon, PowerIcon, SpeakerIcon } from "./icons";

function CallButtons() {
  const micOff = connected.value ? muted.value : joinMuted.value;
  return (
    <>
      {canSilence.value && (
        <button type="button" id="speaker" class="cb" aria-pressed={speakerOff.value} aria-label={speakerOff.value ? "Speaker off" : "Speaker"}
          onClick={toggleSpeaker} title={speakerOff.value ? "Speaker off: replies in text only. Turn it on (S)" : "Turn the speaker off: replies in text only (S)"}>
          <SpeakerIcon />
        </button>
      )}
      <button type="button" id="mute" class="cb" aria-pressed={micOff} aria-label={micOff ? "Unmute" : "Mute"}
        title={connected.value ? (micOff ? "Unmute (M)" : "Mute (M)") : micOff ? "Start with the mic on (M)" : "Start with the mic off (M)"} onClick={pressMute}>
        <MicIcon />
      </button>
      <button type="button" id="start" class="cb go" aria-label="Connect" title="Connect (Space)" onClick={activate}><PowerIcon /></button>
      <button type="button" id="hangup" class="cb end" aria-label="End" title="End (Esc)" onClick={hangup}><HangupIcon /></button>
    </>
  );
}

export function Controls() {
  return (
    <footer>
      <Composer calls={<CallButtons />} />
    </footer>
  );
}
