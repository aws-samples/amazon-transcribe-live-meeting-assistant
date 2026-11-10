/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Audio held while its connection's START is handled: held only while a START
 * is in progress, handed back in order once, bounded by bytes with the oldest
 * frames dropped and counted, and forgotten when the START is refused or the
 * connection closes.
 */
import assert from 'node:assert/strict';
import test from 'node:test';

import { PENDING_AUDIO_SECONDS, PendingAudioFrames, pendingAudioLimitBytes } from './pending-audio';

const bytes = (...values: number[]): Uint8Array => Uint8Array.from(values);

test('frames behind a START in progress are held and released in order, once', () => {
    const pending = new PendingAudioFrames<string>();
    pending.begin('ws1', 100);
    assert.equal(pending.holding('ws1'), true);
    assert.equal(pending.hold('ws1', bytes(1)), true);
    assert.equal(pending.hold('ws1', bytes(2, 2)), true);
    const released = pending.release('ws1');
    assert.deepEqual(released.frames.map((f) => f[0]), [1, 2]);
    assert.equal(released.bytes, 3);
    assert.equal(released.droppedFrames, 0);
    assert.equal(pending.holding('ws1'), false);
    assert.deepEqual(pending.release('ws1').frames, []);
    assert.equal(pending.hold('ws1', bytes(3)), false, 'no START in progress');
});

test('a connection with no START in progress holds nothing', () => {
    const pending = new PendingAudioFrames<string>();
    pending.begin('ws1', 100);
    assert.equal(pending.hold('ws2', bytes(1)), false);
    assert.equal(pending.size, 1);
});

test('frames beyond the bound drop the oldest and are counted', () => {
    const pending = new PendingAudioFrames<string>();
    pending.begin('ws1', 10);
    for (let i = 0; i < 5; i += 1) {
        pending.hold('ws1', new Uint8Array(4).fill(i));
    }
    const released = pending.release('ws1');
    assert.deepEqual(released.frames.map((f) => f[0]), [3, 4]);
    assert.equal(released.bytes, 8);
    assert.equal(released.droppedFrames, 3);
    assert.equal(released.droppedBytes, 12);
});

test('a refused START or a closed connection forgets its frames', () => {
    const pending = new PendingAudioFrames<string>();
    pending.begin('ws1', 100);
    pending.hold('ws1', bytes(1, 2, 3));
    assert.equal(pending.drop('ws1'), 3);
    assert.equal(pending.size, 0);
    assert.deepEqual(pending.release('ws1').frames, []);
    assert.equal(pending.drop('ws1'), 0);
});

test('a new START on the connection starts an empty hold', () => {
    const pending = new PendingAudioFrames<string>();
    pending.begin('ws1', 100);
    pending.hold('ws1', bytes(1));
    pending.begin('ws1', 100);
    assert.deepEqual(pending.release('ws1').frames, []);
});

test('the bound is seconds of stereo 16-bit audio at the START\'s rate, clamped', () => {
    assert.equal(pendingAudioLimitBytes(16000), 16000 * 4 * PENDING_AUDIO_SECONDS);
    assert.equal(pendingAudioLimitBytes(48000), 48000 * 4 * PENDING_AUDIO_SECONDS);
    assert.equal(pendingAudioLimitBytes(undefined), 16000 * 4 * PENDING_AUDIO_SECONDS);
    assert.equal(pendingAudioLimitBytes(Number.NaN), 16000 * 4 * PENDING_AUDIO_SECONDS);
    assert.equal(pendingAudioLimitBytes(1_000_000), 48000 * 4 * PENDING_AUDIO_SECONDS);
    assert.equal(pendingAudioLimitBytes(100), 8000 * 4 * PENDING_AUDIO_SECONDS);
});
