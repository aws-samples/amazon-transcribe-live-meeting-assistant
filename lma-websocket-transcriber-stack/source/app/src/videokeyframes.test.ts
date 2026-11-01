/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Tests for the desktop video's key frame lane (videokeyframes.ts), driven
 * through its stdout/stderr handlers with synthetic samples, so no FFmpeg is
 * needed. Pinned here: a sample is judged only once its presentation time is
 * known, whichever pipe delivers first; the frame's two clocks; the images of
 * samples that are not kept are deleted; and a failure to save one frame does
 * not end the lane.
 */
import { strict as assert } from 'node:assert';
import fs from 'node:fs';
import path from 'node:path';
import { test } from 'node:test';
import { FastifyInstance } from 'fastify';
import {
    KeyframeLane,
    KeyframeLaneIo,
    SAMPLE_BYTES,
    frameIdFor,
    keyframeKeys,
    parseShowinfo,
} from './videokeyframes';
import { CallVideoKeyframeEvent } from './calleventdata/eventtypes';

const quietServer = {
    log: { info: () => undefined, debug: () => undefined, error: () => undefined, warn: () => undefined },
} as unknown as FastifyInstance;

const flat = (v: number) => Buffer.alloc(SAMPLE_BYTES, v);
const showinfo = (n: number, ptsSeconds: number, size = '1920x1080') =>
    `[Parsed_showinfo_0 @ 0x5581] n:${String(n).padStart(4)} pts:${Math.round(ptsSeconds * 12800)} ` +
    `pts_time:${ptsSeconds} duration:2560 pos:1234 fmt:yuv420p sar:1/1 s:${size} i:P iskey:1 type:I\n`;
const tick = () => new Promise((r) => setImmediate(r));

const harness = (videoOffsetBaseSeconds: number | null = 12.5) => {
    const emitted: CallVideoKeyframeEvent[] = [];
    const uploads: string[] = [];
    const removed: string[] = [];
    const io: KeyframeLaneIo = {
        readImage: async () => Buffer.from([0xff, 0xd8, 1, 2, 0xff, 0xd9]),
        upload: async (key) => {
            uploads.push(key);
        },
        emit: async (event) => {
            emitted.push(event);
        },
        remove: (file) => {
            removed.push(file);
        },
    };
    const lane = new KeyframeLane(
        {
            callId: 'Desk call 7',
            timelineBaseSeconds: 12.5,
            videoOffsetBaseSeconds,
            tokens: { accessToken: 'a' },
        },
        quietServer,
        io
    );
    return { lane, io, emitted, uploads, removed };
};

test('showinfo lines give the frame number, time and size', () => {
    assert.deepEqual(parseShowinfo(showinfo(3, 6.4, '1280x720')), { n: 3, ptsSeconds: 6.4, width: 1280, height: 720 });
    assert.equal(parseShowinfo('[Parsed_showinfo_0 @ 0x5581]   color_range:tv color_space:bt709'), null);
    assert.equal(parseShowinfo('frame=   10 fps=0.5 q=-0.0 size=N/A time=00:00:20.00'), null);
    // A frame without a time is still a frame, so the pairing stays in step.
    const noTime = parseShowinfo('[Parsed_showinfo_0 @ 0x1] n:   4 pts:NOPTS pts_time:NOPTS pos:1 fmt:yuv420p sar:1/1 s:640x360 i:P');
    assert.ok(noTime && Number.isNaN(noTime.ptsSeconds) && noTime.n === 4 && noTime.width === 640);
});

test('a settled picture is kept once, with both clocks offset from the stream start', async () => {
    const h = harness();
    for (let i = 0; i < 6; i++) {
        h.lane.onStdout(flat(200));
        h.lane.onStderr(showinfo(i, i * 2));
        await tick();
        await tick();
    }
    assert.equal(h.emitted.length, 1);
    const e = h.emitted[0];
    // First seen at pts 0: 12.5 s into the meeting and into the recording.
    assert.equal(e.Timestamp, 12.5);
    assert.equal(e.VideoOffset, 12.5);
    assert.equal(e.Source, 'DESKTOP_CAPTURE');
    assert.equal(e.ShareDetected, null);
    assert.equal(e.Width, 1568);
    assert.equal(e.Height, 882);
    assert.equal(e.AccessToken, 'a');
    assert.deepEqual(h.uploads.sort(), [
        `lma-video-keyframes/Desk_call_7/${e.FrameId}.jpg`,
        `lma-video-keyframes/Desk_call_7/${e.FrameId}_thumb.jpg`,
    ]);
    // Every sample's images are deleted once judged, the kept one's after upload.
    for (let i = 1; i <= 6; i++) {
        const stem = String(i).padStart(6, '0');
        assert.ok(h.removed.some((f) => f.endsWith(`${stem}.jpg`)), `sample ${i} image removed`);
        assert.ok(h.removed.some((f) => f.endsWith(`${stem}_t.jpg`)), `sample ${i} thumbnail removed`);
    }
});

