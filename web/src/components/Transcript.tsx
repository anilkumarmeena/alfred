// The conversation; follows the newest line unless the user has scrolled up.
import { useComputed, type Signal } from "@preact/signals";
import { useEffect, useRef } from "preact/hooks";
import { heights } from "../lib/fold";
import { openTask, shown } from "../state/panel";
import { SendIcon } from "./icons";
import { reading } from "../state/store";
import { lines } from "../state/transcript";

const READ_FROM = 48, BACK_AT = 4;  // px from bottom: enter reading mode above READ_FROM, leave below BACK_AT
const SETTLE = 150;  // ms of no user input before following resumes
const EASE = 90;  // ms time constant of the follow glide
const TASK = /\b([Tt]ask) (\d+)\b/g;

// JARVIS's text with each "task N" as a button. Unlinked lines bind the signal directly, so streamed chunks update
// one text node without re-rendering.
function Linked({ text }: { text: Signal<string> }) {
  const linked = useComputed(() => shown.value && /\btask \d+\b/i.test(text.value));
  if (!linked.value) return <>{text}</>;
  const t = text.value;
  const parts: preact.ComponentChildren[] = [];
  let last = 0;
  for (const m of t.matchAll(TASK)) {
    parts.push(t.slice(last, m.index));
    parts.push(<button class="tref" onClick={() => openTask(Number(m[2]))} title={`Open task ${m[2]}`}>{m[0]}</button>);
    last = m.index! + m[0].length;
  }
  parts.push(t.slice(last));
  return <>{parts}</>;
}

// Separate component so `reading` changes re-render only this button
function Latest({ go }: { go: () => void }) {
  return <button id="latest" type="button" tabIndex={reading.value ? 0 : -1} onClick={go}><SendIcon />Latest</button>;
}

