/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';

import { isBrowserVideoClient, keyframeSourceFor, videoAllowedFor, videoStartedFrame } from './video-admission';

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
