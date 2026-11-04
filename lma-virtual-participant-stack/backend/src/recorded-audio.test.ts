/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import { strict as assert } from 'node:assert';
import { test } from 'node:test';

import { recordedAudio } from './recorded-audio.js';

test('captured audio is recorded as it is', () => {
    const chunk = Buffer.from([1, 2, 3, 4]);
    assert.equal(recordedAudio(chunk, true), chunk);
});

test('while capture is off the recording gets silence of the same length', () => {
    const recorded = recordedAudio(Buffer.from([9, 9, 9, 9, 9, 9]), false);
    assert.equal(recorded.length, 6);
    assert.ok(recorded.every((byte) => byte === 0));
});

test('a pause leaves the recording as long as the audio the engine was sent', () => {
    // Ten 100 ms chunks of 16 kHz mono PCM16, the middle four paused: the engine
    // is sent all ten (silence while paused), so its clock reads one second at
    // the end; the recording must read one second too, or every line after the
    // pause would play 0.4 s ahead of its audio.
    const chunks = Array.from({ length: 10 }, (_, i) => Buffer.alloc(3200, i + 1));
    const capturing = (i: number) => i < 3 || i > 6;
    const recording = Buffer.concat(chunks.map((chunk, i) => recordedAudio(chunk, capturing(i))));

    assert.equal(recording.length / (16000 * 2), 1);
    // What was said while paused is not in it.
    assert.ok(recording.subarray(3 * 3200, 7 * 3200).every((byte) => byte === 0));
    assert.equal(recording[7 * 3200], 8);
});
