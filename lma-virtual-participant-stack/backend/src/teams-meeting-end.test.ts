/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Regression tests for GitHub #540 — and guards against re-breaking #317/#318.
 *
 * A Teams VP kept running after the user ended the meeting: 135 video segments
 * (~630 MB) uploaded to S3 after everyone had left, a live Nova Sonic session,
 * and a MicroVM that would have run to its 8-hour ceiling. It logged
 * "attendee badge missing/empty — treating as unknown (not leaving)" every 20
 * seconds, forever.
 *
 * Teams removes the roster badge when the meeting ends, so the badge is absent in
 * exactly the case the watchdog exists to detect. The old code returned
 * unconditionally on a missing badge, so NO number of missing reads could ever end
 * the meeting.
 *
 * The tension these tests pin: #317/#318 were the opposite failure — a single
 * transient misread ended live meetings mid-sentence. Both directions must hold,
 * so every "ends" test has a matching "does not end too early" test.
 */
import { strict as assert } from 'node:assert';
import { test } from 'node:test';
import {
    decideAttendeeAction,
    POLLS_BEFORE_END,
    type AttendeeReading,
    type AttendeeWatchdogState,
} from './teams.js';

const fresh = (): AttendeeWatchdogState => ({ consecutiveLonely: 0, consecutiveMissing: 0 });
const MISSING: AttendeeReading = { state: 'BADGE_MISSING' };
const busy: AttendeeReading = { state: 'OK', count: 3 };
const alone: AttendeeReading = { state: 'OK', count: 1 };

test('a sustained missing badge ends the meeting — the #540 fix', () => {
    const state = fresh();
    for (let i = 1; i < POLLS_BEFORE_END; i += 1) {
        assert.equal(
            decideAttendeeAction(MISSING, state).action,
            'continue',
            `poll ${i} must not end the meeting yet`,
        );
    }
    const decision = decideAttendeeAction(MISSING, state);
    assert.equal(decision.action, 'end');
    assert.equal(decision.action === 'end' && decision.reason, 'removed-from-meeting');
    assert.equal(decision.action === 'end' && decision.trigger, 'attendee-badge-missing');
});

test('the missing-badge path is BOUNDED — it cannot loop forever', () => {
    // The literal defect: the old code returned unconditionally, so this loop
    // would have produced 'continue' 50 times (and forever after).
    const state = fresh();
    const actions = new Set<string>();
    for (let i = 0; i < 50; i += 1) actions.add(decideAttendeeAction(MISSING, state).action);
    assert.ok(actions.has('end'), 'a missing badge must eventually end the meeting');
});

test('ONE transient missing badge does not end a live meeting (#317/#318)', () => {
    // The badge legitimately disappears on a collapsed roster, content share or
    // re-layout. Reacting to a single misread is what ended live meetings
    // mid-sentence, so this must stay non-fatal.
    const state = fresh();
    assert.equal(decideAttendeeAction(MISSING, state).action, 'continue');
});

test('a recovered badge resets the missing counter', () => {
    // Flapping must not accumulate toward an exit: content-share toggling on and
    // off should never end a meeting that still has people in it.
    const state = fresh();
    for (let cycle = 0; cycle < 10; cycle += 1) {
        for (let i = 0; i < POLLS_BEFORE_END - 1; i += 1) {
            assert.equal(decideAttendeeAction(MISSING, state).action, 'continue');
        }
        assert.equal(decideAttendeeAction(busy, state).action, 'continue');
        assert.equal(state.consecutiveMissing, 0, 'a good reading must reset the counter');
    }
});

test('sustained alone still ends the meeting, unchanged', () => {
    const state = fresh();
    for (let i = 1; i < POLLS_BEFORE_END; i += 1) {
        assert.equal(decideAttendeeAction(alone, state).action, 'continue');
    }
    const decision = decideAttendeeAction(alone, state);
    assert.equal(decision.action, 'end');
    assert.equal(decision.action === 'end' && decision.reason, 'alone-in-meeting');
    assert.equal(decision.action === 'end' && decision.trigger, 'attendees-left');
});

test('a count of 0 is treated as alone, not as unknown', () => {
    // parseInt failures map to 0 upstream; 0 attendees means the VP is by itself.
    const state = fresh();
    for (let i = 1; i < POLLS_BEFORE_END; i += 1) {
        decideAttendeeAction({ state: 'OK', count: 0 }, state);
    }
    assert.equal(decideAttendeeAction({ state: 'OK', count: 0 }, state).action, 'end');
});

test('others present resets the lonely counter', () => {
    const state = fresh();
    for (let i = 0; i < POLLS_BEFORE_END - 1; i += 1) decideAttendeeAction(alone, state);
    decideAttendeeAction(busy, state);
    assert.equal(state.consecutiveLonely, 0);
    // ...and the next lone reading starts counting from scratch.
    assert.equal(decideAttendeeAction(alone, state).action, 'continue');
});

test('the two counters do not contaminate each other', () => {
    // A reading is either a genuine count or a miss, never both. Interleaving
    // them must never reach an exit, or a flapping badge on a busy meeting would
    // eject the VP.
    const state = fresh();
    for (let i = 0; i < 30; i += 1) {
        const reading: AttendeeReading = i % 2 === 0 ? MISSING : busy;
        assert.equal(
            decideAttendeeAction(reading, state).action,
            'continue',
            `alternating readings must not end the meeting (poll ${i})`,
        );
    }
});

test('the grace period is ~60s, matching the Zoom watchdog', () => {
    // 3 polls x 20s. Long enough to absorb re-renders, short enough that an
    // abandoned meeting is not billed for long.
    assert.equal(POLLS_BEFORE_END, 3);
});

test('a caller can shorten the debounce for testing without touching prod', () => {
    const state = fresh();
    assert.equal(decideAttendeeAction(MISSING, state, 1).action, 'end');
});
