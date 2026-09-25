// Boot: render the page, hand the session its <audio> elements, wire the keys and page lifecycle, then
// ask for the passcode or wait for a tap.
import { render } from "preact";
import "./styles.css";
import "./panel.css";
import { App } from "./components/App";
import { panelKey } from "./components/panel/Overlays";
import { openedFromNotification, setupPush } from "./state/push";
import { activate, hangup, initSession, online, pageHidden, pageShown, pressMute, toggleSpeaker, visibilityChanged } from "./state/session";
import { passcode, showLogin, toggleCaptions } from "./state/store";

const root = document.getElementById("app");
if (!root) throw new Error("Missing #app root element.");
render(<App />, root);

initSession({
  player: document.getElementById("player") as HTMLAudioElement,
  keepalive: document.getElementById("keepalive") as HTMLAudioElement,
});

document.addEventListener("keydown", (e) => {
  const target = e.target as Element;
  if (panelKey(e)) return;
  if (e.metaKey || e.ctrlKey || e.altKey || e.repeat || target.closest("input, textarea, .panel, .pal")) return;
  if (e.code === "Space" && !target.closest("button")) { e.preventDefault(); activate(); }
  else if (e.code === "Escape") hangup();
  else if (e.code === "KeyM") pressMute();
  else if (e.code === "KeyS") toggleSpeaker();
  else if (e.code === "KeyC") toggleCaptions();
});

document.addEventListener("visibilitychange", visibilityChanged);
window.addEventListener("pagehide", pageHidden);
window.addEventListener("pageshow", pageShown);
window.addEventListener("online", online);

openedFromNotification();
setupPush();

if (!passcode.value) showLogin();
