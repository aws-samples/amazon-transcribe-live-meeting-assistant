/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import assert from 'node:assert/strict';
import test from 'node:test';
import { RosterTimeline } from './roster-timeline.js';

const VP = 'LMA (user@example.com)';

test('nothing recorded, or nothing before the row, is Unknown', () => {
    const timeline = new RosterTimeline([VP]);
    assert.equal(timeline.speakerAt(3), 'Unknown');
    timeline.record(5, 'A');
    assert.equal(timeline.speakerAt(4.9), 'Unknown');
});

test('a row is attributed to the speaker active at its start, boundaries inclusive', () => {
    const timeline = new RosterTimeline([VP]);
    timeline.record(0, 'A');
    timeline.record(5, 'B');
    timeline.record(9, 'A');
    assert.deepEqual([4.9, 5, 8.99, 9, 100].map((t) => timeline.speakerAt(t)), ['A', 'B', 'B', 'A', 'A']);
});

test("the VP's own identities are never recorded, so humans talking over it keep their name", () => {
    const timeline = new RosterTimeline([VP, '  ', 'LMA']);
    timeline.record(0, 'A');
    timeline.record(5, VP);
    timeline.record(6, 'none');
    timeline.record(8, 'B');
    assert.equal(timeline.speakerAt(6), 'A');
    assert.equal(timeline.speakerAt(9), 'B');
    const onlyVp = new RosterTimeline([VP]);
    onlyVp.record(1, VP);
    assert.equal(onlyVp.speakerAt(2), 'Unknown');
});

test('equal times keep the latest entry; a step back in the clock is clamped forward', () => {
    const timeline = new RosterTimeline([]);
    timeline.record(5, 'A');
    timeline.record(5, 'B');
    assert.equal(timeline.speakerAt(5), 'B');
    timeline.record(3, 'C');
    assert.equal(timeline.speakerAt(4), 'Unknown');
    assert.equal(timeline.speakerAt(5), 'C');
});
