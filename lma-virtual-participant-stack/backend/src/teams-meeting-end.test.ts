/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Regression tests for the Teams meeting-end watchdog, pinning all three
 * directions it has failed in.
 *
 * GitHub #540 — a Teams VP kept running after the user ended the meeting: 135
 * video segments (~630 MB) uploaded to S3 after everyone had left, a live Nova
 * Sonic session, and a MicroVM that would have run to its 8-hour ceiling. It
 * logged "attendee badge missing/empty — treating as unknown (not leaving)"
 * every 20 seconds, forever, because the missing-badge path returned
 * unconditionally. Teams removes the roster badge when the meeting ends, so the
 * badge is absent in exactly the case the watchdog exists to detect.
 *
 * GitHub #317/#318 — the opposite failure: a single transient misread of a
 * PRESENT badge ended live meetings mid-sentence.
 *
 * GitHub #660 — the same early-leave symptom via the *missing*-badge counter
 * once it was bounded, because both counters shared one ~40s bound. Teams also
 * drops the roster button (and its badge) during content share and toolbar
 * collapse, which routinely exceeds 40s, so the two bounds are now separate and
 * the missing-badge one is minutes wide.
 *
 * Every "ends" test therefore has a matching "does not end too early" test.
 */
import { strict as assert } from 'node:assert';
import { test } from 'node:test';
import {
    DEFAULT_ATTENDEE_WATCHDOG_CONFIG,
    decideAttendeeAction,
    resolveAttendeeWatchdogConfig,
    type AttendeeReading,
    type AttendeeWatchdogState,
} from './teams.js';

const fresh = (): AttendeeWatchdogState => ({
    consecutiveLonely: 0,
    consecutiveMissing: 0,
    suppressedMissing: 0,
    unreadableRun: 0,
});
const MISSING: AttendeeReading = { state: 'BADGE_MISSING' };
const MISSING_IN_MEETING: AttendeeReading = { state: 'BADGE_MISSING_IN_MEETING' };
const busy: AttendeeReading = { state: 'OK', count: 3 };
const alone: AttendeeReading = { state: 'OK', count: 1 };

const ALONE_BOUND = DEFAULT_ATTENDEE_WATCHDOG_CONFIG.pollsBeforeEnd;
const MISSING_BOUND = DEFAULT_ATTENDEE_WATCHDOG_CONFIG.pollsBeforeEndMissing;

