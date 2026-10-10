// The page's single AudioContext, and track analysers (for the reactor, and for detecting iOS mic-mute silence).

let actx: AudioContext | null = null;

export const audioContext = (): AudioContext | null => actx;

// Create within a user gesture so iOS lets it run.
export function audioCtx(): AudioContext | null {
  const AC = window.AudioContext || (window as Window & { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
  if (!actx && AC) { try { actx = new AC(); } catch {} }
  resumeAudio();
  return actx;
}

export function resumeAudio(): void {  // iOS also has "interrupted"
  if (actx && actx.state !== "running" && actx.state !== "closed") actx.resume().catch(() => {});
}

export interface Tap {
  track: MediaStreamTrack;
  copy: MediaStreamTrack | null;  // clone actually analysed, if requested
  src: MediaStreamAudioSourceNode;
  an: AnalyserNode;
  freq: Uint8Array<ArrayBuffer>;
  wave: Float32Array<ArrayBuffer>;
  at: number;
}

// copy: analyse a clone, which keeps hearing when the page mutes the mic.
export function tap(track: MediaStreamTrack | undefined, copy = false): Tap | null {
  const a = audioCtx();
  if (!a || !track) return null;
  let feed = track;
  try {
    if (copy) { feed = track.clone(); feed.enabled = true; }
    const src = a.createMediaStreamSource(new MediaStream([feed]));
    const an = a.createAnalyser();
    an.fftSize = 256; an.smoothingTimeConstant = .72;
    src.connect(an);  // analysis only; playback stays on the <audio> element
    return { track, copy: copy ? feed : null, src, an, freq: new Uint8Array(an.frequencyBinCount), wave: new Float32Array(an.fftSize), at: Date.now() };
  } catch { if (feed !== track) feed.stop(); return null; }
}

export function untap(t: Tap | null): null {
  try { t && t.src.disconnect(); } catch {}
  try { t && t.copy && t.copy.stop(); } catch {}
  return null;
}
