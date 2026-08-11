/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Regression tests for GitHub #536.
 *
 * A live Zoom meeting stopped transcribing 75 seconds in and never recovered.
 * ffmpeg handed over an audio buffer larger than Amazon Transcribe accepts, and
 * the resulting BadRequestException matched the "non-retryable configuration
 * error" predicate — so the retry loop broke out permanently while the VP stayed
 * ACTIVE, showing no sign of trouble in the UI.
 */
import { strict as assert } from 'node:assert';
import { test } from 'node:test';
import {
    frameAudioChunk,
    isTransientFramingError,
    MAX_AUDIO_CHUNK_BYTES,
} from './scribe.js';

test('the frame cap is well under the service limit but still a useful size', () => {
    // 16 KB = 512 ms at 16 kHz mono PCM16. Small enough to be safely accepted,
    // large enough that per-frame overhead stays negligible.
    assert.equal(MAX_AUDIO_CHUNK_BYTES, 16 * 1024);
});

test('a buffer within the limit is passed through unchanged and uncopied', () => {
    // The common case must cost nothing: same object identity, not a copy.
    const chunk = Buffer.alloc(1024, 7);
    const frames = frameAudioChunk(chunk);
    assert.equal(frames.length, 1);
    assert.equal(frames[0], chunk);
});

test('a buffer exactly at the limit is not split', () => {
    const chunk = Buffer.alloc(MAX_AUDIO_CHUNK_BYTES, 1);
    assert.equal(frameAudioChunk(chunk).length, 1);
});

test('an oversized buffer is split into frames within the limit', () => {
    const chunk = Buffer.alloc(MAX_AUDIO_CHUNK_BYTES * 3 + 123, 9);
    const frames = frameAudioChunk(chunk);
    assert.equal(frames.length, 4);
    for (const frame of frames) {
        assert.ok(
            frame.length <= MAX_AUDIO_CHUNK_BYTES,
            `frame of ${frame.length} exceeds the cap`,
        );
    }
});

test('splitting loses no audio and preserves byte order', () => {
    // This is the property that matters: dropping the overflow would silently
    // lose speech, and reordering would corrupt the transcript.
    const chunk = Buffer.alloc(MAX_AUDIO_CHUNK_BYTES * 2 + 500);
    for (let i = 0; i < chunk.length; i += 1) chunk[i] = i % 251;
    const rejoined = Buffer.concat(frameAudioChunk(chunk));
    assert.equal(rejoined.length, chunk.length, 'no bytes may be dropped');
    assert.ok(rejoined.equals(chunk), 'bytes must be forwarded in order');
});

test('an empty buffer yields a single empty frame rather than nothing', () => {
    assert.deepEqual(frameAudioChunk(Buffer.alloc(0)).length, 1);
});

test('the exact Transcribe message from the incident is treated as transient', () => {
    // Verbatim from the failing meeting's logs.
    assert.equal(
        isTransientFramingError({
            message: 'Your stream is too big. Reduce the frame size and try your request again.',
        }),
        true,
    );
});

test('framing-error detection is case-insensitive and matches either phrase', () => {
    assert.equal(isTransientFramingError({ message: 'STREAM IS TOO BIG' }), true);
    assert.equal(isTransientFramingError({ message: 'please Reduce The Frame Size' }), true);
});

test('genuine configuration errors are NOT treated as transient', () => {
    // These must keep aborting: retrying a bad language configuration would spin
    // for the whole meeting without ever succeeding.
    for (const message of [
        'The language code provided is not valid',
        'LanguageOptions must contain at least two languages',
        'validation error detected: Value null at LanguageCode failed to satisfy constraint',
        '',
    ]) {
        assert.equal(
            isTransientFramingError({ message }),
            false,
            `"${message}" must not be classified as transient`,
        );
    }
});

test('a missing or malformed error object does not throw', () => {
    assert.equal(isTransientFramingError({}), false);
    assert.equal(isTransientFramingError(undefined as unknown as { message: string }), false);
});