export function Transcript() {
  const box = useRef<HTMLElement>(null);
  const latest = useRef(() => {});

  useEffect(() => {
    const t = box.current!, feed = t.firstElementChild as HTMLElement, log = feed.firstElementChild as HTMLElement;
    const still = matchMedia("(prefers-reduced-motion: reduce)");
    let height = t.clientHeight, fromBottom = 0, prevTop = t.scrollTop, wrote = -1;
    let raf = 0, last = 0, touching = false, userAt = 0;
    let anchor: HTMLElement | null = null, anchorTop = 0;

    const end = () => t.scrollHeight - t.clientHeight;
    // el's offset within the transcript. Summed up the chain: while sliding, the transformed feed becomes the
    // offsetParent.
    const topOf = (el: HTMLElement) => {
      let y = 0;
      for (let e: Element | null = el; e instanceof HTMLElement && e !== t; e = e.offsetParent) y += e.offsetTop;
      return y;
    };
    const write = (top: number) => {
      t.scrollTop = top;
      wrote = prevTop = t.scrollTop;
      fromBottom = t.scrollHeight - wrote - t.clientHeight;
    };

    // Follow: glide to the newest line instead of jumping per chunk; wait while touched or recently scrolled.
    const step = (now: number) => {
      raf = 0;
      if (reading.peek() || touching) return;
      if (now - userAt < SETTLE) { last = now; raf = requestAnimationFrame(step); return; }
      const gap = end() - t.scrollTop;
      if (gap <= 1 || still.matches) { if (gap > 0) write(end()); return; }
      const dt = Math.min(now - last, 64);
      last = now;
      write(t.scrollTop + Math.max(1, gap * (1 - Math.exp(-dt / EASE))));
      raf = requestAnimationFrame(step);
    };
    const follow = () => {
      if (raf || reading.peek()) return;
      last = performance.now();
      raf = requestAnimationFrame(step);
    };

    const onScroll = () => {
      if (t.clientHeight !== height) return;  // layout resize, not the user
      const top = t.scrollTop, up = top < prevTop - .5;
      prevTop = top;
      fromBottom = t.scrollHeight - top - t.clientHeight;
      if (Math.abs(top - wrote) < 1) return;  // our own scroll
      userAt = performance.now();
      if (!reading.peek() && up && fromBottom > READ_FROM) reading.value = true;
      else if (reading.peek() && fromBottom < BACK_AT) reading.value = false;
    };
    const onTouch = (e: TouchEvent) => {
      touching = e.touches.length > 0;
      userAt = performance.now();
      if (touching) { cancelAnimationFrame(raf); raf = 0; } else follow();
    };
    const onWheel = () => { userAt = performance.now(); };

    // With too few lines to scroll, a new line pushes the rest up: offset them back, then ease into place from
    // wherever the last ease was. Inline transform first so this frame shows it even if the animation starts late.
    let sliding: Animation | null = null;
    const slide = (by: number) => {
      if (!by || still.matches) return;
      const now = getComputedStyle(feed).transform, from = now !== "none" ? new DOMMatrix(now).m42 : 0;
      const start = `translateY(${from - by}px)`;
      sliding?.cancel();
      feed.style.transform = start;
      const a = sliding = feed.animate([{ transform: start }, { transform: "none" }], { duration: 320, easing: "cubic-bezier(.2,.8,.2,1)" });
      a.ready.then(() => { if (sliding === a) feed.style.transform = ""; }, () => {});
      a.onfinish = () => { if (sliding === a) { sliding = null; feed.style.transform = ""; } };
    };

    // The stage overlays the transcript top. Once lines reach under it, add room to scroll past READ_FROM so
    // reading mode can fold the stage away.
    const roomy = () => {
      const split = document.body.dataset.layout === "split";
      const content = feed.offsetHeight - (topOf(log) - topOf(feed));  // lines sit at the feed's bottom
      const pb = parseFloat(getComputedStyle(t).paddingBottom) || 0;
      const room = !split && content > t.clientHeight - pb - heights.stage - 22;
      feed.classList.toggle("room", room);
    };
    // Keep the view steady on any resize: if the transcript resized, keep distance from bottom; otherwise pin the
    // newest line on screen. Manual because Safari has no scroll anchoring.
    const settle = () => {
      roomy();
      if (t.clientHeight !== height) {
        height = t.clientHeight;
        write(Math.max(0, end() - fromBottom));
      } else if (anchor?.isConnected) {
        const moved = topOf(anchor) - anchorTop, before = t.scrollTop;
        if (moved) { write(before + moved); slide(moved - (t.scrollTop - before)); }
      }
      anchor = log.lastElementChild as HTMLElement | null;
      anchorTop = anchor ? topOf(anchor) : 0;
      if (!reading.peek()) follow();
    };
    latest.current = () => { reading.value = false; follow(); };

    // Streamed text bypasses the DOM tree, so watch sizes (also catches the typing dots and the stage)
    const resized = new ResizeObserver(settle);
    resized.observe(t);
    for (const el of feed.children) resized.observe(el);  // not feed: its min-height hides inner growth
    const host = t.parentElement!;
    host.addEventListener("fold", settle);
    t.addEventListener("scroll", onScroll, { passive: true });
    t.addEventListener("wheel", onWheel, { passive: true });
    for (const type of ["touchstart", "touchend", "touchcancel"] as const) t.addEventListener(type, onTouch, { passive: true });
    return () => {
      resized.disconnect(); host.removeEventListener("fold", settle);
      t.removeEventListener("scroll", onScroll); t.removeEventListener("wheel", onWheel);
      for (const type of ["touchstart", "touchend", "touchcancel"] as const) t.removeEventListener(type, onTouch);
      cancelAnimationFrame(raf); sliding?.cancel();
      reading.value = false;
    };
  }, []);

  return (
    <>
    <section id="transcript" ref={box} aria-label="Conversation">
      <div class="feed">
        <div id="log" aria-live="polite">
          {lines.value.map((line) => line.kind === "divider"
            ? <div key={line.id} class="divider">{line.text}</div>
            : <div key={line.id} class={`msg ${line.who}`} data-segment={line.segment}>
                <p>{line.who === "jarvis" ? <Linked text={line.text} /> : line.text}</p>
              </div>)}
        </div>
        <div class="typing" aria-hidden="true"><i /><i /><i /></div>
        <div id="suggest">
          <h2>Try asking</h2>
          <p>“What's on my calendar today?”</p>
          <p>“Anything important in my inbox?”</p>
          <p>“How are my background tasks doing?”</p>
        </div>
      </div>
    </section>
    <Latest go={() => latest.current()} />
    </>
  );
}
