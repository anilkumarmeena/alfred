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

// Not in Preact's <audio> types, but iOS reads it, as it did on the old page.
const inline = { playsinline: "" } as {};

export function App() {
  // The stylesheet styles by state on body[data-*]: the core's colour, which controls show, the layout.
  // A plain effect, not useSignalEffect, which waits a frame: the body follows each change at once.
  useEffect(() => effect(() => {
    setData("state", agentState.value);
    setData("link", link.value);
    setData("view", loginShown.value ? "login" : null);
    setData("live", live.value ? "" : null);
    setData("muted", muted.value ? "" : null);
    setData("paused", paused.value || null);
    setData("speaker", speakerOff.value ? "off" : null);
    const transcript = captions.value || speakerOff.value;  // with the speaker off it's the only way to hear JARVIS
    setData("captions", transcript ? null : "off");
    // Wide screens only split into reactor and transcript once there's a conversation to show, and only while the
    // panel's column isn't taking the room.
    const column = shown.value && wide.value && !sideOff.value;
    const split = lines.value.length && transcript && !loginShown.value && !column && sideBySide.value;
    setData("layout", split ? "split" : null);
    // Reading back up the transcript: the core shrinks into a strip, unless it's beside the transcript anyway
    setData("reading", reading.value && transcript && !split ? "" : null);
    // The panel: a column beside the voice screen (desktop), or a sheet over it from the dock (phone)
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
