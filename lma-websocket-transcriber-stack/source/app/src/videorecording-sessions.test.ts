/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * A meeting's video recording, per capture session (videorecording.ts). A
 * continued meeting's later session streams video under the same callId while
 * the earlier session's video may still be muxing, so each session's video is
 * kept apart: its START_VIDEO opens a recording of its own, its audio WAV is
 * handed to its own video, and the ADD_S3_VIDEO_RECORDING_URL it writes names
 * the session. FFmpeg is replaced by a command that fails at once, so the mux
 * falls back to uploading the received stream; S3 and Kinesis are stubbed.
 */
import { strict as assert } from 'node:assert';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { test } from 'node:test';
import { FastifyInstance } from 'fastify';
import { KinesisClient } from '@aws-sdk/client-kinesis';
import { S3Client } from '@aws-sdk/client-s3';

const TMP = fs.mkdtempSync(path.join(os.tmpdir(), 'lma-video-sessions-'));
// Read by the modules at import, so set before they are loaded below.
process.env['LOCAL_TEMP_DIR'] = TMP + path.sep;
process.env['RECORDINGS_BUCKET_NAME'] = 'video-sessions-test-bucket';
process.env['ENABLE_VIDEO_RECORDING'] = 'true';
process.env['FFMPEG_PATH'] = 'false';

/* eslint-disable @typescript-eslint/no-require-imports */
const video = require('./videorecording') as typeof import('./videorecording');
/* eslint-enable @typescript-eslint/no-require-imports */

const quietServer = {
    log: { info: () => undefined, debug: () => undefined, error: () => undefined, warn: () => undefined },
} as unknown as FastifyInstance;

type Meta = Parameters<typeof video.startVideoSession>[0];
const meta = (callId: string, sessionNumber?: number): Meta =>
    ({ callId, videoTimeOffsetMs: 0, ...(sessionNumber === undefined ? {} : { sessionNumber }) }) as Meta;

/** A few bytes of "video", enough for the session to have something to mux. */
const CHUNK = new Uint8Array([0, 0, 0, 8, 102, 116, 121, 112]);

const stubAws = (t: { mock: { method: typeof test.mock.method } }): Array<Record<string, unknown>> => {
    const events: Array<Record<string, unknown>> = [];
    // Reads the body, as S3 does, before the upload is answered.
    t.mock.method(S3Client.prototype, 'send', async (command: { input: { Body?: AsyncIterable<unknown> } }) => {
        for await (const part of command.input.Body ?? []) {
            void part;
        }
        return {};
    });
    t.mock.method(KinesisClient.prototype, 'send', async (command: { input: { Data: Buffer } }) => {
        events.push(JSON.parse(Buffer.from(command.input.Data).toString('utf8')) as Record<string, unknown>);
        return {};
    });
    return events;
};

const videoEvents = (events: Array<Record<string, unknown>>): Array<Record<string, unknown>> =>
    events.filter((event) => event['EventType'] === 'ADD_S3_VIDEO_RECORDING_URL');

test.after(() => {
    fs.rmSync(TMP, { recursive: true, force: true });
});

test('a continued session\'s video recording names its session', async (t) => {
    const events = stubAws(t);
    const callId = 'video-session-named';
    const session = video.startVideoSession(meta(callId, 2), quietServer, 0);
    video.writeVideoChunk(session, CHUNK, quietServer);
    await video.endVideoSession(callId, quietServer, 2);
    video.notifyAudioRecordingDone(callId, undefined, quietServer, 2);
    await video.awaitPendingMuxes(quietServer, 5_000);

    const written = videoEvents(events);
    assert.equal(written.length, 1, 'one video recording link is written');
    assert.equal(written[0]?.['SessionNumber'], 2);
});

test('a first session\'s video recording names no session, as before', async (t) => {
    const events = stubAws(t);
    const callId = 'video-session-first';
    const session = video.startVideoSession(meta(callId), quietServer, 0);
    video.writeVideoChunk(session, CHUNK, quietServer);
    await video.endVideoSession(callId, quietServer);
    video.notifyAudioRecordingDone(callId, undefined, quietServer);
    await video.awaitPendingMuxes(quietServer, 5_000);

    const written = videoEvents(events);
    assert.equal(written.length, 1);
    assert.equal('SessionNumber' in (written[0] ?? {}), false);
});

test('a later session\'s video starts while the earlier session\'s video is still finalizing', async (t) => {
    const events = stubAws(t);
    const callId = 'video-session-overlap';

    // Session 1's video has ended; its audio side has not finished yet, so its
    // mux is still waiting.
    const first = video.startVideoSession(meta(callId, 1), quietServer, 0);
    video.writeVideoChunk(first, CHUNK, quietServer);
    await video.endVideoSession(callId, quietServer, 1);
    assert.equal(first.videoEnded, true);
    assert.equal(first.muxStarted, false);

    // The meeting is continued; session 2 starts streaming video.
    const second = video.startVideoSession(meta(callId, 2), quietServer, 0);
    assert.notEqual(second, first, 'session 2 has a video recording of its own');
    assert.equal(second.discarding, false, 'session 2\'s video is recorded, not discarded');
    video.writeVideoChunk(second, CHUNK, quietServer);

    // Session 1's audio finishes: its WAV goes to session 1's video only.
    video.notifyAudioRecordingDone(callId, undefined, quietServer, 1);
    await video.awaitPendingMuxes(quietServer, 5_000);
    assert.equal(second.muxStarted, false, 'session 1\'s audio does not finalize session 2\'s video');

    await video.endVideoSession(callId, quietServer, 2);
    video.notifyAudioRecordingDone(callId, undefined, quietServer, 2);
    await video.awaitPendingMuxes(quietServer, 5_000);

    const sessions = videoEvents(events).map((event) => event['SessionNumber']);
    assert.deepEqual(sessions, [1, 2], 'each session writes its own video recording link');
});

test('a withheld stream is accepted into a discarding session that keeps nothing', async (t) => {
    const events = stubAws(t);
    const callId = 'video-session-withheld';
    const session = video.startVideoSession(meta(callId), quietServer, 0, 0, 'DESKTOP_CAPTURE', true);
    assert.equal(session.discarding, true);
    video.writeVideoChunk(session, CHUNK, quietServer);
    assert.equal(session.fileSize, 0, 'no video byte is written');
    await video.endVideoSession(callId, quietServer);
    video.notifyAudioRecordingDone(callId, undefined, quietServer);
    await video.awaitPendingMuxes(quietServer, 5_000);
    assert.deepEqual(videoEvents(events), [], 'no video recording link is written');
});

test('a stream that is not withheld is recorded, as before', async (t) => {
    const events = stubAws(t);
    const callId = 'video-session-not-withheld';
    const session = video.startVideoSession(meta(callId), quietServer, 0, 0, 'DESKTOP_CAPTURE', false);
    assert.equal(session.discarding, false);
    video.writeVideoChunk(session, CHUNK, quietServer);
    await video.endVideoSession(callId, quietServer);
    video.notifyAudioRecordingDone(callId, undefined, quietServer);
    await video.awaitPendingMuxes(quietServer, 5_000);
    assert.equal(videoEvents(events).length, 1);
});
