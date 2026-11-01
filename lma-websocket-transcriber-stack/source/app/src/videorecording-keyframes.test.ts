/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * How a video session starts its key frame lane (videorecording.ts): the first
 * stream at the client's offset on both clocks, a resumed stream on the same
 * lane, and a fresh restart on a new lane placed by wall-clock time with no
 * recording position. FFmpeg is replaced by a command that exits at once, so
 * no decoding happens here; videokeyframes.test.ts covers the lane itself.
 */
import { strict as assert } from 'node:assert';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { test } from 'node:test';
import { FastifyInstance } from 'fastify';

const TMP = fs.mkdtempSync(path.join(os.tmpdir(), 'lma-kf-wiring-'));
// Read by the modules at import, so set before they are loaded below.
process.env['LOCAL_TEMP_DIR'] = TMP + path.sep;
process.env['RECORDINGS_BUCKET_NAME'] = 'wiring-test-bucket';
process.env['ENABLE_VIDEO_RECORDING'] = 'true';
process.env['FFMPEG_PATH'] = process.platform === 'win32' ? 'cmd' : 'true';

// eslint-disable-next-line @typescript-eslint/no-require-imports
const { startVideoSession, endVideoSession } = require('./videorecording') as typeof import('./videorecording');

const quietServer = {
    log: { info: () => undefined, debug: () => undefined, error: () => undefined, warn: () => undefined },
} as unknown as FastifyInstance;

type LaneOptions = { timelineBaseSeconds: number, videoOffsetBaseSeconds: number | null };
const optionsOf = (lane: unknown): LaneOptions => (lane as { options: LaneOptions }).options;

test('first stream, resumed stream and fresh restart each get the lane they need', async () => {
    const audioStartedAtMs = Date.now() - 60_000;
    const meta = { callId: 'wiring-call', videoTimeOffsetMs: 2500 } as Parameters<typeof startVideoSession>[0];

    const first = startVideoSession(meta, quietServer, audioStartedAtMs);
    const lane1 = first.keyframes;
    assert.ok(lane1, 'the first stream has a lane');
    assert.deepEqual(optionsOf(lane1), { ...optionsOf(lane1), timelineBaseSeconds: 2.5, videoOffsetBaseSeconds: 2.5 });

    const resumed = startVideoSession({ ...meta, videoResume: true }, quietServer, audioStartedAtMs);
    assert.equal(resumed.keyframes, lane1, 'a resumed stream keeps feeding the same lane');

    const restarted = startVideoSession({ ...meta, videoResume: false }, quietServer, audioStartedAtMs);
    const lane2 = restarted.keyframes;
    assert.ok(lane2 && lane2 !== lane1, 'a fresh encoder session gets a new lane');
    const { timelineBaseSeconds, videoOffsetBaseSeconds } = optionsOf(lane2);
    assert.equal(videoOffsetBaseSeconds, null, 'its place in the recording is not known');
    assert.ok(timelineBaseSeconds >= 59 && timelineBaseSeconds <= 70, `placed by wall clock (${timelineBaseSeconds})`);

    await endVideoSession('wiring-call', quietServer);
    assert.equal(restarted.keyframes, undefined, 'ending the video stops its lane');
    fs.rmSync(TMP, { recursive: true, force: true });
});

test('the detector is the same code as the Virtual Participant\'s', () => {
    // Compared without comments, whitespace, braces and semicolons: the copy
    // differs only in its header and in the braces this package's lint adds.
    const vp = path.resolve(__dirname, '../../../../lma-virtual-participant-stack/backend/src/keyframe-detector.ts');
    const here = path.resolve(__dirname, '../src/keyframe-detector.ts');
    if (!fs.existsSync(vp) || !fs.existsSync(here)) {
        return; // not in a full checkout
    }
    const normalize = (file: string) =>
        fs.readFileSync(file, 'utf8')
            .replace(/\/\*[\s\S]*?\*\//g, '')
            .replace(/\/\/.*$/gm, '')
            .replace(/[\s{};]/g, '');
    assert.equal(normalize(here), normalize(vp), 'keyframe-detector.ts has drifted from the VP copy; run the same change in both');
});