test('images written after their sample was judged are removed by a later sample', async () => {
    const h = harness();
    // A remove that, like unlink, does nothing for a file that is not there yet.
    h.io.remove = (file) => {
        if (fs.existsSync(file)) {
            fs.unlinkSync(file);
        }
    };
    fs.mkdirSync(h.lane.dir, { recursive: true });
    try {
        let v = 0;
        for (let i = 0; i < 40; i++) {
            // A moving picture, so nothing is kept and every image is scratch.
            h.lane.onStdout(flat((v += 37) % 256));
            h.lane.onStderr(showinfo(i, i * 2));
            // FFmpeg's write of this sample's images lands after it was judged.
            const stem = path.join(h.lane.dir, String(i + 1).padStart(6, '0'));
            fs.writeFileSync(`${stem}.jpg`, 'x');
            fs.writeFileSync(`${stem}_t.jpg`, 'x');
            await tick();
        }
        assert.equal(h.emitted.length, 0);
        // Only the newest sample's late images remain, until the next sample.
        assert.deepEqual(fs.readdirSync(h.lane.dir).sort(), ['000040.jpg', '000040_t.jpg']);
    } finally {
        fs.rmSync(h.lane.dir, { recursive: true, force: true });
    }
});

test('samples wait for their time, whichever pipe delivers first', async () => {
    const h = harness();
    // All the samples first, split across chunk boundaries, then the times.
    const all = Buffer.concat([flat(90), flat(90), flat(90), flat(90), flat(90)]);
    h.lane.onStdout(all.subarray(0, 1000));
    h.lane.onStdout(all.subarray(1000));
    assert.equal(h.emitted.length, 0);
    h.lane.onStderr(showinfo(0, 0) + showinfo(1, 2).slice(0, 20));
    h.lane.onStderr(showinfo(1, 2).slice(20) + showinfo(2, 4) + showinfo(3, 6) + showinfo(4, 8));
    await tick();
    await tick();
    assert.equal(h.emitted.length, 1);
});

test('a restarted stream has a place on the transcript clock but not in the recording', async () => {
    const h = harness(null);
    for (let i = 0; i < 6; i++) {
        h.lane.onStdout(flat(30));
        h.lane.onStderr(showinfo(i, 10 + i * 2));
        await tick();
    }
    await tick();
    assert.equal(h.emitted.length, 1);
    assert.equal(h.emitted[0].Timestamp, 22.5);
    assert.equal(h.emitted[0].VideoOffset, null);
});

test('a frame whose images never appear is announced once they do, while it stays on screen', async () => {
    const h = harness();
    let images = false;
    h.io.readImage = async () => (images ? Buffer.from([0xff, 0xd8, 0xff, 0xd9, 0xff, 0xd9]) : null);
    let pts = 0;
    const feed = async (v: number, count: number) => {
        for (let i = 0; i < count; i++) {
            h.lane.onStdout(flat(v));
            h.lane.onStderr(showinfo(0, (pts += 2)));
            await tick();
            await tick();
        }
    };
    await feed(200, 5);
    assert.equal(h.emitted.length, 0);
    images = true;
    // The same picture, still on screen: not taken as already captured.
    await feed(200, 4);
    assert.equal(h.emitted.length, 1);
});

test('a frame whose event could not be written is announced again while it stays on screen', async () => {
    const h = harness();
    let fail = true;
    h.io.emit = async (event) => {
        if (fail) {
            throw new Error('ProvisionedThroughputExceededException');
        }
        h.emitted.push(event);
    };
    let pts = 0;
    const feed = async (count: number) => {
        for (let i = 0; i < count; i++) {
            h.lane.onStdout(flat(200));
            h.lane.onStderr(showinfo(0, (pts += 2)));
            await tick();
            await tick();
        }
    };
    await feed(4);
    assert.equal(h.emitted.length, 0);
    fail = false;
    await feed(4);
    assert.equal(h.emitted.length, 1);
    assert.equal(h.lane.framesEmitted, 1, 'only the frame that was announced is counted');
});

test('ids sort in time order and keys follow the deletion prefix rule', () => {
    assert.ok(frameIdFor(9.5, 'a') < frameIdFor(10, 'a'));
    assert.equal(frameIdFor(-1, 'x'), '0000000000-x');
    assert.deepEqual(keyframeKeys('__m 1__', 'f'), {
        image: 'lma-video-keyframes/m_1/f.jpg',
        thumbnail: 'lma-video-keyframes/m_1/f_thumb.jpg',
    });
});
