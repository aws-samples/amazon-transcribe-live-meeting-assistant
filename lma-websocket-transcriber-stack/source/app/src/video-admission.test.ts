/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import path from 'node:path';

import {
    PendingVideoChunks,
    keyframeSourceFor,
    videoAdmission,
    videoAllowed,
    videoStartedFrame,
} from './video-admission';

test('an accepted video stream is acknowledged with its callId', () => {
    assert.deepEqual(videoStartedFrame('Weekly - 1'), { event: 'VIDEO_STARTED', callId: 'Weekly - 1' });
});

const INDEX_SOURCE = readFileSync(path.resolve(__dirname, '../src/index.ts'), 'utf8');

const ON = { stackRecordsVideo: true, recordScreenVideo: true };
const SETTING_OFF = { stackRecordsVideo: true, recordScreenVideo: false };
const STACK_OFF = { stackRecordsVideo: false, recordScreenVideo: true };

test('screen video is recorded only when the stack and the setting both allow it', () => {
    assert.equal(videoAllowed(ON), true);
    assert.equal(videoAllowed(SETTING_OFF), false);
    assert.equal(videoAllowed(STACK_OFF), false);
    assert.equal(videoAllowed({ stackRecordsVideo: false, recordScreenVideo: false }), false);
});

test('a client that handles a refusal is refused with the setting off, and recorded with it on', () => {
    assert.equal(videoAdmission(ON, true), 'record');
    assert.equal(videoAdmission(SETTING_OFF, true), 'refuse');
});

test('a client that does not declare it handles a refusal is withheld, not refused, with the setting off', () => {
    // Accepted and discarded: an earlier desktop build reconnects after every
    // close, so a refusal would bring it straight back.
    assert.equal(videoAdmission(SETTING_OFF, false), 'withhold');
    assert.equal(videoAdmission(ON, false), 'record');
});

test('with the stack off every START_VIDEO is discarded, whatever the setting or the client', () => {
    for (const handlesRefusal of [true, false]) {
        assert.equal(videoAdmission(STACK_OFF, handlesRefusal), 'discard');
        assert.equal(videoAdmission({ stackRecordsVideo: false, recordScreenVideo: false }, handlesRefusal), 'discard');
    }
});

test('index.ts reads handlesRefusal from START_VIDEO and withholds a stream into a discarding session', () => {
    const startVideo = INDEX_SOURCE.slice(INDEX_SOURCE.indexOf('const admission = videoAdmission('));
    assert.ok(startVideo.length > 0, 'START_VIDEO does not decide its admission with videoAdmission');
    assert.ok(/callMetaData\.handlesRefusal === true/.test(startVideo.slice(0, 600)), 'handlesRefusal is not read');
    assert.ok(/admission === 'withhold'/.test(startVideo), 'a withheld stream is not handled');
    assert.ok(startVideo.includes('noteScreenVideoWithheld('), 'a withheld stream is not counted');
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
