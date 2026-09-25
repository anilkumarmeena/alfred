// The page's one AudioContext, and analysers on audio tracks: the reactor draws their levels, and the
// earphone code listens to a copy of the mic for the digital silence of iOS muting it.

let actx: AudioContext | null = null;

export const audioContext = (): AudioContext | null => actx;

// Create inside a tap so iOS lets it run.
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
  track: MediaStreamTrack;  // the track tapped
  copy: MediaStreamTrack | null;  // the clone actually analysed, if asked for one
  src: MediaStreamAudioSourceNode;
  an: AnalyserNode;
  freq: Uint8Array<ArrayBuffer>;
  wave: Float32Array<ArrayBuffer>;
  at: number;
}

// copy: analyse a copy of the track, which keeps hearing when the page mutes the mic.
export function tap(track: MediaStreamTrack | undefined, copy = false): Tap | null {
  const a = audioCtx();
  if (!a || !track) return null;
  let feed = track;
  try {
    if (copy) { feed = track.clone(); feed.enabled = true; }
    const src = a.createMediaStreamSource(new MediaStream([feed]));
    const an = a.createAnalyser();
    an.fftSize = 256; an.smoothingTimeConstant = .72;
    src.connect(an);  // analysed only; playback stays on the <audio> element
    return { track, copy: copy ? feed : null, src, an, freq: new Uint8Array(an.frequencyBinCount), wave: new Float32Array(an.fftSize), at: Date.now() };
  } catch { if (feed !== track) feed.stop(); return null; }
}

export function untap(t: Tap | null): null {
  try { t && t.src.disconnect(); } catch {}
  try { t && t.copy && t.copy.stop(); } catch {}
  return null;
}
