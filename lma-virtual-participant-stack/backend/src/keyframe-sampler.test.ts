/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import assert from 'node:assert/strict';
import test from 'node:test';
import {
    KeyframeSampler,
    KeyframeSamplerIo,
    KeyframeSamplerOptions,
    SAMPLE_BYTES,
    frameIdFor,
    keyframeKeys,
    posixifyFilename,
} from './keyframe-sampler.js';
import { screenShareProbeFor } from './screen-share-probe.js';
import type { VideoKeyframeEvent } from './kinesis-stream.js';

const flat = (v: number) => new Uint8Array(SAMPLE_BYTES).fill(v);
const tick = () => new Promise((r) => setImmediate(r));

function harness(overrides: Partial<KeyframeSamplerOptions> = {}) {
    let now = 100_000;
    const uploads: string[] = [];
    const frames: VideoKeyframeEvent[] = [];
    const io: KeyframeSamplerIo = {
        now: () => now,
        grab: async () => ({ image: Buffer.from('big'), thumbnail: Buffer.from('small'), width: 1568, height: 882 }),
        upload: async (_bucket, key) => {
            uploads.push(key);
        },
    };
    const sampler = new KeyframeSampler(io);
    const options: KeyframeSamplerOptions = {
        callId: 'Weekly sync - 2026-11-01',
        bucket: 'recordings',
        isLive: () => true,
        // The transcript clock runs 30 s behind wall-clock time from 100 s.
        timelineSeconds: () => (now - 100_000) / 1000 + 30,
        recordingStartedAtMs: () => 90_000,
        emit: async (frame) => {
            frames.push(frame);
        },
        ...overrides,
    };
    // start() also spawns FFmpeg; tests drive handleSample directly instead.
    (sampler as any).options = options;
    (sampler as any).stopped = false;
    const feed = async (sample: Uint8Array, count: number) => {
        for (let i = 0; i < count; i++) {
            sampler.handleSample(sample);
            await tick();
            await tick();
            now += 1000;
        }
    };
    return { sampler, uploads, frames, feed, setShare: (v: boolean | null) => ((sampler as any).shareOnScreen = v) };
}

test('a settled picture becomes one key frame, with both images uploaded and both clocks stamped', async () => {
    const h = harness();
    await h.feed(flat(200), 6);
    assert.equal(h.frames.length, 1);
    const f = h.frames[0];
    // First seen at wall 100 s: timeline 30 s, 10 s into a recording that began at 90 s.
    assert.equal(f.Timestamp, 30);
    assert.equal(f.VideoOffset, 10);
    assert.equal(f.Source, 'VIRTUAL_PARTICIPANT');
    assert.equal(f.ShareDetected, null, 'no probe: the platform gave no share signal');
    assert.match(f.FrameId, /^0000030000-[0-9a-f]{6}$/);
    assert.deepEqual(h.uploads.sort(), [
        `lma-video-keyframes/Weekly_sync___2026_11_01/${f.FrameId}.jpg`,
        `lma-video-keyframes/Weekly_sync___2026_11_01/${f.FrameId}_thumb.jpg`,
    ]);
    assert.equal(
        f.ThumbnailUrl,
        `https://recordings.s3.${process.env.AWS_REGION || 'us-east-1'}.amazonaws.com/${encodeURIComponent(`lma-video-keyframes/Weekly_sync___2026_11_01/${f.FrameId}_thumb.jpg`)}`,
    );
});

test('nothing is judged while the meeting is not being transcribed', async () => {
    let live = false;
    const h = harness({ isLive: () => live });
    await h.feed(flat(200), 10);
    assert.equal(h.frames.length, 0);
    live = true;
    await h.feed(flat(200), 4);
    assert.equal(h.frames.length, 1, 'judged from when it went live, settling from scratch');
});

test('with a share probe, samples are judged only while a share is on screen', async () => {
    const h = harness({ shareActive: async () => true });
    h.setShare(false);
    await h.feed(flat(200), 10);
    assert.equal(h.frames.length, 0);
    h.setShare(true);
    await h.feed(flat(200), 4);
    assert.equal(h.frames.length, 1);
    assert.equal(h.frames[0].ShareDetected, true);
});

test('no recording means no video offset', async () => {
    const h = harness({ recordingStartedAtMs: () => null });
    await h.feed(flat(200), 4);
    assert.equal(h.frames[0].VideoOffset, null);
});

test('a frame that could not be saved is captured again while it stays on screen', async () => {
    const h = harness();
    let fail = true;
    (h.sampler as any).io.upload = async () => {
        if (fail) throw new Error('throttled');
    };
    await h.feed(flat(200), 4);
    assert.equal(h.frames.length, 0);
    fail = false;
    // The same picture, still on screen: not taken as already captured.
    await h.feed(flat(200), 4);
    assert.equal(h.frames.length, 1);
});

test('a frame whose event could not be written is captured again', async () => {
    let fail = true;
    const h = harness({
        emit: async (frame) => {
            if (fail) throw new Error('ProvisionedThroughputExceededException');
            h.frames.push(frame);
        },
    });
    await h.feed(flat(200), 4);
    assert.equal(h.frames.length, 0);
    fail = false;
    await h.feed(flat(200), 4);
    assert.equal(h.frames.length, 1);
    assert.equal(h.sampler.framesEmitted, 1, 'only the frame that was announced is counted');
});

test('ids sort in time order and keys follow the deletion prefix rule', () => {
    assert.ok(frameIdFor(9.5, 'a') < frameIdFor(10, 'a'));
    assert.ok(frameIdFor(10, 'a') < frameIdFor(100, 'a'));
    assert.equal(frameIdFor(-3, 'x'), '0000000000-x');
    assert.equal(posixifyFilename('__a b/c.d__'), 'a_b_c.d');
    assert.deepEqual(keyframeKeys('m 1', 'f'), {
        image: 'lma-video-keyframes/m_1/f.jpg',
        thumbnail: 'lma-video-keyframes/m_1/f_thumb.jpg',
    });
});

test('Zoom and Teams have a share probe; Webex and Chime do not', () => {
    assert.ok(screenShareProbeFor('ZOOM'));
    assert.ok(screenShareProbeFor('TEAMS', 'dom'));
    assert.ok(screenShareProbeFor('Teams', 'sdk'));
    assert.equal(screenShareProbeFor('WEBEX'), null);
    assert.equal(screenShareProbeFor('CHIME'), null);
});
