// Tests for src/lib/record.ts: the voiceprint recording's conversion to what the panel takes, 16 kHz 16-bit PCM in
// base64. Run with: make test (or npm test in web/)
import assert from "node:assert/strict";
import { test } from "vitest";
import { downsample, join, level, RATE, toBase64, toPcm16 } from "../src/lib/record";

test("48 kHz becomes 16 kHz, each sample the mean of the three it covers", () => {
  const out = downsample(new Float32Array([0.3, 0.6, 0.9, -0.3, -0.6, -0.9, 1, 1]), 48000);
  assert.equal(out.length, 2);
  assert.ok(Math.abs(out[0]! - 0.6) < 1e-6 && Math.abs(out[1]! + 0.6) < 1e-6);
});

test("44.1 kHz comes out at the right length, and 16 kHz unchanged", () => {
  assert.equal(downsample(new Float32Array(44100), 44100).length, RATE);
  const same = new Float32Array([0.1, 0.2]);
  assert.deepEqual(Array.from(downsample(same, RATE)), Array.from(same));
  assert.notEqual(downsample(same, RATE), same);  // a copy
});

test("samples become 16-bit, clipped to full scale", () => {
  assert.deepEqual(Array.from(toPcm16(new Float32Array([0, 0.5, -0.5, 1, -1, 2, -2]))),
    [0, 16384, -16383, 32767, -32767, 32767, -32767]);  // ±16383.5 rounded up
});

test("PCM goes as base64 of its little-endian bytes, however long", () => {
  const pcm = toPcm16(new Float32Array([0.5, -1]));
  assert.equal(toBase64(new Uint8Array(pcm.buffer)), btoa(String.fromCharCode(0x00, 0x40, 0x01, 0x80)));
  const long = new Uint8Array(100_000).fill(65);
  assert.equal(atob(toBase64(long)).length, 100_000);  // in chunks, past what one fromCharCode call takes
});

test("chunks join in order, and the level meter rises with loudness", () => {
  assert.deepEqual(Array.from(join([new Float32Array([1, 2]), new Float32Array([3])])), [1, 2, 3]);
  assert.equal(level(new Float32Array(100)), 0);
  assert.ok(level(new Float32Array(100).fill(0.05)) > 0.3);
  assert.equal(level(new Float32Array(100).fill(1)), 1);
});
