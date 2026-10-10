// Reading back (body[data-reading]) folds the stage into a strip beside the status. Transforms only, over a
// full-height transcript: no layout change and no scroll-position change. (Animating the stage's size would resize
// the canvas and re-scroll every frame, stuttering and fighting the finger on iOS.)
//
// Computes per-part CSS vars --fx/--fy/--fs, plus --stage-h/--strip-h on the parent for the transcript mask.
const CORE = 46, GAP = 12, ROW_GAP = 4, TOP = 4, BOTTOM = 6;  // px: strip core size, gaps, padding
const STATUS = 10.5 / 13, HINT = 12.5 / 14;  // readout font scale in the strip
const BELOW = ["unlockAudio", "error", "notice"];  // stacked under the strip; everything else fades out

/** Stage and folded-strip heights; the transcript reads them on the parent's "fold" event. */
export const heights = { stage: 0, strip: 0 };

function place(el: HTMLElement, x: number, y: number, s = 1) {
  el.style.setProperty("--fx", `${x.toFixed(1)}px`);
  el.style.setProperty("--fy", `${y.toFixed(1)}px`);
  el.style.setProperty("--fs", s.toFixed(4));
}

// Measure the stage now and on any resize of it or its readout. Returns a stop function.
export function mountFold(stage: HTMLElement): () => void {
  const host = stage.parentElement!;
  const $ = (id: string) => stage.querySelector<HTMLElement>(`#${id}`);

  function measure() {
    const core = $("core"), status = $("status"), hint = $("hint");
    const cw = core?.offsetWidth;
    if (!core || !status || !hint || !cw) return;  // not laid out yet
    // Offsets ignore transforms, so these are unfolded positions even while folded
    const pad = parseFloat(getComputedStyle(status).paddingLeft) || 0;  // letter-spacing balance
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
  // Mask transition only after the first measurement, or it sweeps down the page on load
  let raf = requestAnimationFrame(() => { raf = requestAnimationFrame(() => { host.dataset.fold = ""; }); });
  return () => { resized.disconnect(); cancelAnimationFrame(raf); delete host.dataset.fold; };
}
