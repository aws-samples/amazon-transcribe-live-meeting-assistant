/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Tests for the outbound-audio classifier used to diagnose GitHub #543.
 *
 * These assert the thresholds that decide whether a live session's audio is
 * leaving cleanly. That matters because three previous fixes were shipped on
 * reasoning rather than measurement, and one of them broke transcription — so the
 * next change must be justified by what these numbers say.
 */
import { strict as assert } from 'node:assert';
import { test } from 'node:test';
import {
    classifyOutboundAudio,
    EXPECTED_PACKETS_PER_SECOND,
    STATS_INTERVAL_MS,
} from './audio-diagnostics.js';

test('the expected cadence matches Opus at a 20ms frame', () => {
    // 1000ms / 20ms = 50 packets per second. If the meeting client negotiates a
    // different ptime this constant must change, or every sample reads IRREGULAR.
    assert.equal(EXPECTED_PACKETS_PER_SECOND, 50);
});

test('a steady sender is reported healthy', () => {
    const v = classifyOutboundAudio({ packetsPerSecond: 50, sendDelayPerPacketMs: 1 });
    assert.equal(v.healthy, true);
    assert.match(v.reason, /steady/);
});

test('normal sampling drift is tolerated', () => {
    // The sampling window is not frame-aligned, so a healthy sender still varies.
    // Flagging that would bury the real signal in noise.
    for (const pps of [41, 45, 50, 55, 59]) {
        assert.equal(
            classifyOutboundAudio({ packetsPerSecond: pps }).healthy,
            true,
            `${pps}/s should be treated as healthy drift`,
        );
    }
});

test('a starved encoder is flagged', () => {
    // The signature we are hunting: audio being produced too slowly, which is
    // what a CPU-starved capture or encode looks like from the sender side.
    const v = classifyOutboundAudio({ packetsPerSecond: 30 });
    assert.equal(v.healthy, false);
    assert.match(v.reason, /below the expected/);
});

test('bursting after a stall is flagged', () => {
    // The other half of a stall: the queue drains faster than realtime once the
    // CPU frees up. Crackle can come from either side of that.
    const v = classifyOutboundAudio({ packetsPerSecond: 90 });
    assert.equal(v.healthy, false);
    assert.match(v.reason, /exceeds the expected/);
});

test('silence is distinguished from an irregular cadence', () => {
    // "No audio at all" and "audio arriving unevenly" need different fixes, so
    // they must not collapse into one message.
    const v = classifyOutboundAudio({ packetsPerSecond: 0 });
    assert.equal(v.healthy, false);
    assert.match(v.reason, /no audio packets/);
});

test('queueing is flagged even when the cadence looks right', () => {
    // A sender can hit ~50/s while still adding latency per packet; that is a
    // stall being absorbed by a buffer rather than a clean path.
    const v = classifyOutboundAudio({ packetsPerSecond: 50, sendDelayPerPacketMs: 45 });
    assert.equal(v.healthy, false);
    assert.match(v.reason, /send delay/);
});

test('a missing send-delay stat does not produce a false alarm', () => {
    // Not every browser reports totalPacketSendDelay; absence must not read as 0
    // problems OR as a failure.
    assert.equal(classifyOutboundAudio({ packetsPerSecond: 50 }).healthy, true);
});

test('the sampling interval is frequent enough to catch a short meeting', () => {
    assert.ok(STATS_INTERVAL_MS <= 10000, 'should sample at least every 10s');
    assert.ok(STATS_INTERVAL_MS >= 1000, 'but not so often that it adds load');
});