test('a sustained missing badge ends the meeting — the #540 fix', () => {
    const state = fresh();
    for (let i = 1; i < MISSING_BOUND; i += 1) {
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
    // The literal #540 defect: the old code returned unconditionally, so this
    // loop would have produced 'continue' forever.
    const state = fresh();
    const actions = new Set<string>();
    for (let i = 0; i < MISSING_BOUND * 4; i += 1) {
        actions.add(decideAttendeeAction(MISSING, state).action);
    }
    assert.ok(actions.has('end'), 'a missing badge must eventually end the meeting');
});

test('ONE transient missing badge does not end a live meeting (#317/#318)', () => {
    // The badge legitimately disappears on a collapsed roster, content share or
    // re-layout. Reacting to a single misread is what ended live meetings
    // mid-sentence, so this must stay non-fatal.
    const state = fresh();
    assert.equal(decideAttendeeAction(MISSING, state).action, 'continue');
});

test('the missing-badge bound survives a multi-minute content share (#660)', () => {
    // The reported failure: a Teams content share collapsed the toolbar, the
    // badge vanished, and the VP left a live meeting ~40s later while people
    // were still speaking. The bound must absorb a share of several minutes.
    const { pollMs, pollsBeforeEndMissing } = DEFAULT_ATTENDEE_WATCHDOG_CONFIG;
    assert.ok(
        (pollsBeforeEndMissing * pollMs) / 1000 >= 240,
        'a missing badge must be tolerated for at least four minutes',
    );

    const state = fresh();
    const pollsInFourMinutes = Math.floor((4 * 60 * 1000) / pollMs);
    for (let i = 0; i < pollsInFourMinutes; i += 1) {
        assert.equal(
            decideAttendeeAction(MISSING, state).action,
            'continue',
            `a missing badge at poll ${i} must not end a live meeting`,
        );
    }
});

test('the two bounds are decoupled — the missing bound is much wider (#660)', () => {
    // Sharing one bound is what re-introduced early-leave. A genuine "<=1" read
    // is a direct measurement and stays tight; an absent badge measures nothing.
    assert.ok(
        MISSING_BOUND > ALONE_BOUND,
        'an absent badge must be tolerated for longer than a genuine empty roster',
    );

    // At the alone bound, a run of missing readings must still be harmless.
    const state = fresh();
    for (let i = 0; i < ALONE_BOUND; i += 1) {
        assert.equal(decideAttendeeAction(MISSING, state).action, 'continue');
    }
});

test('a recovered badge resets the missing counter', () => {
    // Flapping must not accumulate toward an exit: content-share toggling on and
    // off should never end a meeting that still has people in it.
    const state = fresh();
    for (let cycle = 0; cycle < 10; cycle += 1) {
        for (let i = 0; i < MISSING_BOUND - 1; i += 1) {
            assert.equal(decideAttendeeAction(MISSING, state).action, 'continue');
        }
        assert.equal(decideAttendeeAction(busy, state).action, 'continue');
        assert.equal(state.consecutiveMissing, 0, 'a good reading must reset the counter');
    }
});

test('sustained alone still ends the meeting, unchanged', () => {
    const state = fresh();
    for (let i = 1; i < ALONE_BOUND; i += 1) {
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
    for (let i = 1; i < ALONE_BOUND; i += 1) {
        decideAttendeeAction({ state: 'OK', count: 0 }, state);
    }
    assert.equal(decideAttendeeAction({ state: 'OK', count: 0 }, state).action, 'end');
});

test('others present resets the lonely counter', () => {
    const state = fresh();
    for (let i = 0; i < ALONE_BOUND - 1; i += 1) decideAttendeeAction(alone, state);
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
    for (let i = 0; i < MISSING_BOUND * 4; i += 1) {
        const reading: AttendeeReading = i % 2 === 0 ? MISSING : busy;
        assert.equal(
            decideAttendeeAction(reading, state).action,
            'continue',
            `alternating readings must not end the meeting (poll ${i})`,
        );
    }
});

test('in-meeting chrome protects a long content share (#660)', () => {
    // The mechanism the wide bound alone does not cover: nothing in the VP moves
    // the pointer, so a toolbar that auto-hides during a share stays hidden for
    // the whole share, which outlasts any bound worth setting. While the
    // in-meeting controls are still there, a missing badge must not count.
    const state = fresh();
    const { pollMs, maxSuppressedPolls } = DEFAULT_ATTENDEE_WATCHDOG_CONFIG;
    assert.ok(
        (maxSuppressedPolls * pollMs) / 1000 >= 25 * 60,
        'a share of at least 25 minutes must be survivable',
    );
    for (let i = 0; i < maxSuppressedPolls; i += 1) {
        assert.equal(
            decideAttendeeAction(MISSING_IN_MEETING, state).action,
            'continue',
            `a corroborated miss at poll ${i} must not end the meeting`,
        );
    }
    assert.equal(state.suppressedMissing, maxSuppressedPolls, 'vetoes must be counted');
    assert.equal(state.consecutiveMissing, 0);
});

test('the in-meeting veto is SPENT, so #540 cannot come back through it', () => {
    // Teams does not reliably remove its in-meeting controls when a meeting ends:
    // in the #540 incident the hang-up button was still present and visible
    // afterwards, which is why the HANGUP_BUTTON_HIDDEN watch never fired. So
    // "chrome is present" cannot hold a VP in a meeting indefinitely, or that
    // incident recurs — a VP recording video and holding a voice session until the
    // four-hour meeting timeout.
    const state = fresh();
    const { maxSuppressedPolls, pollsBeforeEndMissing } = DEFAULT_ATTENDEE_WATCHDOG_CONFIG;
    let ended = -1;
    for (let i = 0; i < maxSuppressedPolls + pollsBeforeEndMissing + 10; i += 1) {
        if (decideAttendeeAction(MISSING_IN_MEETING, state).action === 'end') {
            ended = i;
            break;
        }
    }
    assert.notEqual(ended, -1, 'a permanent in-meeting reading must still end the meeting');
    assert.ok(ended >= maxSuppressedPolls, `ended at poll ${ended}, before the allowance was spent`);
});

/** Drive the state machine until it ends, returning the 1-based poll index. */
const pollsUntilEnd = (
    nextReading: (poll: number) => AttendeeReading,
    limit = 5000,
): number | null => {
    const state = fresh();
    for (let i = 0; i < limit; i += 1) {
        if (decideAttendeeAction(nextReading(i), state).action === 'end') return i + 1;
    }
    return null;
};

test('no interleaving of unreadable readings can outlast the additive ceiling', () => {
    // This is measured against the state machine rather than multiplied out of the
    // constants, and the difference matters: a veto resets the miss counter, so an
    // alternating sequence used to multiply the two bounds instead of adding them.
    // "14 misses then 1 veto" reached 1365 polls — 7.5 hours at the default cadence,
    // which is the #540 outcome by another route.
    const { pollMs, maxSuppressedPolls, pollsBeforeEndMissing } = DEFAULT_ATTENDEE_WATCHDOG_CONFIG;
    const ceiling = maxSuppressedPolls + pollsBeforeEndMissing;
    const patterns: Array<[string, (poll: number) => AttendeeReading]> = [
        ['every poll corroborated', () => MISSING_IN_MEETING],
        ['every poll a bare miss', () => MISSING],
        ['alternating miss / corroborated', (i) => (i % 2 ? MISSING : MISSING_IN_MEETING)],
        ['one veto every other poll', (i) => (i % 2 ? MISSING_IN_MEETING : MISSING)],
        [
            'a veto just before each bound would fire',
            (i) => (i % pollsBeforeEndMissing === pollsBeforeEndMissing - 1 ? MISSING_IN_MEETING : MISSING),
        ],
        [
            'a miss just before the allowance is spent',
            (i) => (i % maxSuppressedPolls === maxSuppressedPolls - 1 ? MISSING : MISSING_IN_MEETING),
        ],
    ];
    for (const [label, pattern] of patterns) {
        const ended = pollsUntilEnd(pattern);
        assert.ok(ended !== null, `"${label}" never ended the meeting`);
        assert.ok(
            ended <= ceiling,
            `"${label}" ended at poll ${ended} (${((ended * pollMs) / 60000).toFixed(1)} min), ` +
                `past the ${ceiling}-poll ceiling`,
        );
    }
});

test('the worst case stays under 40 minutes at the default cadence', () => {
    const { pollMs, maxSuppressedPolls, pollsBeforeEndMissing } = DEFAULT_ATTENDEE_WATCHDOG_CONFIG;
    const worstCaseMinutes = ((maxSuppressedPolls + pollsBeforeEndMissing) * pollMs) / 60000;
    assert.ok(worstCaseMinutes <= 40, `worst case was ${worstCaseMinutes} minutes`);
});

test('the additive ceiling holds at every cadence, not just the default', () => {
    // The two windows are each capped at 30 minutes, so their sum can reach an hour
    // at the slowest cadence. That is the true ceiling to state in the docs — 35
    // minutes is the DEFAULT-cadence figure, not the guarantee.
    for (const cadence of ['1000', '5000', '20000', '60000', '120000']) {
        const config = resolveAttendeeWatchdogConfig({ VP_ATTENDEE_POLL_MS: cadence });
        const ceilingMs = (config.maxSuppressedPolls + config.pollsBeforeEndMissing) * config.pollMs;
        assert.ok(
            ceilingMs <= 60 * 60 * 1000,
            `cadence ${cadence} allows ${(ceilingMs / 60000).toFixed(1)} minutes`,
        );
    }
});

test('a readable badge between shares still restores the full allowance', () => {
    // The run ceiling must not turn a long meeting with several shares into a leave:
    // it is UNREADABLE polls in a row that are bounded, not shares in a meeting.
    const { maxSuppressedPolls } = DEFAULT_ATTENDEE_WATCHDOG_CONFIG;
    const cycle = maxSuppressedPolls - 1;
    const ended = pollsUntilEnd((i) => (i % cycle === cycle - 1 ? busy : MISSING_IN_MEETING), 3000);
    assert.equal(ended, null, `a periodically readable badge must never end the meeting (ended at ${ended})`);
});

test('a readable badge restores the veto allowance', () => {
    // A meeting with several shares in it should get a fresh allowance for each;
    // it is an unbroken run of vetoes that has to stay bounded, not their total.
    const state = fresh();
    for (let i = 0; i < DEFAULT_ATTENDEE_WATCHDOG_CONFIG.maxSuppressedPolls; i += 1) {
        decideAttendeeAction(MISSING_IN_MEETING, state);
    }
    assert.equal(decideAttendeeAction(busy, state).action, 'continue');
    assert.equal(state.suppressedMissing, 0, 'a readable badge must reset the allowance');
    assert.equal(decideAttendeeAction(MISSING_IN_MEETING, state).action, 'continue');
});

test('in-meeting chrome resets a run of uncorroborated misses', () => {
    // A share that starts part-way through a run of bare misses must not inherit
    // the strikes accrued before the chrome was seen.
    const state = fresh();
    for (let i = 0; i < MISSING_BOUND - 1; i += 1) decideAttendeeAction(MISSING, state);
    assert.equal(decideAttendeeAction(MISSING_IN_MEETING, state).action, 'continue');
    assert.equal(state.consecutiveMissing, 0);
    // ...and the next bare miss starts counting from scratch.
    for (let i = 0; i < MISSING_BOUND - 1; i += 1) {
        assert.equal(decideAttendeeAction(MISSING, state).action, 'continue');
    }
});

test('a corroborated miss does not accrue toward the lonely exit either', () => {
    // No count was read, so there is no evidence about being alone. Letting these
    // polls feed the lonely counter would eject the VP from a busy meeting whose
    // roster is merely hidden.
    const state = fresh();
    for (let i = 0; i < ALONE_BOUND - 1; i += 1) decideAttendeeAction(alone, state);
    decideAttendeeAction(MISSING_IN_MEETING, state);
    assert.equal(state.consecutiveLonely, 0);
    assert.equal(decideAttendeeAction(alone, state).action, 'continue');
});

test('an UNcorroborated miss is still bounded — the #540 guard survives', () => {
    // With no badge and no in-meeting chrome, the meeting really has gone. This
    // is the path that must remain finite, or a VP holds a voice session and
    // uploads video to the 8-hour ceiling.
    const state = fresh();
    const actions = new Set<string>();
    for (let i = 0; i < MISSING_BOUND * 4; i += 1) actions.add(decideAttendeeAction(MISSING, state).action);
    assert.ok(actions.has('end'));
});

test('the missing-badge tolerance is bounded ABOVE as well as below', () => {
    // The file's own promise is a test in both directions. Without this, raising
    // the default to 100000 would leave every other test green while restoring
    // the #540 failure.
    const { pollMs, pollsBeforeEndMissing } = DEFAULT_ATTENDEE_WATCHDOG_CONFIG;
    assert.ok(
        (pollsBeforeEndMissing * pollMs) / 1000 <= 600,
        'an ended meeting must not be tolerated for more than ten minutes',
    );
});

test('the alone grace period is long enough to absorb a re-render, short enough to bill', () => {
    // A range rather than an exact equality: the point is the property, not the
    // specific number, so a deliberate cadence change should not fail this.
    const { pollMs, pollsBeforeEnd } = DEFAULT_ATTENDEE_WATCHDOG_CONFIG;
    const seconds = (pollsBeforeEnd * pollMs) / 1000;
    assert.ok(seconds >= 30 && seconds <= 180, `alone grace was ${seconds}s`);
});

test('a caller can shorten either bound for testing without touching prod', () => {
    assert.equal(
        decideAttendeeAction(MISSING, fresh(), {
            ...DEFAULT_ATTENDEE_WATCHDOG_CONFIG,
            pollsBeforeEndMissing: 1,
        }).action,
        'end',
    );
    assert.equal(
        decideAttendeeAction(alone, fresh(), {
            ...DEFAULT_ATTENDEE_WATCHDOG_CONFIG,
            pollsBeforeEnd: 1,
        }).action,
        'end',
    );
});

test('an unset environment yields the defaults', () => {
    assert.deepEqual(resolveAttendeeWatchdogConfig({}), DEFAULT_ATTENDEE_WATCHDOG_CONFIG);
});

test('an operator can widen the bounds from the environment', () => {
    // The whole point of reading these from the task definition: a deployment
    // hitting an unusual Teams layout can be mitigated without rebuilding the
    // container image.
    assert.deepEqual(
        resolveAttendeeWatchdogConfig({
            VP_ATTENDEE_POLL_MS: '30000',
            VP_POLLS_BEFORE_END: '4',
            VP_POLLS_BEFORE_END_MISSING: '40',
        }),
        { pollMs: 30000, pollsBeforeEnd: 4, pollsBeforeEndMissing: 40, maxSuppressedPolls: 60 },
    );
});

test('a bad override falls back to the default rather than disabling the watchdog', () => {
    // An unbounded or zero-length watchdog is the #540 failure mode, so a
    // nonsense value must never be honoured.
    for (const bad of ['', '   ', '0', '-1', 'abc', '1.5', 'NaN', 'Infinity', '3 4', '0x10', '2e4']) {
        assert.deepEqual(
            resolveAttendeeWatchdogConfig({
                VP_ATTENDEE_POLL_MS: bad,
                VP_POLLS_BEFORE_END: bad,
                VP_POLLS_BEFORE_END_MISSING: bad,
            }),
            DEFAULT_ATTENDEE_WATCHDOG_CONFIG,
            `"${bad}" must be ignored`,
        );
    }
});

test('an over-large poll count is rejected, not honoured', () => {
    // One extra zero is the likeliest typo of all, and honouring it would mean a
    // VP that outlives its meeting by most of an hour — the #540 shape again.
    const { pollsBeforeEndMissing, pollsBeforeEnd } = resolveAttendeeWatchdogConfig({
        VP_POLLS_BEFORE_END_MISSING: '150',
        VP_POLLS_BEFORE_END: '300',
    });
    assert.equal(pollsBeforeEndMissing, DEFAULT_ATTENDEE_WATCHDOG_CONFIG.pollsBeforeEndMissing);
    assert.equal(pollsBeforeEnd, DEFAULT_ATTENDEE_WATCHDOG_CONFIG.pollsBeforeEnd);
});

test('the total missing-badge tolerance is capped however the values combine', () => {
    // Both values can be individually in range while their product is not; what
    // decides how long a VP outlives its meeting is the product.
    const { pollMs, pollsBeforeEndMissing } = resolveAttendeeWatchdogConfig({
        VP_ATTENDEE_POLL_MS: '120000',
        VP_POLLS_BEFORE_END_MISSING: '90',
    });
    assert.equal(pollMs, 120000);
    assert.ok(pollMs * pollsBeforeEndMissing <= 30 * 60 * 1000, 'tolerance must cap at 30 minutes');
    assert.ok(pollsBeforeEndMissing >= 1, 'the cap must never reach zero polls');
});

test('an over-large poll interval cannot become a 1ms hot loop', () => {
    // Node clamps a setInterval delay above 2^31-1 to 1ms, so an accidentally
    // huge cadence would spin on page.evaluate and burn the whole missing bound
    // in milliseconds — the opposite of what the operator asked for.
    for (const huge of ['2147483648', '20000000000', '99999999999999999999']) {
        assert.equal(
            resolveAttendeeWatchdogConfig({ VP_ATTENDEE_POLL_MS: huge }).pollMs,
            DEFAULT_ATTENDEE_WATCHDOG_CONFIG.pollMs,
            `"${huge}" must be ignored`,
        );
    }
    // Whatever is accepted must stay inside the signed 32-bit timer range.
    assert.ok(resolveAttendeeWatchdogConfig({ VP_ATTENDEE_POLL_MS: '120000' }).pollMs < 2 ** 31 - 1);
});

test('lowering the cadence cannot narrow the tolerance below the floor', () => {
    // 1000 is the DOCUMENTED minimum, not a typo: an operator investigating an
    // early leave might reasonably lower the cadence for more frequent logging.
    // Before the floor existed that produced a 15-second tolerance — twenty times
    // tighter than the ~40s that caused #660 in the first place.
    const { pollMs, pollsBeforeEndMissing } = resolveAttendeeWatchdogConfig({
        VP_ATTENDEE_POLL_MS: '1000',
    });
    assert.equal(pollMs, 1000);
    const toleranceMs = pollMs * pollsBeforeEndMissing;
    assert.ok(
        toleranceMs >= 4 * 60 * 1000,
        `tolerance was ${toleranceMs}ms, below the four-minute floor`,
    );
});

test('the floor holds at every cadence in range, for any requested count', () => {
    for (const cadence of ['1000', '5000', '20000', '60000', '120000']) {
        for (const requested of ['1', '3', '15', '90']) {
            const config = resolveAttendeeWatchdogConfig({
                VP_ATTENDEE_POLL_MS: cadence,
                VP_POLLS_BEFORE_END_MISSING: requested,
            });
            const toleranceMs = config.pollMs * config.pollsBeforeEndMissing;
            assert.ok(
                toleranceMs >= 4 * 60 * 1000,
                `cadence ${cadence} with ${requested} polls gave ${toleranceMs}ms`,
            );
            assert.ok(
                toleranceMs <= 30 * 60 * 1000,
                `cadence ${cadence} with ${requested} polls gave ${toleranceMs}ms`,
            );
            assert.ok(config.maxSuppressedPolls >= 1);
        }
    }
});

test('the empty-roster bound is capped by wall clock too', () => {
    // 90 polls is half an hour at the default cadence and three hours at the
    // slowest, which is not a "grace period" by any reading.
    const { pollMs, pollsBeforeEnd } = resolveAttendeeWatchdogConfig({
        VP_ATTENDEE_POLL_MS: '120000',
        VP_POLLS_BEFORE_END: '90',
    });
    assert.ok(pollMs * pollsBeforeEnd <= 10 * 60 * 1000, 'alone tolerance must cap at ten minutes');
    assert.ok(pollsBeforeEnd >= 1);
});

test('a sub-second poll interval is rejected', () => {
    // Polling faster than a second would hammer page.evaluate for no benefit and
    // could starve the audio pipeline.
    assert.equal(
        resolveAttendeeWatchdogConfig({ VP_ATTENDEE_POLL_MS: '100' }).pollMs,
        DEFAULT_ATTENDEE_WATCHDOG_CONFIG.pollMs,
    );
    assert.equal(resolveAttendeeWatchdogConfig({ VP_ATTENDEE_POLL_MS: '1000' }).pollMs, 1000);
});
