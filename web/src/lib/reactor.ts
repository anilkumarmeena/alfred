// The core's canvas animation, tinted by agent state. Runs while visible; wakeReactor() restarts it on return.
import type { Tap } from "./audio";

export interface ReactorInput {
  state: string;  // as on body[data-state]
  connected: boolean;  // full brightness
  muted: boolean;
  paused: boolean;
  agent: Tap | null;
  mic: Tap | null;
}

const TINT: Record<string, number[]> = {  // unlisted states use idle
  idle: [79, 209, 255],
  thinking: [255, 181, 71], speaking: [150, 234, 255], reconnecting: [255, 181, 71], muted: [255, 95, 109],
};
const SPIN: Record<string, number> = { thinking: 2.4, connecting: 1.5, initializing: 1.5, reconnecting: 1.5 };
const IDLE_LEVEL: Record<string, number> = { idle: .05, connecting: .12, initializing: .14, listening: .07, thinking: .2, speaking: .3, reconnecting: .1 };
const BARS = 56, HALF = BARS / 2, TAU = Math.PI * 2;
const IDLE_FRAME = 1000 / 30;  // frame interval (ms) with no audio: idle wave at half the cost

let wake: (() => void) | null = null;

export function wakeReactor(): void { wake?.(); }

// Draw on cv from read() each frame. Returns a stop function.
export function mountReactor(cv: HTMLCanvasElement, read: () => ReactorInput): () => void {
  const g = cv.getContext("2d")!;
  const still = matchMedia("(prefers-reduced-motion: reduce)");
  const bars = new Float32Array(HALF), col = [79, 209, 255];
  let size = 0, bitmap = 0, level = 0, spin = 0, last = 0, raf = 0, shown = 1;

  // Bitmap only grows: resizing clears it, so a resizing core would blink. Drawing is in CSS pixels, scaled.
  function fit() {
    size = cv.clientWidth;
    if (!size) return;
    const want = Math.round(size * Math.min(window.devicePixelRatio || 1, 2.5));
    const grew = want > bitmap;
    if (grew) { bitmap = want; cv.width = cv.height = want; }
    g.setTransform(bitmap / size, 0, 0, bitmap / size, 0, 0);
    if (grew) frame(performance.now(), true);  // redraw now rather than show it cleared
  }
  const resize = new ResizeObserver(fit);
  resize.observe(cv);

  function frame(now: number, forced = false) {
    if (!forced) raf = document.hidden ? 0 : requestAnimationFrame(frame);
    const { state: s, connected, agent, mic, paused, muted: wantsMuted } = read();
    const muted = connected && wantsMuted;
    // Source: Alfred while speaking, the mic while listening, else an idle wave.
    const src = s === "speaking" ? agent : connected && !muted && !paused && s === "listening" ? mic : null;
    if (!src && !forced && now - last < IDLE_FRAME - 2) return;  // -2 ms tolerates slightly early 60 Hz frames

    const dt = Math.min((now - last) / 1000 || 0, .05); last = now;
    const t = now / 1000, calm = still.matches;
    const tint = muted ? TINT.muted! : paused ? TINT.reconnecting! : TINT[s] || TINT.idle!;
    for (let i = 0; i < 3; i++) col[i]! += (tint[i]! - col[i]!) * Math.min(1, dt * 5);

    let raw = 0;
    if (src) src.an.getByteFrequencyData(src.freq);
    for (let i = 0; i < HALF; i++) {
      let v: number;
      if (src) v = src.freq[i + 1]! / 255;
      else {
        const base = muted ? .03 : IDLE_LEVEL[s] ?? .05;
        v = calm ? base : base * (.55 + .45 * Math.sin(i * .8 + t * (s === "thinking" ? 7 : s === "speaking" ? 9 : 2.2)));
      }
      bars[i]! += (v - bars[i]!) * (v > bars[i]! ? .5 : .14);
      raw += v;
    }
    raw = Math.min(1, raw / HALF * (src ? 2.2 : 1));
    level += (raw - level) * (raw > level ? .35 : .08);

    const R = size / 2;
    if (!R) return;
    const rgb = `${col[0]! | 0},${col[1]! | 0},${col[2]! | 0}`, c = (a: number) => `rgba(${rgb},${a})`;
    const on = connected ? 1 : .6;
    spin += (calm ? 0 : SPIN[s] ?? .1) * dt;
    // When folded (reading back) the core is scaled down by --fs: keep hairlines 1 screen px through the transition.
    const fold = document.body.dataset.reading != null ? Number(cv.parentElement?.style.getPropertyValue("--fs")) || 1 : 1;
    shown = calm ? fold : shown + (fold - shown) * Math.min(1, dt * 9);
    const px = 1 / shown;
    g.clearRect(0, 0, size, size);
    g.save(); g.translate(R, R);

    // Outer tick ring, turning slowly
    for (const major of [false, true]) {
      g.beginPath();
      for (let i = major ? 0 : 1; i < 72; i += major ? 6 : 1) {
        if (!major && i % 6 === 0) continue;
        const a = i / 72 * TAU + spin * .12, r1 = R * (major ? .895 : .925), r2 = R * .965;
        g.moveTo(Math.cos(a) * r1, Math.sin(a) * r1); g.lineTo(Math.cos(a) * r2, Math.sin(a) * r2);
      }
      g.strokeStyle = c((major ? .55 : .2) * on); g.lineWidth = (major ? 1.6 : 1) * px; g.stroke();
    }

    // Three arc segments, fast while thinking or connecting
    g.lineCap = "round"; g.lineWidth = Math.max(2 * px, R * .022);
    g.strokeStyle = c((.5 + .35 * level) * on);
    for (let k = 0; k < 3; k++) {
      const a0 = spin + k * TAU / 3;
      g.beginPath(); g.arc(0, 0, R * .835, a0, a0 + TAU / 3 - .9); g.stroke();
    }
    // Counter-rotating dashed ring
    g.lineWidth = px; g.strokeStyle = c(.28 * on);
    g.setLineDash([2 * px, 7 * px]);
    g.beginPath(); g.arc(0, 0, R * .775, -spin * .6, -spin * .6 + TAU); g.stroke();
    g.setLineDash([]);

    // Spectrum bars, mirrored left and right from the top
    g.globalCompositeOperation = "lighter";
    g.lineWidth = Math.max(1.5 * px, R * .02);
    for (let k = 0; k < BARS; k++) {
      const v = bars[k < HALF ? k : BARS - 1 - k]!;
      const a = -Math.PI / 2 + (k + .5) * TAU / BARS, r1 = R * .5, r2 = r1 + R * (.018 + .2 * v);
      g.strokeStyle = c((.22 + .7 * v) * on);
      g.beginPath(); g.moveTo(Math.cos(a) * r1, Math.sin(a) * r1); g.lineTo(Math.cos(a) * r2, Math.sin(a) * r2); g.stroke();
    }

    // Core glow
    const cr = R * (.3 + .16 * level + (calm ? 0 : Math.sin(t * 1.6) * .015));
    const glow = g.createRadialGradient(0, 0, 0, 0, 0, cr);
    glow.addColorStop(0, c((.5 + .4 * level) * on)); glow.addColorStop(.45, c((.16 + .2 * level) * on)); glow.addColorStop(1, c(0));
    g.fillStyle = glow; g.beginPath(); g.arc(0, 0, cr, 0, TAU); g.fill();
    g.globalCompositeOperation = "source-over";

    g.strokeStyle = c(.45 * on); g.lineWidth = px;
    g.beginPath(); g.arc(0, 0, R * .255, 0, TAU); g.stroke();

    // Bright centre with a halo. A radial gradient mimics shadowBlur=b, which would be CPU-blurred every frame.
    const w = col.map((v) => (v + (255 - v) * .72) | 0);
    const r = R * (.07 + .045 * level), b = R * .12 * (.6 + level), span = r / 2 + b;
    const halo = g.createRadialGradient(0, 0, r / 2, 0, 0, r + b);
    halo.addColorStop(0, c(.45)); halo.addColorStop(r / 2 / span, c(.45)); halo.addColorStop((r + b) / 2 / span, c(.14)); halo.addColorStop(1, c(0));
    g.fillStyle = halo; g.beginPath(); g.arc(0, 0, r + b, 0, TAU); g.fill();
    g.fillStyle = `rgb(${w})`;
    g.beginPath(); g.arc(0, 0, r, 0, TAU); g.fill();
    g.restore();
  }

  wake = () => { if (!raf) raf = requestAnimationFrame(frame); };
  raf = requestAnimationFrame(frame);
  return () => { cancelAnimationFrame(raf); raf = 0; resize.disconnect(); wake = null; };
}
