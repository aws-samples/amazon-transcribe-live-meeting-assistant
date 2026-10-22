/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * How a meeting's ending is classified.
 *
 * Each platform handler returns an `ExitInfo` saying why it stopped. One bit of
 * that -- whether the VP was ever in the meeting -- decides whether the row ends
 * up COMPLETED or FAILED, so a handler that reports a pre-join dead end with a
 * code meaning "ended for some reason we could not classify" is reported to the
 * user as a meeting that completed.
 *
 * Two things are asserted. `didJoinMeeting` is checked against every reason code
 * there is, so the classification of each one is written down somewhere other
 * than the function itself. And the handlers' own pre-join branches are read from
 * source, because the only way to reach them for real is a live meeting the VP
 * is refused entry to.
 */
import { strict as assert } from 'node:assert';
import { test } from 'node:test';
import { readFileSync } from 'node:fs';
import { ExitReasonCode, didJoinMeeting, formatExitMessage } from './details.js';

/**
 * Every reason code, with whether it means the VP was in the meeting.
 *
 * `unknown` is `true` on purpose: every place that produces it is in a
 * handler's post-admission wait for the meeting to end, so it means "was in the
 * meeting, could not classify the exit". Not being admitted has its own code.
 */
const REASONS: ReadonlyArray<readonly [ExitReasonCode, boolean]> = [
    ['end-command', true],
    ['alone-in-meeting', true],
    ['removed-from-meeting', true],
    ['host-ended', true],
    ['meeting-timeout', true],
    ['page-closed', true],
    ['unknown', true],
    ['never-joined', false],
];

test('didJoinMeeting classifies every reason code', () => {
    for (const [reason, joined] of REASONS) {
        assert.equal(didJoinMeeting({ reason }), joined, reason);
    }
});

test('didJoinMeeting is false only for never-joined', () => {
    const notJoined = REASONS.filter(([, joined]) => !joined).map(([reason]) => reason);
    assert.deepEqual(notJoined, ['never-joined']);
});

test('didJoinMeeting ignores the trigger and the rest of the exit info', () => {
    // Only the reason code decides this. A trigger is free-form and is added to
    // without thought for this function.
    assert.equal(didJoinMeeting({ reason: 'never-joined', trigger: 'pre-join:not-admitted' }), false);
    assert.equal(didJoinMeeting({ reason: 'unknown', trigger: 'error:whatever' }), true);
    assert.equal(
        didJoinMeeting({ reason: 'end-command', trigger: 'chat', requestedBy: 'Jeremy' }),
        true,
    );
});

test('every reason code has prose of its own', () => {
    const messages = REASONS.map(([reason]) => formatExitMessage({ reason }));
    assert.equal(new Set(messages).size, messages.length, 'two reason codes share one message');
    for (const message of messages) {
        assert.ok(message.endsWith('.'), `not a sentence: ${message}`);
    }
});

test('the never-joined message does not read as a successful meeting', () => {
    const message = formatExitMessage({ reason: 'never-joined' });
    assert.match(message, /could not join/i);
});

// ---------------------------------------------------------------------------
// The handlers' pre-join branches
// ---------------------------------------------------------------------------

const HANDLERS = ['chime', 'webex', 'zoom', 'zoom-sdk', 'teams', 'teams-sdk'];

/** Source of one handler, comments stripped. */
const handlerSource = (name: string): string => {
    // Resolved from this module so the suite works whether it runs from dist/
    // (the normal `node --test dist/*.test.js`) or from src/ under ts-node.
    const here = new URL('.', import.meta.url);
    const text = [new URL(`../src/${name}.ts`, here), new URL(`./${name}.ts`, here)]
        .map((url) => {
            try {
                return readFileSync(url, 'utf8');
            } catch {
                return '';
            }
        })
        .find((candidate) => candidate.length > 0);
    assert.ok(text, `could not read ${name}.ts — the path in this test needs updating`);
    return text.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '');
};

/** Literal `reason` / `trigger` pairs, in that order, across all handlers. */
const reasonTriggerPairs = (): Array<{ file: string; reason: string; trigger: string }> => {
    const pairs: Array<{ file: string; reason: string; trigger: string }> = [];
    for (const name of HANDLERS) {
        const matches = handlerSource(name).matchAll(
            /reason:\s*'([a-z-]+)',\s*trigger:\s*['`]([^'`]*)['`]/g,
        );
        for (const match of matches) {
            pairs.push({ file: `${name}.ts`, reason: match[1], trigger: match[2] });
        }
    }
    return pairs;
};

const PAIRS = reasonTriggerPairs();

test('the handler sources yield reason/trigger pairs to check', () => {
    // Guards the scanner: a regex that stopped matching would make the
    // assertions below pass by inspecting nothing.
    assert.ok(PAIRS.length >= 15, `only found ${PAIRS.length} reason/trigger pairs`);
});

test('a pre-join trigger always reports never-joined', () => {
    // A branch tagged `pre-join:` has not entered the meeting by definition, and
    // 'never-joined' is the only code that keeps it from being scored as a
    // normal completion.
    const wrong = PAIRS.filter(
        (pair) => pair.trigger.startsWith('pre-join:') && pair.reason !== 'never-joined',
    );
    assert.deepEqual(wrong, [], 'pre-join branches not reporting never-joined');
});

test('a not-admitted trigger always reports never-joined', () => {
    // Covers the SDK handlers too, whose triggers are 'acs-not-admitted' and
    // 'sdk-not-admitted' rather than 'pre-join:*'.
    const wrong = PAIRS.filter(
        (pair) => pair.trigger.includes('not-admitted') && pair.reason !== 'never-joined',
    );
    assert.deepEqual(wrong, [], 'not-admitted branches not reporting never-joined');
});

test('a Webex meeting the VP is not admitted to reports never-joined', () => {
    const notAdmitted = PAIRS.filter(
        (pair) => pair.file === 'webex.ts' && pair.trigger === 'pre-join:not-admitted',
    );
    assert.equal(notAdmitted.length, 1, 'the Webex not-admitted branch is no longer recognisable');
    assert.equal(notAdmitted[0].reason, 'never-joined');
});

test('a Chime meeting the VP is not admitted to reports never-joined', () => {
    const preJoin = PAIRS.filter(
        (pair) => pair.file === 'chime.ts' && pair.trigger.startsWith('pre-join:'),
    );
    assert.deepEqual(
        preJoin.map((pair) => pair.trigger).sort(),
        ['pre-join:no-join-button', 'pre-join:not-admitted'],
        'the Chime pre-join dead ends are no longer recognisable',
    );
    for (const pair of preJoin) {
        assert.equal(pair.reason, 'never-joined', pair.trigger);
    }
});

test('no handler reports a pre-join dead end as unknown', () => {
    // 'unknown' is reserved for exits after admission; scoring one of those as a
    // failure would report FAILED for meetings that ran and were transcribed,
    // which is why the pre-join paths have to carry their own code.
    const unknownPreJoin = PAIRS.filter(
        (pair) => pair.reason === 'unknown' && pair.trigger.startsWith('pre-join:'),
    );
    assert.deepEqual(unknownPreJoin, []);
});
