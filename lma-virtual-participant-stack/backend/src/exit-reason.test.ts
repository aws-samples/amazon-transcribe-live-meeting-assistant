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
import {
    ExitReasonCode,
    MAX_EXIT_TRIGGER_LENGTH,
    didJoinMeeting,
    formatExitMessage,
    normalizeExitTrigger,
} from './details.js';

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
// Bounding the trigger before it is stored
// ---------------------------------------------------------------------------

test('normalizeExitTrigger leaves a trigger from the taxonomy exactly as it is', () => {
    // The whole value of storing this is that rows sharing a cause share a
    // string, so the fixed triggers must survive untouched. The ones that
    // already read as a slug are unchanged by being slugged.
    for (const trigger of [
        'pre-join:not-admitted',
        'pre-join:ai-error-screen',
        'acs-not-admitted',
        'enterprise-login-timeout',
        'attendees-left',
        'meetingTimeout',
        'chat',
        'ZOOM_END_DIALOG',
        'HANGUP_BUTTON_HIDDEN',
        'error:renderer-crashed',
    ]) {
        assert.equal(normalizeExitTrigger(trigger), trigger);
    }
});

/**
 * A trigger template literal, split at the colon that separates its prefix
 * from its detail.
 *
 * The interpolations are blanked out first, because an interpolation can
 * contain a colon of its own — the Teams SDK picks its prefix with a ternary —
 * and that colon is not the one that matters. `prefix` is returned with its
 * interpolations expanded into the fixed words they can produce, so a prefix
 * built that way yields one name per word.
 */
const splitTrigger = (template: string) => {
    const interpolations = [...template.matchAll(/\$\{[^}]*\}/g)].map((match) => match[0]);
    const blanked = template.replace(/\$\{[^}]*\}/g, '\u0000');
    const colon = blanked.indexOf(':');
    if (colon < 0) {
        return { prefixes: [], detailInterpolated: false, hasColon: false };
    }
    const beforeColon = blanked.slice(0, colon);
    const afterColon = blanked.slice(colon + 1);
    // Expand each interpolation in the prefix into the quoted words it can
    // yield; anything else leaves the prefix unenumerable.
    let prefixes = [beforeColon];
    for (let index = 0; index < [...beforeColon].filter((c) => c === '\u0000').length; index += 1) {
        const words = [...interpolations[index].matchAll(/'([A-Za-z0-9_-]+)'/g)].map((m) => m[1]);
        prefixes = prefixes.flatMap((candidate) =>
            (words.length ? words : ['\u0000']).map((word) => candidate.replace('\u0000', word)),
        );
    }
    return {
        prefixes,
        detailInterpolated: afterColon.includes('\u0000'),
        hasColon: true,
    };
};

test('every trigger that carries a detail puts it after a colon', () => {
    // Read from the handlers rather than restated, because a fixture that
    // encoded the shape a handler used to emit would keep passing while the
    // handler drifted away from it — which is how the Teams SDK's end-of-call
    // trigger came to carry the platform's codes with no colon, and so never
    // be slugged. normalizeExitTrigger leaves everything before the first colon
    // alone, so an interpolated value placed before it is stored verbatim and
    // its rows cannot be counted.
    //
    // A trigger may interpolate before the colon as well, as long as it also
    // interpolates after one: the Teams SDK picks its prefix from two fixed
    // words that way, which is bounded and so still groupable.
    const offenders: string[] = [];
    let checked = 0;
    for (const name of HANDLERS) {
        for (const literal of handlerSource(name).matchAll(/trigger:\s*`([^`]*)`/g)) {
            const template = literal[1];
            if (!template.includes('${')) continue;
            checked += 1;
            const { hasColon, detailInterpolated } = splitTrigger(template);
            if (!hasColon || !detailInterpolated) {
                offenders.push(`${name}.ts: \`${template}\``);
            }
        }
    }
    assert.equal(checked, 5, `expected five interpolated triggers, found ${checked}`);
    assert.deepEqual(offenders, [], 'these triggers interpolate a detail outside a colon');
});

