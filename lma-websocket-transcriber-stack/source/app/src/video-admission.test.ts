/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';

import {
    PendingVideoChunks,
    isBrowserVideoClient,
    keyframeSourceFor,
    videoAllowedFor,
    videoStartedFrame,
} from './video-admission';

test('an accepted video stream is acknowledged with its callId', () => {
    assert.deepEqual(videoStartedFrame('Weekly - 1'), { event: 'VIDEO_STARTED', callId: 'Weekly - 1' });
});

const ON = { stackRecordsVideo: true, recordBrowserVideo: true };
const SETTING_OFF = { stackRecordsVideo: true, recordBrowserVideo: false };
const STACK_OFF = { stackRecordsVideo: false, recordBrowserVideo: true };

test('the two browser clients are the ones the Meeting Recording setting governs', () => {
    assert.equal(isBrowserVideoClient('STREAM_AUDIO'), true);
    assert.equal(isBrowserVideoClient('BROWSER_EXTENSION'), true);
    assert.equal(isBrowserVideoClient('DESKTOP_APP'), false);
    assert.equal(isBrowserVideoClient('API'), false);
    assert.equal(isBrowserVideoClient(undefined), false);
});

test('browser clients send video only when the stack and the setting both allow it', () => {
    for (const source of ['STREAM_AUDIO', 'BROWSER_EXTENSION'] as const) {
        assert.equal(videoAllowedFor(source, ON), true, source);
        assert.equal(videoAllowedFor(source, SETTING_OFF), false, source);
        assert.equal(videoAllowedFor(source, STACK_OFF), false, source);
    }
});

test('the desktop app and the streaming API keep following the stack switch alone', () => {
    for (const source of ['DESKTOP_APP', 'API', undefined] as const) {
        assert.equal(videoAllowedFor(source, SETTING_OFF), true, String(source));
        assert.equal(videoAllowedFor(source, STACK_OFF), false, String(source));
    }
});

test('key frames are recorded under the client that streamed the video', () => {
    assert.equal(keyframeSourceFor('STREAM_AUDIO'), 'STREAM_AUDIO');
    assert.equal(keyframeSourceFor('BROWSER_EXTENSION'), 'BROWSER_EXTENSION');
    assert.equal(keyframeSourceFor('DESKTOP_APP'), 'DESKTOP_CAPTURE');
    assert.equal(keyframeSourceFor('API'), 'DESKTOP_CAPTURE');
    assert.equal(keyframeSourceFor(undefined), 'DESKTOP_CAPTURE');
});

test('chunks behind a START_VIDEO in progress are held and released in arrival order', () => {
    const pending = new PendingVideoChunks<string>();
    pending.begin('ws');
    const init = Uint8Array.of(1, 2, 3);
    const fragment = Uint8Array.of(4, 5);
    assert.equal(pending.hold('ws', init), 'held');
    assert.equal(pending.hold('ws', fragment), 'held');
    assert.deepEqual(pending.release('ws'), [init, fragment]);
    assert.equal(pending.hold('ws', fragment), 'no-start');
    assert.deepEqual(pending.release('ws'), []);
});

test('a connection with no START_VIDEO in progress holds nothing', () => {
    const pending = new PendingVideoChunks<string>();
    assert.equal(pending.hold('ws', Uint8Array.of(1)), 'no-start');
});

test('held chunks are kept per connection and stop at the byte limit', () => {
    const pending = new PendingVideoChunks<string>(4);
    pending.begin('a');
    pending.begin('b');
    assert.equal(pending.hold('a', Uint8Array.of(1, 2, 3)), 'held');
    assert.equal(pending.hold('a', Uint8Array.of(4, 5)), 'full');
    assert.equal(pending.hold('b', Uint8Array.of(9)), 'held');
    assert.deepEqual(pending.release('a'), [Uint8Array.of(1, 2, 3)]);
    assert.deepEqual(pending.release('b'), [Uint8Array.of(9)]);
});

test('a refused START_VIDEO forgets the chunks held for it', () => {
    const pending = new PendingVideoChunks<string>();
    pending.begin('ws');
    pending.hold('ws', Uint8Array.of(1));
    pending.drop('ws');
    assert.deepEqual(pending.release('ws'), []);
    assert.equal(pending.hold('ws', Uint8Array.of(2)), 'no-start');
});
