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
    routeBinaryFrame,
    videoAdmission,
    videoAllowed,
    videoStartedFrame,
} from './video-admission';

test('an accepted video stream is acknowledged with its callId', () => {
    assert.deepEqual(videoStartedFrame('Weekly - 1'), { event: 'VIDEO_STARTED', callId: 'Weekly - 1' });
});

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
    const startVideo = INDEX.slice(INDEX.indexOf('const admission = videoAdmission('));
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

test('a binary frame goes to the video session, is held behind START_VIDEO, or is audio', () => {
    const pending = new PendingVideoChunks<string>(4);
    const chunk = Uint8Array.of(1, 2, 3);
    assert.equal(routeBinaryFrame('ws', chunk, true, pending), 'video');
    assert.equal(routeBinaryFrame('ws', chunk, false, pending), 'audio');
    pending.begin('ws');
    assert.equal(routeBinaryFrame('ws', chunk, false, pending), 'held');
    assert.equal(routeBinaryFrame('ws', chunk, false, pending), 'dropped');
    // A session wins over a START_VIDEO still recorded as in progress.
    assert.equal(routeBinaryFrame('ws', chunk, true, pending), 'video');
    assert.deepEqual(pending.release('ws'), [chunk]);
    assert.equal(routeBinaryFrame('ws', chunk, false, pending), 'audio');
});

// index.ts registers the server's handlers when it is imported, so its wiring
// of the held chunks is checked in its source: each step of the path is where
// the comments in index.ts say it is.
const INDEX = readFileSync(path.resolve(__dirname, '../src/index.ts'), 'utf8');

const body = (start: RegExp, end: RegExp): string => {
    const from = INDEX.search(start);
    assert.ok(from >= 0, `index.ts has no ${start}`);
    const rest = INDEX.slice(from);
    const to = rest.slice(1).search(end);
    return to >= 0 ? rest.slice(0, to + 1) : rest;
};

test('index.ts holds the chunks behind START_VIDEO from before its first await', () => {
    const handler = body(/const onTextMessage = async/, /\nconst [a-zA-Z]+ = /);
    const begin = handler.indexOf('pendingVideoChunks.begin(ws)');
    assert.ok(begin >= 0, 'START_VIDEO does not begin holding chunks');
    assert.ok(begin < handler.indexOf('await '), 'chunks are held only after the handler first awaits');
});

test('index.ts writes the held chunks once START_VIDEO has its session, before any later chunk', () => {
    const start = INDEX.indexOf('const session = startVideoSession(');
    const release = INDEX.indexOf('pendingVideoChunks.release(ws)');
    assert.ok(start >= 0 && release > start, 'the held chunks are not released after the session starts');
    const between = INDEX.slice(start, release);
    assert.ok(between.includes('videoSocketMap.set(ws, session)'), 'the session is not registered before the release');
    assert.ok(!between.includes('await '), 'a later chunk could overtake the held ones');
});

test('index.ts routes every binary frame through routeBinaryFrame', () => {
    const handler = body(/const onBinaryMessage = async/, /\nconst [a-zA-Z]+ = /);
    assert.ok(handler.includes('routeBinaryFrame(ws, data,'), 'onBinaryMessage does not route through routeBinaryFrame');
    assert.ok(!handler.includes('pendingVideoChunks.hold('), 'onBinaryMessage holds chunks outside routeBinaryFrame');
});

test('index.ts forgets the held chunks on every refusal, on close and on a connection error', () => {
    const startVideo = INDEX.slice(INDEX.indexOf('callMetaData.callEvent === \'START_VIDEO\''), INDEX.indexOf('const session = startVideoSession('));
    const refusals = startVideo.split('ws.close(').length - 1;
    const drops = startVideo.split('pendingVideoChunks.drop(ws)').length - 1;
    assert.ok(refusals > 0 && drops >= refusals, `${refusals} refusals of START_VIDEO but ${drops} drops`);
    assert.ok(body(/const onWsClose = async/, /\nconst [a-zA-Z]+ = /).includes('pendingVideoChunks.drop(ws)'), 'close keeps them');
    assert.ok(body(/function onWsErrorEvent/, /\n {4}}\n/).includes('pendingVideoChunks.drop(ws)'), 'an error keeps them');
});