test('every trigger the code can write is documented', () => {
    // The published vocabulary and the code have drifted apart more than once,
    // always in the same direction: a trigger added or reshaped in a handler
    // while the page kept describing the old one. Reading both and comparing is
    // the only version of this that stays true.
    //
    // Fixed literals must appear verbatim. A trigger built from a template
    // contributes its prefix instead, since the detail after the colon comes
    // from the platform and cannot be enumerated.
    const docs = readFileSync(
        new URL('../../../docs/virtual-participant.md', new URL('.', import.meta.url)),
        'utf8',
    );
    const documented = (value: string) => docs.includes(`\`${value}`);

    const sources = [...HANDLERS, 'index'];
    const missing: string[] = [];
    let found = 0;
    for (const name of sources) {
        const source = handlerSource(name);
        for (const match of source.matchAll(/trigger:\s*'([^']+)'/g)) {
            found += 1;
            if (!documented(match[1])) missing.push(`${name}.ts: ${match[1]}`);
        }
        for (const match of source.matchAll(/trigger:\s*`([^`]*)`/g)) {
            const { hasColon, prefixes } = splitTrigger(match[1]);
            if (!hasColon) continue; // asserted separately
            for (const prefix of prefixes) {
                found += 1;
                if (!documented(prefix)) missing.push(`${name}.ts: ${prefix}:…`);
            }
        }
        // setFailed's second argument carries the container's own classes.
        for (const match of source.matchAll(/setFailed\([^)]*'(error:[a-z-]+)'/g)) {
            found += 1;
            if (!documented(match[1])) missing.push(`${name}.ts: ${match[1]}`);
        }
    }
    assert.ok(found > 40, `only found ${found} triggers — the scanner needs updating`);
    assert.deepEqual(missing, [], 'these triggers are not on the Virtual Participant page');
});

test('the triggers the handlers build normalise to groupable values', () => {
    // The shapes the handlers actually emit, with a representative detail
    // substituted. These are the only triggers whose stored value is not the
    // literal in the source, so they are the ones worth pinning.
    const emitted: Array<[string, string]> = [
        ['ACS_DISCONNECTED:code=490 subCode=0', 'ACS_DISCONNECTED:code-490-subcode-0'],
        ['ACS_REMOVED:no endReason', 'ACS_REMOVED:no-endreason'],
        ['participants-ui:counter_gone', 'participants-ui:counter-gone'],
        ['participants-ui:page_dead', 'participants-ui:page-dead'],
        ['webex-text:meeting has ended', 'webex-text:meeting-has-ended'],
        ['error:Target closed', 'error:target-closed'],
        ['poll-error:Target closed', 'poll-error:target-closed'],
    ];
    for (const [raw, stored] of emitted) {
        assert.equal(normalizeExitTrigger(raw), stored, raw);
    }
});

test('normalizeExitTrigger slugs what follows the colon however short it is', () => {
    // Slugging only long triggers would store a short platform message
    // verbatim, and two reports of one failure differing by a word or a full
    // stop would land in groups of one — which is the property the field
    // exists for.
    assert.equal(normalizeExitTrigger('error:Target closed'), 'error:target-closed');
    assert.equal(normalizeExitTrigger('error:Target closed.'), 'error:target-closed');
    assert.equal(normalizeExitTrigger('poll-error:Target closed'), 'poll-error:target-closed');
    // The phrase a Webex meeting-end was matched on is a closed set, and
    // hyphenating it keeps it one value per phrase.
    assert.equal(
        normalizeExitTrigger('webex-text:meeting has ended'),
        'webex-text:meeting-has-ended',
    );
});

test('normalizeExitTrigger groups two reports of one short failure', () => {
    const first = normalizeExitTrigger('error:Target closed');
    const second = normalizeExitTrigger('error:  Target  CLOSED!  ');
    assert.equal(first, second);
});

test('normalizeExitTrigger leaves a trigger with no colon alone', () => {
    // These are the fixed ones; slugging would only make them harder to
    // recognise, and they are already one value per cause.
    for (const trigger of ['ZOOM_END_DIALOG', 'URL_CHANGE_BLANK', 'attendee-badge-missing']) {
        assert.equal(normalizeExitTrigger(trigger), trigger);
    }
});

test('normalizeExitTrigger reduces an interpolated error message to something groupable', () => {
    // A Playwright message is long, multi-line and near enough unique. Stored
    // whole it would put every such row in a group of one and duplicate
    // errorMessage; the part that classifies is the prefix plus the start of
    // the message, which is stable.
    const playwright =
        'error:page.waitForSelector: Target closed\n' +
        '=========================== logs ===========================\n' +
        "waiting for selector 'button[data-test=\"join\"]' to be visible\n" +
        '============================================================';
    const normalized = normalizeExitTrigger(playwright);
    assert.ok(normalized);
    assert.ok(normalized.length <= MAX_EXIT_TRIGGER_LENGTH);
    assert.ok(normalized.startsWith('error:page-waitforselector-target-closed'));
    assert.ok(!normalized.includes('\n'), 'must be a single line');

    // Two occurrences of the same failure normalise to the same value — the
    // property the whole field depends on.
    assert.equal(normalized, normalizeExitTrigger(`${playwright}\nretry 2`));
});

test('normalizeExitTrigger keeps the classifying prefix of a long trigger', () => {
    const poll = `poll-error:${'Execution context was destroyed. '.repeat(10)}`;
    const normalized = normalizeExitTrigger(poll);
    assert.ok(normalized?.startsWith('poll-error:execution-context-was-destroyed'));
});

test('normalizeExitTrigger returns nothing for a trigger that says nothing', () => {
    assert.equal(normalizeExitTrigger(undefined), undefined);
    assert.equal(normalizeExitTrigger(''), undefined);
    assert.equal(normalizeExitTrigger('   \n  '), undefined);
});

test('normalizeExitTrigger is idempotent', () => {
    // It runs at the persistence boundary; applying it twice must not erode the
    // value further.
    for (const trigger of ['pre-join:not-admitted', `error:${'x y '.repeat(60)}`]) {
        const once = normalizeExitTrigger(trigger);
        assert.equal(normalizeExitTrigger(once), once);
    }
});

test('a trigger with no colon is still bounded', () => {
    const normalized = normalizeExitTrigger('A'.repeat(500));
    assert.equal(normalized?.length, MAX_EXIT_TRIGGER_LENGTH);
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

test('every bare unknown is a post-admission wait initialiser', () => {
    // The pair scanner above only sees `reason: '…', trigger: '…'` in that
    // order, so a bare `reason: 'unknown'` with no trigger is invisible to it —
    // and that is the shape of the four initialisers. Each one has to be the
    // declaration of the exit value in the handler's wait for the meeting to
    // end, which is reached only past the admission gate. A bare unknown
    // anywhere else, a pre-join branch most of all, would be scored as a
    // meeting that completed.
    //
    // If this fails because a handler was added or renamed, check where the new
    // occurrence sits before changing the expectation.
    const bare: Array<{ file: string; line: string }> = [];
    for (const name of HANDLERS) {
        for (const line of handlerSource(name).split('\n')) {
            if (/reason:\s*'unknown'\s*}/.test(line)) {
                bare.push({ file: `${name}.ts`, line: line.trim() });
            }
        }
    }
    assert.equal(bare.length, 4, `expected four bare unknowns, found ${bare.length}`);
    for (const { file, line } of bare) {
        assert.match(
            line,
            /^let exitInfo: ExitInfo = \{ reason: 'unknown' \};$/,
            `${file}: a bare unknown outside a wait initialiser`,
        );
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
