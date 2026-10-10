import { effect } from "@preact/signals";
import { useEffect } from "preact/hooks";
import { shown, sheet, sideOff, wide } from "../state/panel";
import { agentState, captions, link, live, loginShown, muted, paused, reading, sideBySide, speakerOff } from "../state/store";
import { lines } from "../state/transcript";
import { Controls } from "./Controls";
import { Header } from "./Header";
import { ApprovalCard, Activity, Palette, Toast } from "./panel/Overlays";
import { Dock, Panel } from "./panel/Panel";
import { Stage } from "./Stage";
import { Transcript } from "./Transcript";

function setData(key: string, value: string | null) {
  if (value == null) delete document.body.dataset[key];
  else document.body.dataset[key] = value;
}

// Missing from Preact's <audio> types, but iOS needs it.
const inline = { playsinline: "" } as {};

export function App() {
  // Mirror state to body[data-*] for CSS. Plain effect, not useSignalEffect (which defers a frame).
  useEffect(() => effect(() => {
    setData("state", agentState.value);
    setData("link", link.value);
    setData("view", loginShown.value ? "login" : null);
    setData("live", live.value ? "" : null);
    setData("muted", muted.value ? "" : null);
    setData("paused", paused.value || null);
    setData("speaker", speakerOff.value ? "off" : null);
    const transcript = captions.value || speakerOff.value;  // speaker off: transcript is the only output
    setData("captions", transcript ? null : "off");
    // Split only once there's a conversation, and only while the panel column isn't taking the space.
    const column = shown.value && wide.value && !sideOff.value;
    const split = lines.value.length && transcript && !loginShown.value && !column && sideBySide.value;
    setData("layout", split ? "split" : null);
    // Reading back folds the core into a strip, unless already side by side
    setData("reading", reading.value && transcript && !split ? "" : null);
    // Panel: side column (desktop) or dock + sheet (phone)
    setData("panel", shown.value ? (wide.value ? (sideOff.value ? "off" : "side") : "dock") : null);
    setData("sheet", shown.value && !wide.value && sheet.value !== "closed" ? sheet.value : null);
  }), []);

  return (
    <>
      <div class="bg" aria-hidden="true" />
      <div class="vcard" aria-hidden="true" />
      <Header />
      <main>
        <Stage />
        <Transcript />
      </main>
      <Controls />
      {shown.value && <>
        <Panel />
        <div class="dim" aria-hidden="true" onClick={() => { sheet.value = "closed"; }} />
        <Dock />
        <ApprovalCard />
        <Activity />
        <Palette />
      </>}
      <Toast />
      <audio id="player" autoplay {...inline} />
      <audio id="keepalive" {...inline} hidden />
    </>
  );
}
