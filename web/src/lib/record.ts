// Voiceprint recording: mic as 16 kHz mono 16-bit PCM for the panel's voiceprint_enroll.
// Conversions are pure functions; recordMic is the browser part.

export const RATE = 16000;

// Resample `from` Hz to `to` Hz with a box filter (mean of covered input): fine for speech, aliasing no worse than linear.
export function downsample(input: Float32Array, from: number, to = RATE): Float32Array {
  if (from === to) return input.slice();
  const ratio = from / to, out = new Float32Array(Math.floor(input.length / ratio));
  for (let i = 0; i < out.length; i++) {
    const start = Math.floor(i * ratio), end = Math.max(start + 1, Math.floor((i + 1) * ratio));
    let sum = 0;
    for (let j = start; j < end; j++) sum += input[j]!;
    out[i] = sum / (end - start);
  }
  return out;
}

// Float samples (clipped to ±1) to 16-bit integers
export function toPcm16(samples: Float32Array): Int16Array {
  const out = new Int16Array(samples.length);
  for (let i = 0; i < samples.length; i++) out[i] = Math.round(Math.max(-1, Math.min(1, samples[i]!)) * 32767);
  return out;
}

export function toBase64(bytes: Uint8Array): string {
  let text = "";
  for (let i = 0; i < bytes.length; i += 0x8000) text += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
  return btoa(text);
}

// Concatenate float chunks
export function join(chunks: Float32Array[]): Float32Array {
  const out = new Float32Array(chunks.reduce((n, c) => n + c.length, 0));
  let at = 0;
  for (const c of chunks) { out.set(c, at); at += c.length; }
  return out;
}

// Level meter value 0..1: RMS scaled so normal speech reads high
export function level(chunk: Float32Array): number {
  let sum = 0;
  for (let i = 0; i < chunk.length; i++) sum += chunk[i]! * chunk[i]!;
  return Math.min(1, Math.sqrt(sum / Math.max(1, chunk.length)) * 8);
}

export interface Recording {
  seconds(): number;
  level(): number;  // latest chunk, 0..1
  stop(): string;  // base64 of 16 kHz 16-bit PCM
  cancel(): void;
}

// Record with the same processing as a call (echo cancel, noise suppression, AGC) so the voiceprint matches live
// audio. Call directly from a user gesture: iOS only starts audio from one.
export async function recordMic(): Promise<Recording> {
  const ctx = new AudioContext();  // before any await, while the gesture still counts
  let stream: MediaStream;
  try {
    stream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1 },
    });
    await ctx.resume();
  } catch (e) {
    ctx.close().catch(() => {});
    throw new Error(`Couldn't use the microphone: ${(e as Error).message || e}`);
  }
  const source = ctx.createMediaStreamSource(stream);
  // ScriptProcessor, not AudioWorklet: no extra file, and still universally supported. Output stays silent; it's
  // connected only because it won't run otherwise.
  const node = ctx.createScriptProcessor(4096, 1, 1);
  const chunks: Float32Array[] = [];
  let samples = 0, latest = 0;
  node.onaudioprocess = (e) => {
    const chunk = new Float32Array(e.inputBuffer.getChannelData(0));
    chunks.push(chunk);
    samples += chunk.length;
    latest = level(chunk);
  };
  source.connect(node);
  node.connect(ctx.destination);
  const end = () => {
    node.onaudioprocess = null;
    source.disconnect(); node.disconnect();
    stream.getTracks().forEach((t) => t.stop());
    ctx.close().catch(() => {});
  };
  return {
    seconds: () => samples / ctx.sampleRate,
    level: () => latest,
    stop() {
      end();
      return toBase64(new Uint8Array(toPcm16(downsample(join(chunks), ctx.sampleRate)).buffer));
    },
    cancel: end,
  };
}
