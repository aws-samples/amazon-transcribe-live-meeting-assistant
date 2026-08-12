/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Tests for the outbound-audio classifier used to diagnose GitHub #543.
 *
 * These pin the thresholds that decide whether a live session's audio is leaving
 * cleanly. That matters twice over: three previous fixes were shipped on reasoning
 * rather than measurement (one of them broke transcription), and then the FIRST
 * version of this classifier produced false alarms that would have justified a
 * fourth wrong fix. The numbers in these tests are taken verbatim from the live
 * Teams capture so that specific misreading cannot come back.
 */
import { strict as assert } from 'node:assert';
import { test } from 'node:test';
import {
    classifyAudioWindow,
    EXPECTED_PACKETS_PER_SECOND,
    MIN_CAPTURE_RATIO,
    SPEECH_ENERGY_THRESHOLD,
    STATS_INTERVAL_MS,
} from './audio-diagnostics.js';

/** A window with no audio energy: the sender is deliberately silent. */
const silent = { seconds: 1, packets: 5, captureRatio: 1.0, energyDelta: 0 };
/** A window mid-utterance on a healthy sender. */
const speaking = { seconds: 1, packets: 50, captureRatio: 1.0, energyDelta: 0.02 };

test('the expected cadence matches Opus at a 20ms frame', () => {
    // 1000ms / 20ms = 50 packets per second.
    assert.equal(EXPECTED_PACKETS_PER_SECOND, 50);
});

test('DTX comfort noise during silence is NOT a problem', () => {
    // Regression for the false alarm that filled the first live capture: 4.8
    // packets/s for ten minutes, every line reported "encoder or capture starving".
    // A DTX sender that has nothing to say is healthy, and drowning the log in
    // this hid whether anything real was happening.
    for (const packets of [4, 5, 6]) {
        const v = classifyAudioWindow({ ...silent, packets });
        assert.equal(v.state, 'idle', `${packets} pkt/s of comfort noise should read idle`);
        assert.match(v.reason, /silent/);
    }
});

test('a short utterance averaged over a long window is not misread as frame loss', () => {
    // The other artefact: sampling every 5s, a ~2.5s utterance at a perfect 50/s
    // reads as ~25/s. The old code called that "capture starving" — indistinguishable
    // from genuinely dropping half the frames, which was the question being asked.
    // At a 1s window the same sender is judged on capture continuity instead.
    const v = classifyAudioWindow({ seconds: 5, packets: 125, captureRatio: 1.0, energyDelta: 0.02 });
    assert.notEqual(v.state, 'problem');
});

test('capture starvation is flagged from the capture ratio alone', () => {
    // The signature actually being hunted: Chromium losing audio before the encoder
    // sees it. Independent of speech, DTX and window alignment.
    const v = classifyAudioWindow({ ...speaking, captureRatio: 0.6 });
    assert.equal(v.state, 'problem');
    assert.match(v.reason, /capture starving/);
});

test('capture starvation is flagged even while silent', () => {
    // Capture runs whether or not the agent is speaking, so a gap during silence is
    // still a real fault — and it is the cheapest window in which to notice one.
    const v = classifyAudioWindow({ ...silent, captureRatio: 0.5 });
    assert.equal(v.state, 'problem');
    assert.match(v.reason, /capture starving/);
});

test('a healthy capture ratio is accepted with its normal jitter', () => {
    // The stat is a float differenced across an unaligned window, so it will never
    // read exactly 1.0; a tight bound here would recreate the false-alarm problem.
    for (const ratio of [MIN_CAPTURE_RATIO, 0.99, 1.0, 1.01]) {
        assert.notEqual(
            classifyAudioWindow({ ...speaking, captureRatio: ratio }).state,
            'problem',
            `${ratio} should be treated as healthy`,
        );
    }
});

test('far-end packet loss is reported as a wire fault, not a capture fault', () => {
    // Crackle from loss on the wire and crackle from a starved encoder need
    // opposite fixes, so the two must never collapse into one message.
    const v = classifyAudioWindow({ ...speaking, remotePacketsLost: 3 });
    assert.equal(v.state, 'problem');
    assert.match(v.reason, /wire/);
});

test('capture starvation outranks far-end loss', () => {
    // If capture is already losing frames, the far end will also report loss; the
    // upstream cause is the actionable one.
    const v = classifyAudioWindow({ ...speaking, captureRatio: 0.4, remotePacketsLost: 3 });
    assert.match(v.reason, /capture starving/);
});

test('queueing is flagged even when the cadence looks right', () => {
    const v = classifyAudioWindow({ ...speaking, sendDelayPerPacketMs: 45 });
    assert.equal(v.state, 'problem');
    assert.match(v.reason, /send delay/);
});

test('a starved cadence during speech is flagged', () => {
    const v = classifyAudioWindow({ ...speaking, packets: 20 });
    assert.equal(v.state, 'problem');
    assert.match(v.reason, /only 20\.0 pkt\/s/);
});

test('audio energy with no packets at all is flagged', () => {
    // Distinct from silence: the agent is producing sound and none of it is leaving.
    const v = classifyAudioWindow({ ...speaking, packets: 0 });
    assert.equal(v.state, 'problem');
    assert.match(v.reason, /no packets sent/);
});

test('a high cadence at the start of an utterance is not flagged', () => {
    // A DTX sender resuming mid-window legitimately overshoots 50/s. The first
    // version called this "bursting after a stall" and it was just speech onset.
    assert.equal(classifyAudioWindow({ ...speaking, packets: 62 }).state, 'ok');
});

test('missing optional stats do not produce a false alarm', () => {
    // Not every stat is present on every browser or before the first RTCP report.
    // Absence must read as "unknown", never as a fault.
    const v = classifyAudioWindow({ seconds: 1, packets: 50, energyDelta: 0.02 });
    assert.equal(v.state, 'ok');
});

test('the speech threshold sits above a silent null sink and below real speech', () => {
    // Energy is summed squared amplitude; the observed values differ by orders of
    // magnitude, so this only has to land between them.
    assert.ok(SPEECH_ENERGY_THRESHOLD > 0);
    assert.equal(classifyAudioWindow({ ...silent, energyDelta: SPEECH_ENERGY_THRESHOLD }).state, 'idle');
    assert.equal(classifyAudioWindow({ ...speaking, energyDelta: 0.01 }).state, 'ok');
});

test('the sampling interval can resolve a single utterance', () => {
    // A typical assistant reply is a few seconds. At 5s the utterance and the
    // silence around it landed in one window and the rate became meaningless.
    assert.ok(STATS_INTERVAL_MS <= 1000, 'must be <= 1s to separate speech from silence');
    assert.ok(STATS_INTERVAL_MS >= 1000, 'but not so often that polling adds load');
});
