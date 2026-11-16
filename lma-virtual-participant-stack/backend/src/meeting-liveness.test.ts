/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

import { strict as assert } from 'node:assert';
import { test } from 'node:test';
import {
    PARTICIPANT_ACTIVITY_WINDOW_MS,
    lastParticipantActivity,
    noteParticipantActivity,
    participantsActiveWithin,
    resetParticipantActivity,
} from './meeting-liveness.js';

test('no participant activity yet is not evidence of a live meeting', () => {
    resetParticipantActivity();
    assert.equal(participantsActiveWithin(), false);
    assert.deepEqual(lastParticipantActivity(), { at: null, source: '' });
});

test('participants count as active for the window after they last spoke, and not after', () => {
    resetParticipantActivity();
    const t0 = Date.parse('2026-11-16T16:00:00Z');
    noteParticipantActivity('participant speech', t0);
    assert.equal(participantsActiveWithin(PARTICIPANT_ACTIVITY_WINDOW_MS, t0), true);
    assert.equal(participantsActiveWithin(PARTICIPANT_ACTIVITY_WINDOW_MS, t0 + PARTICIPANT_ACTIVITY_WINDOW_MS), true);
    assert.equal(participantsActiveWithin(PARTICIPANT_ACTIVITY_WINDOW_MS, t0 + PARTICIPANT_ACTIVITY_WINDOW_MS + 1), false);
    noteParticipantActivity('active speaker', t0 + PARTICIPANT_ACTIVITY_WINDOW_MS);
    assert.equal(participantsActiveWithin(PARTICIPANT_ACTIVITY_WINDOW_MS, t0 + PARTICIPANT_ACTIVITY_WINDOW_MS + 1), true);
    assert.equal(lastParticipantActivity().source, 'active speaker');
    resetParticipantActivity();
});
