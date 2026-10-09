// Recording the user's voice in the page for their voiceprint (Settings, Preferences): the mic as 16 kHz mono 16-bit PCM,
// which the panel's voiceprint_enroll takes (jarvis.voice.speaker). The conversions are plain functions, which
// tests/record.test.ts checks; recordMic is the browser part.

export const RATE = 16000;

// Float samples at `from` Hz as `to` Hz, each output sample the mean of the input it covers: a box filter, enough for
// speech on its way to a speaker model, and no worse than linear interpolation at aliasing.
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

// Float samples, clipped to ±1, as 16-bit integers
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

// Chunks of float samples joined
export function join(chunks: Float32Array[]): Float32Array {
  const out = new Float32Array(chunks.reduce((n, c) => n + c.length, 0));
  let at = 0;
  for (const c of chunks) { out.set(c, at); at += c.length; }
  return out;
}

// The loudness of a chunk, 0 to 1, for a level meter: its RMS, scaled so ordinary speech reaches most of the way
export function level(chunk: Float32Array): number {
  let sum = 0;
  for (let i = 0; i < chunk.length; i++) sum += chunk[i]! * chunk[i]!;
  return Math.min(1, Math.sqrt(sum / Math.max(1, chunk.length)) * 8);
}

export interface Recording {
  seconds(): number;  // recorded so far
  level(): number;  // the latest chunk's, 0 to 1
  stop(): string;  // ends it: the recording as base64 of 16 kHz 16-bit PCM
  cancel(): void;
}

// Start recording from the mic, with the processing a call's mic gets (echo cancelling, noise suppression, gain), so
// the voiceprint hears what a conversation does. Call it straight from a tap: iOS only starts audio from one.
export async function recordMic(): Promise<Recording> {
  const ctx = new AudioContext();  // made before any await, while the tap still counts
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
  // A ScriptProcessor rather than an AudioWorklet: one file, and every browser JARVIS runs in still has it. Its output
  // is left silent; it's connected only because it doesn't run otherwise.
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
