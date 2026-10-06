// Reading back up the transcript (body[data-reading]) folds the stage into a strip: the core shrinks to the left of
// what JARVIS is doing, so the transcript gets the room. Everything moves by transform alone, over a transcript
// that runs the full height under the stage the whole time, so folding changes no layout and never touches the
// transcript's scroll position. (Animating the stage's size instead resized the core's canvas and re-scrolled the
// transcript every frame, which stuttered, and on iOS fought the finger mid-scroll.)
//
// This works out where each part goes, as CSS variables the stylesheet applies while reading (--fx, --fy, --fs),
// and the stage's two heights (--stage-h, --strip-h on its parent), which the transcript is masked from.
const CORE = 46, GAP = 12, ROW_GAP = 4, TOP = 4, BOTTOM = 6;  // px: the strip's core, its gaps and its padding
const STATUS = 10.5 / 13, HINT = 12.5 / 14;  // the readout's two lines, scaled to the strip's font sizes
const BELOW = ["unlockAudio", "error", "notice"];  // shown under the strip, each on its own line; the rest fade out

/** The stage's height, and the strip's when folded: the transcript reads them on the parent's "fold" event. */
export const heights = { stage: 0, strip: 0 };

function place(el: HTMLElement, x: number, y: number, s = 1) {
  el.style.setProperty("--fx", `${x.toFixed(1)}px`);
  el.style.setProperty("--fy", `${y.toFixed(1)}px`);
  el.style.setProperty("--fs", s.toFixed(4));
}

// Measures the stage now and whenever it or its readout changes size. Returns a function that stops it.
export function mountFold(stage: HTMLElement): () => void {
  const host = stage.parentElement!;
  const $ = (id: string) => stage.querySelector<HTMLElement>(`#${id}`);

  function measure() {
    const core = $("core"), status = $("status"), hint = $("hint");
    const cw = core?.offsetWidth;
    if (!core || !status || !hint || !cw) return;  // not laid out (yet)
    // Offsets ignore transforms, so these are the unfolded places whether or not the stage is folded now
    const pad = parseFloat(getComputedStyle(status).paddingLeft) || 0;  // the status's letter-spacing balance
    const sh = status.offsetHeight * STATUS, textH = sh + 2 + hint.offsetHeight * HINT;
    const textW = Math.max((status.offsetWidth - pad) * STATUS, hint.offsetWidth * HINT);
    const row = Math.max(CORE, textH), left = Math.max(0, (stage.clientWidth - CORE - GAP - textW) / 2);
    place(core, left + CORE / 2 - core.offsetLeft - cw / 2, TOP + row / 2 - core.offsetTop - cw / 2, CORE / cw);
    const x = left + CORE + GAP, y = TOP + (row - textH) / 2;
    place(status, x - pad * STATUS - status.offsetLeft, y - status.offsetTop, STATUS);
    place(hint, x - hint.offsetLeft, y + sh + 2 - hint.offsetTop, HINT);
    let bottom = TOP + row;
    for (const id of BELOW) {
      const el = $(id);
      if (!el?.offsetHeight) continue;
      bottom += ROW_GAP;
      place(el, 0, bottom - el.offsetTop);
      bottom += el.offsetHeight;
    }
    const stageH = stage.offsetHeight, stripH = Math.round(bottom + BOTTOM);
    if (stageH === heights.stage && stripH === heights.strip) return;
    heights.stage = stageH; heights.strip = stripH;
    host.style.setProperty("--stage-h", `${stageH}px`);
    host.style.setProperty("--strip-h", `${stripH}px`);
    host.dispatchEvent(new Event("fold"));
  }

  const resized = new ResizeObserver(measure);
  resized.observe(stage);
  for (const id of ["status", "hint"]) { const el = $(id); if (el) resized.observe(el); }
  measure();
  // The transcript's mask follows --stage-h with a transition: not from the first measurement, or it would sweep
  // down over the page as it loads
  let raf = requestAnimationFrame(() => { raf = requestAnimationFrame(() => { host.dataset.fold = ""; }); });
  return () => { resized.disconnect(); cancelAnimationFrame(raf); delete host.dataset.fold; };
}
