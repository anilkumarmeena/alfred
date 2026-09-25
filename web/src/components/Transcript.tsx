// The conversation, following the newest line unless you've scrolled up to read.
import { useComputed, type Signal } from "@preact/signals";
import { useEffect, useRef } from "preact/hooks";
import { heights } from "../lib/fold";
import { openTask, shown } from "../state/panel";
import { SendIcon } from "./icons";
import { reading } from "../state/store";
import { lines } from "../state/transcript";

const READ_FROM = 48, BACK_AT = 4;  // px from the bottom: reading back starts past the first, ends under the second
const SETTLE = 150;  // ms the reader leaves the transcript alone before following picks up again
const EASE = 90;  // ms: how quickly following glides down to the newest line
const TASK = /\b([Tt]ask) (\d+)\b/g;

// JARVIS's words, with each "task 6" a button that opens it in the panel. Most lines link nothing: their text is
// bound straight to its node, so a streamed chunk changes one text node and re-renders nothing.
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

// Its own component, so reading back re-renders this button rather than the whole conversation
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
    // Where el lies in the transcript's content. Not el.offsetTop alone: while the feed slides (below) it's
    // transformed, which makes it the offsetParent of everything in it.
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

    // Following: glide down to the newest line, rather than jump a line at a time as it streams in, and only once
    // the reader has left the transcript alone (a finger on it, or a scroll just now, waits).
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
      if (t.clientHeight !== height) return;  // the layout's doing, mid-resize (below), not the reader's
      const top = t.scrollTop, up = top < prevTop - .5;
      prevTop = top;
      fromBottom = t.scrollHeight - top - t.clientHeight;
      if (Math.abs(top - wrote) < 1) return;  // following's own scroll
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

    // What scrolling can't absorb: with too few lines to scroll, the lines sit at the bottom and each new one pushes
    // the rest up. Show them where they were, then ease them into place, from wherever the last ease had got to.
    // The offset goes on inline first, so this very frame shows it even if the animation starts a frame late.
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

    // The stage lies over the top of the transcript (lib/fold.ts). Once lines reach under it, leave room to scroll
    // up past READ_FROM, so reading back can fold it away and show them.
    const roomy = () => {
      const split = document.body.dataset.layout === "split";
      const content = feed.offsetHeight - (topOf(log) - topOf(feed));  // the lines sit at the bottom of the feed
      const pb = parseFloat(getComputedStyle(t).paddingBottom) || 0;
      const room = !split && content > t.clientHeight - pb - heights.stage - 22;
      feed.classList.toggle("room", room);
    };
    // Whatever changed size, keep the view steady: when the transcript itself resizes, the reader stays as far from
    // the bottom as they were; otherwise the newest line stays where it was on screen, whatever moved above it
    // (the stage, room, old lines dropping off the top). Safari has no scroll anchoring to do this itself.
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

    // Lines stream in a chunk at a time, straight into their text nodes: watch the sizes rather than the DOM, which
    // also catches the typing dots and the stage
    const resized = new ResizeObserver(settle);
    resized.observe(t);
    for (const el of feed.children) resized.observe(el);  // not the feed: its min-height hides growth inside it
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
