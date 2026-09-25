// The top bar: the link LED and session timer, connection quality, the clock, and the toggles: search (⌘K), the
// transcript, the activity list (or, before the passcode is in, notifications) and the desktop's panel column.
import { toggleNotifications } from "../state/push";
import { activityOpen, activityUnread, needYou, paletteOpen, setSideOff, shown, sideOff, toggleActivity, unseen, wide } from "../state/panel";
import { captions, clock, connected, linkText, passcode, pushOn, quality, setPasscode, showLogin, toggleCaptions } from "../state/store";
import { BellIcon, CaptionsIcon, LockIcon, PanelIcon, SearchIcon } from "./icons";

export function Header() {
  return (
    <header>
      <div class="brand">
        <span class="word">J.A.R.V.I.S.</span>
        <span class="link"><span class="led" /><span id="linkText">{linkText}</span></span>
      </div>
      <div class="tools">
        <span id="signal" class="signal" title="Connection quality" data-q={quality.value ?? undefined} hidden={quality.value == null}>
          <i /><i /><i />
        </span>
        {shown.value && <button class="palbtn" title="Jump to anything · press ? for all shortcuts" onClick={() => { paletteOpen.value = true; }}>
          <SearchIcon />Search <kbd>⌘K</kbd>
        </button>}
        <span id="clock">{clock}</span>
        <button id="captionsBtn" class="mini" aria-pressed={captions.value} aria-label="Transcript" title="Transcript (C)" onClick={toggleCaptions}>
          <CaptionsIcon />
        </button>
        {shown.value
          ? <button id="activityBtn" class="mini" aria-pressed={activityOpen.value} aria-label="Activity" title="Activity"
              onClick={() => toggleActivity()}>
              <BellIcon />{activityUnread.value && <i class="bdg dot" />}
            </button>
          : <button id="notifyBtn" class="mini" aria-pressed={pushOn.value} aria-label="Notifications"
              title={pushOn.value ? "Notifications on (tap to turn off)" : "Enable notifications"} onClick={toggleNotifications}>
              <BellIcon />
            </button>}
        {shown.value && wide.value && <button id="sideBtn" class="mini" aria-pressed={!sideOff.value} aria-label="Panel" title="Show or hide the panel (\)"
          onClick={() => setSideOff(!sideOff.peek())}>
          <PanelIcon />
          {sideOff.value && (needYou.value ? <i class="bdg n">{needYou.value}</i> : unseen.value.length ? <i class="bdg dot" /> : null)}
        </button>}
        <button id="lock" class="mini" aria-label="Forget passcode" title="Forget passcode"
          hidden={connected.value || !passcode.value || shown.value} onClick={() => { setPasscode(null); showLogin(); }}>
          <LockIcon />
        </button>
      </div>
    </header>
  );
}
