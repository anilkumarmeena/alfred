// Top bar: the status pill (signal bars for the link and its quality, then Live and its timer, Linking, Reconnecting
// or Offline, which connects on a tap), clock, and toggles (⌘K, activity or notifications before login, settings,
// desktop panel column).
import { toggleNotifications } from "../state/push";
import { activate } from "../state/session";
import {
  activityOpen, activityUnread, closeSheet, go, needYou, paletteOpen, section, setSideOff, shown, sideOff, toggleActivity, unseen, visible, wide,
} from "../state/panel";
import { clock, connected, link, linkText, passcode, pushOn, quality, setPasscode, showLogin } from "../state/store";
import { BellIcon, LockIcon, PanelIcon, SearchIcon, SettingsIcon } from "./icons";

// Settings toggles like a dock button: open it, or close the sheet (phone) or go back to Tasks (desktop column).
function toggleSettings() {
  if (!(visible.peek() && section.peek() === "settings")) go("settings");
  else if (wide.peek()) go("tasks");
  else closeSheet();
}

function StatusPill() {
  const inner = <>
    <span class="signal" title={quality.value ? `Connection: ${quality.value}` : undefined} data-q={quality.value ?? undefined}><i /><i /><i /></span>
    <span id="linkText">{linkText}</span>
  </>;
  return link.value === "off"
    ? <button class="link" title="Connect (Space)" onClick={activate}>{inner}</button>
    : <span class="link">{inner}</span>;
}

export function Header() {
  const settingsOn = visible.value && section.value === "settings";
  return (
    <header>
      <div class="brand">
        <span class="word">J.A.R.V.I.S.</span>
        <StatusPill />
      </div>
      <div class="tools">
        {shown.value && <button class="palbtn" title="Jump to anything · press ? for all shortcuts" onClick={() => { paletteOpen.value = true; }}>
          <SearchIcon />Search <kbd>⌘K</kbd>
        </button>}
        <span id="clock">{clock}</span>
        {shown.value
          ? <button id="activityBtn" class="mini" aria-pressed={activityOpen.value} aria-label="Activity" title="Activity"
              onClick={() => toggleActivity()}>
              <BellIcon />{activityUnread.value && <i class="bdg dot" />}
            </button>
          : <button id="notifyBtn" class="mini" aria-pressed={pushOn.value} aria-label="Notifications"
              title={pushOn.value ? "Notifications on (tap to turn off)" : "Enable notifications"} onClick={toggleNotifications}>
              <BellIcon />
            </button>}
        {shown.value && <button id="settingsBtn" class="mini" aria-pressed={settingsOn} aria-label="Settings" title="Settings"
          onClick={toggleSettings}>
          <SettingsIcon />
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
