/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The audio conversion the enrollment portal does before anything is sent.
 *
 * Worth testing directly because a fault here is invisible rather than loud: a
 * mis-scaled or mis-resampled clip still produces a voiceprint, just one that
 * recognises the wrong person or nobody. The numbers below are chosen so the
 * expected output is arithmetic rather than a recorded blob.
 */
import { describe, it, expect } from 'vitest';

import { clipToPcm, resample, toBase64, toMono, toPcm16, TARGET_SAMPLE_RATE } from './pcm';

describe('mixing to mono', () => {
  it('averages the channels rather than keeping one', () => {
    // A laptop with a stereo microphone can put most of a voice on one side;
    // keeping only the other would quietly halve the signal.
    const left = new Float32Array([1, 0, -1]);
    const right = new Float32Array([0, 1, -1]);
    expect(Array.from(toMono([left, right], 3))).toEqual([0.5, 0.5, -1]);
  });

  it('passes a single channel through unchanged', () => {
    expect(Array.from(toMono([new Float32Array([0.25, -0.5])], 2))).toEqual([0.25, -0.5]);
  });
});

describe('resampling', () => {
  it('returns the samples untouched when the rate already matches', () => {
    const samples = new Float32Array([0.1, 0.2]);
    expect(resample(samples, 16000, 16000)).toBe(samples);
  });

  it('produces the right number of samples when downsampling', () => {
    const samples = new Float32Array(48000);
    expect(resample(samples, 48000, 16000)).toHaveLength(16000);
  });

  it('interpolates rather than dropping samples', () => {
    // Halving the rate of [0, 1, 2, 3] should land on the even positions.
    const out = resample(new Float32Array([0, 1, 2, 3]), 2, 1);
    expect(Array.from(out)).toEqual([0, 2]);
  });

  it('does not read past the end of the input', () => {
    const out = resample(new Float32Array([0, 1, 2]), 3, 2);
    expect(out.every((value) => Number.isFinite(value))).toBe(true);
  });
});

describe('quantising to 16-bit', () => {
  it('maps full scale to the 16-bit range', () => {
    const view = new DataView(toPcm16(new Float32Array([0, 1, -1])));
    expect(view.getInt16(0, true)).toBe(0);
    expect(view.getInt16(2, true)).toBe(32767);
    expect(view.getInt16(4, true)).toBe(-32767);
  });

  it('clamps a sample outside the range instead of letting it wrap', () => {
    // Slightly out-of-range samples are normal after resampling, and a wrap
    // would put a loud click in a recording somebody is identified by.
    const view = new DataView(toPcm16(new Float32Array([1.5, -1.5])));
    expect(view.getInt16(0, true)).toBe(32767);
    expect(view.getInt16(2, true)).toBe(-32767);
  });

  it('writes little-endian, which is what pcm_s16le means', () => {
    const bytes = new Uint8Array(toPcm16(new Float32Array([1])));
    expect(bytes[0]).toBe(0xff);
    expect(bytes[1]).toBe(0x7f);
  });
});

describe('base64', () => {
  it('round-trips through atob', () => {
    const buffer = toPcm16(new Float32Array([0, 0.5, -0.5]));
    const decoded = atob(toBase64(buffer));
    expect(decoded).toHaveLength(6);
  });

  it('handles a clip longer than one chunk', () => {
    // Encoding in one call would overflow the argument list on a real clip.
    const long = toPcm16(new Float32Array(200000));
    expect(atob(toBase64(long))).toHaveLength(400000);
  });
});

describe('converting a recorded clip', () => {
  const fakeDecode = (sampleRate, channels, length) => async () => ({
    sampleRate,
    length,
    numberOfChannels: channels.length,
    getChannelData: (index) => channels[index],
  });

  it('produces base64 PCM and the duration at the target rate', async () => {
    // Three seconds at 48 kHz is three seconds at 16 kHz.
    const samples = new Float32Array(48000 * 3);
    const blob = { arrayBuffer: async () => new ArrayBuffer(0) };
    const result = await clipToPcm(blob, fakeDecode(48000, [samples], samples.length));
    expect(result.seconds).toBeCloseTo(3, 2);
    expect(atob(result.pcm)).toHaveLength(TARGET_SAMPLE_RATE * 3 * 2);
  });

  it('mixes a stereo recording down before sending it', async () => {
    const left = new Float32Array(16000);
    const right = new Float32Array(16000);
    const blob = { arrayBuffer: async () => new ArrayBuffer(0) };
    const result = await clipToPcm(blob, fakeDecode(16000, [left, right], 16000));
    // Mono: one sample per frame, two bytes each.
    expect(atob(result.pcm)).toHaveLength(32000);
  });

  it('reports the duration of a clip already at the target rate', async () => {
    const samples = new Float32Array(16000 * 2);
    const blob = { arrayBuffer: async () => new ArrayBuffer(0) };
    const result = await clipToPcm(blob, fakeDecode(16000, [samples], samples.length));
    expect(result.seconds).toBeCloseTo(2, 5);
  });
});
