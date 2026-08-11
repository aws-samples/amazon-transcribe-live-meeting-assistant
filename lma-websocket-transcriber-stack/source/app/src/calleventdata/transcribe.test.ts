/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import assert from 'node:assert/strict';
import test from 'node:test';

import { MAX_AUDIO_CHUNK_BYTES, frameAudioChunk } from './transcribe';

test('the frame cap matches the Virtual Participant scribe', () => {
    assert.equal(MAX_AUDIO_CHUNK_BYTES, 16 * 1024);
});

test('a chunk within the limit is passed through without copying', () => {
    const chunk = Buffer.alloc(MAX_AUDIO_CHUNK_BYTES, 1);
    const frames = frameAudioChunk(chunk);

    assert.equal(frames.length, 1);
    assert.equal(frames[0], chunk);
});

test('an oversized chunk is split into frames within the limit', () => {
    // A MicroVM ASR acquisition delays the Transcribe consumer by seconds, so the
    // first read can carry the whole backlog: 20s of 16 kHz mono PCM16 here.
    const chunk = Buffer.alloc(16000 * 2 * 20, 9);
    const frames = frameAudioChunk(chunk);

    assert.ok(frames.length > 1);
    for (const frame of frames) {
        assert.ok(
            frame.length <= MAX_AUDIO_CHUNK_BYTES,
            `frame of ${frame.length} bytes exceeds the limit`
        );
    }
});

test('splitting loses no audio and preserves order', () => {
    const chunk = Buffer.alloc(MAX_AUDIO_CHUNK_BYTES * 3 + 517);
    for (let i = 0; i < chunk.length; i += 1) {
        chunk[i] = i % 251;
    }

    const rejoined = Buffer.concat(frameAudioChunk(chunk));

    assert.equal(rejoined.length, chunk.length);
    assert.ok(rejoined.equals(chunk));
});

test('an empty chunk yields one empty frame rather than nothing', () => {
    assert.deepEqual(frameAudioChunk(Buffer.alloc(0)), [Buffer.alloc(0)]);
});
