// Top bar: the status pill (signal bars for the link and its quality, then Live and its timer, Linking, Reconnecting
// or Offline, which connects on a tap) and toggles (⌘K, history, activity or notifications before login,
// settings). On desktop the sidebar carries the wordmark, the pill and History instead.
import { toggleNotifications } from "../state/push";
import { activate } from "../state/session";
import {
  activityOpen, activityUnread, closeSheet, go, paletteOpen, section, setHome, shown, toggleActivity, visible, wide,
} from "../state/panel";
import { connected, link, linkText, passcode, pushOn, quality, setPasscode, showLogin } from "../state/store";
import { BellIcon, HistoryIcon, LockIcon, SearchIcon, SettingsIcon } from "./icons";

// History and Settings toggle like dock buttons: open, or close the sheet (phone) or go back to Home (desktop).
function toggle(sec: "history" | "settings") {
  if (!(visible.peek() && section.peek() === sec)) go(sec);
  else if (wide.peek()) setHome(true);
  else closeSheet();
}

export function StatusPill() {
  const inner = <>
    <span class="signal" title={quality.value ? `Connection: ${quality.value}` : undefined} data-q={quality.value ?? undefined}><i /><i /><i /></span>
    <span id="linkText">{linkText}</span>
  </>;
  return link.value === "off"
    ? <button class="link" title="Connect (Space)" onClick={activate}>{inner}</button>
    : <span class="link">{inner}</span>;
}

export function Header() {
  const on = (sec: "history" | "settings") => visible.value && section.value === sec;
  return (
    <header>
      <div class="brand">
        <span class="word">J.A.R.V.I.S.</span>
        <StatusPill />
      </div>
      <div class="tools">
        {shown.value && <button class="palbtn" title="Jump to anything · press ? for all shortcuts" onClick={() => { paletteOpen.value = true; }}>
          <SearchIcon /><span>Jump to a task, a section, or type a to-do…</span><kbd>⌘K</kbd>
        </button>}
        {shown.value && !wide.value && <button id="historyBtn" class="mini" aria-pressed={on("history")} aria-label="History" title="Past conversations"
          onClick={() => toggle("history")}>
          <HistoryIcon />
        </button>}
        {shown.value
          ? <button id="activityBtn" class="mini" aria-pressed={activityOpen.value} aria-label="Activity" title="Activity"
              onClick={() => toggleActivity()}>
              <BellIcon />{activityUnread.value && <i class="bdg dot" />}
            </button>
          : <button id="notifyBtn" class="mini" aria-pressed={pushOn.value} aria-label="Notifications"
              title={pushOn.value ? "Notifications on (tap to turn off)" : "Enable notifications"} onClick={toggleNotifications}>
              <BellIcon />
            </button>}
        {shown.value && <button id="settingsBtn" class="mini" aria-pressed={on("settings")} aria-label="Settings" title="Settings"
          onClick={() => toggle("settings")}>
          <SettingsIcon />
        </button>}
        <button id="lock" class="mini" aria-label="Forget passcode" title="Forget passcode"
          hidden={connected.value || !passcode.value || shown.value} onClick={() => { setPasscode(null); showLogin(); }}>
          <LockIcon />
        </button>
      </div>
    </header>
  );
}
