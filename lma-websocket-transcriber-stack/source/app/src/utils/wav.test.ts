/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Tests for the WAV header and u-Law decoding used by the meeting recorder.
 *
 * These are worth pinning because the header is assembled from bare decimal
 * integers written big-endian (`1380533830` is 'RIFF', `1463899717` is 'WAVE',
 * and so on). A single transposed digit produces a 44-byte header that looks
 * plausible, uploads without error, and yields a recording no player will open —
 * a failure that surfaces only when somebody tries to play back a meeting, long
 * after the audio is gone. Nothing else in the codebase checks these constants.
 */
import { strict as assert } from 'node:assert';
import { test } from 'node:test';
import { createWavHeader, msToBytes, ulawToL16 } from './wav';

/** The recorder writes 16-bit stereo, so a frame is 4 bytes. */
const BYTES_PER_FRAME = 4;

test('the header is exactly the canonical 44 bytes', () => {
    // Any other length shifts the audio data and desynchronises playback.
    assert.equal(createWavHeader(8000, 0).length, 44);
});

test('the four RIFF chunk identifiers are correct ASCII', () => {
    // The whole point of this file: the identifiers are written as decimal
    // integers, so this is the only place their values are verified.
    const header = createWavHeader(16000, 1024);
    assert.equal(header.toString('ascii', 0, 4), 'RIFF');
    assert.equal(header.toString('ascii', 8, 12), 'WAVE');
    assert.equal(header.toString('ascii', 12, 16), 'fmt ');
    assert.equal(header.toString('ascii', 36, 40), 'data');
});

test('the declared sizes account for the header itself', () => {
    // RIFF size counts everything after the first 8 bytes: 36 + payload. Getting
    // this wrong makes players either truncate the audio or read past the end.
    const length = 64000;
    const header = createWavHeader(8000, length);
    assert.equal(header.readUInt32LE(4), 36 + length, 'RIFF chunk size');
    assert.equal(header.readUInt32LE(40), length, 'data chunk size');
});

test('the format block describes 16-bit stereo PCM', () => {
    // The transcriber interleaves two channels (caller and agent) at 16 bits.
    // A mismatch here does not fail, it just plays back at the wrong speed or
    // swaps the speakers into one another's channel.
    const header = createWavHeader(8000, 0);
    assert.equal(header.readUInt32LE(16), 16, 'fmt chunk length');
    assert.equal(header.readUInt16LE(20), 1, 'format tag: uncompressed PCM');
    assert.equal(header.readUInt16LE(22), 2, 'channel count');
    assert.equal(header.readUInt16LE(34), 16, 'bits per sample');
});

test('byte rate and block align are derived from the sample rate', () => {
    // These two must agree with the sample rate or playback drifts in pitch.
    for (const rate of [8000, 16000, 44100]) {
        const header = createWavHeader(rate, 0);
        assert.equal(header.readUInt32LE(24), rate, 'sample rate');
        assert.equal(header.readUInt32LE(28), rate * BYTES_PER_FRAME, 'byte rate');
        assert.equal(header.readUInt16LE(32), BYTES_PER_FRAME, 'block align');
    }
});

test('msToBytes converts a duration to whole frames of PCM', () => {
    // 1s of 16 kHz 16-bit mono is 32000 bytes; the helper takes bytes-per-sample
    // rather than per-frame, so callers pass 2 for one 16-bit channel.
    assert.equal(msToBytes(1000, 16000, 2), 32000);
    assert.equal(msToBytes(500, 8000, 2), 8000);
    assert.equal(msToBytes(0, 16000, 2), 0);
});

test('the u-Law table covers every possible byte', () => {
    // A short table would silently decode high bytes to undefined, which lands in
    // an Int16Array as 0 — i.e. audible dropouts rather than an error.
    const all = new Uint8Array(256);
    for (let i = 0; i < 256; i += 1) {
        all[i] = i;
    }
    const decoded = ulawToL16(all);
    assert.equal(decoded.length, 256);
    for (let i = 0; i < 256; i += 1) {
        assert.ok(Number.isInteger(decoded[i]), `byte ${i} decoded to a non-integer`);
        assert.ok(decoded[i] >= -32768 && decoded[i] <= 32767, `byte ${i} out of Int16 range`);
    }
});

test('u-Law silence decodes to silence', () => {
    // 0xFF and 0x7F are the u-Law encodings nearest zero. If the table were
    // shifted by one, silence would decode to full-scale and a quiet recording
    // would come back as a loud click.
    assert.equal(ulawToL16(new Uint8Array([0xff]))[0], 0);
    assert.equal(ulawToL16(new Uint8Array([0x7f]))[0], -1);
});

test('the u-Law sign bit maps to the sign of the sample', () => {
    // In u-Law the top bit is the sign, so byte i and byte i+128 must decode to
    // the same magnitude with opposite signs. This catches a table pasted in the
    // wrong order far more reliably than spot-checking single entries.
    const low = ulawToL16(Uint8Array.from({ length: 128 }, (_, i) => i));
    const high = ulawToL16(Uint8Array.from({ length: 128 }, (_, i) => i + 128));
    for (let i = 0; i < 128; i += 1) {
        assert.ok(low[i] < 0, `byte ${i} should decode negative`);
        assert.ok(high[i] >= 0, `byte ${i + 128} should decode non-negative`);
        // The two halves differ by one least-significant step at the extremes
        // (-1 vs 0, -32124 vs 32124), so compare magnitudes within that tolerance.
        assert.ok(
            Math.abs(Math.abs(low[i]) - high[i]) <= 1,
            `magnitude mismatch at ${i}: ${low[i]} vs ${high[i]}`,
        );
    }
});

test('an empty buffer decodes to an empty result', () => {
    assert.equal(ulawToL16(new Uint8Array([])).length, 0);
});
