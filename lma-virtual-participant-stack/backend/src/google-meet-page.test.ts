/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The Google Meet handler's page knowledge, tested without a browser: meeting
 * code and link parsing, the readings of Meet's screens and controls, and the
 * exits each screen maps to. What these cannot show is that Meet's live page
 * still matches the selectors and phrases; google-meet-page.ts marks each of
 * those as an assumption to verify in a live meeting.
 */
import { strict as assert } from 'node:assert';
import { existsSync, readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';
import {
    MEET_IN_PAGE_SELECTORS,
    MEET_SCREEN_PHRASES,
    MEET_SELECTORS,
    classifyMeetScreenText,
    cleanMeetParticipantName,
    isMeetRefusalRedirect,
    isOwnMeetName,
    normalizeMeetText,
    parseGoogleMeetMeeting,
    parseMeetParticipantCount,
    pickMeetActiveSpeaker,
    senderFromMeetGroupHeader,
} from './google-meet-page.js';
import { isOwnMeetMessage, meetInMeetingExit, meetPreJoinExit } from './google-meet.js';
import { details } from './details.js';

// ---------------------------------------------------------------- codes and links

/**
 * The table every implementation of the Meet code rule runs (the create form,
 * the invitation parser, the SDK and the meeting assistant's tools too), found
 * by walking up from this file so it resolves from dist/ as well as src/.
 */
function sharedMeetCases(): Array<{ input: string | null; code: string | null }> {
    let dir = dirname(fileURLToPath(import.meta.url));
    for (;;) {
        const candidate = join(dir, 'test-fixtures', 'google-meet-inputs.json');
        if (existsSync(candidate)) return JSON.parse(readFileSync(candidate, 'utf8')).cases;
        const parent = dirname(dir);
        assert.notEqual(parent, dir, 'test-fixtures/google-meet-inputs.json not found above this file');
        dir = parent;
    }
}

test('each shared Meet input is reduced to its code, or refused, as the table says', () => {
    const cases = sharedMeetCases();
    assert.ok(cases.length > 20, 'the shared table was read');
    for (const { input, code } of cases) {
        const parsed = parseGoogleMeetMeeting(input);
        if (code === null) {
            assert.equal(parsed, null, String(input));
        } else {
            assert.deepEqual(parsed, { code, url: `https://meet.google.com/${code}?hl=en` }, String(input));
        }
    }
});

test('undefined is refused like an empty value', () => {
    assert.equal(parseGoogleMeetMeeting(undefined), null);
});

test('the refusal redirect is recognised, and nothing else is', () => {
    assert.equal(isMeetRefusalRedirect('https://workspace.google.com/products/meet/'), true);
    assert.equal(isMeetRefusalRedirect('https://workspace.google.com/products/meet'), true);
    assert.equal(isMeetRefusalRedirect('https://workspace.google.com/products/meetings/'), false);
    assert.equal(isMeetRefusalRedirect('https://meet.google.com/abc-defg-hij'), false);
    assert.equal(isMeetRefusalRedirect('about:blank'), false);
    assert.equal(isMeetRefusalRedirect(''), false);
});

// ---------------------------------------------------------------- screens

test("the refusal page as observed live reads as 'refused'", () => {
    // The text of the page a not-signed-in browser on an EC2 host was shown.
    const page = [
        "You can’t join this video call",
        'Return to home screen',
        'Submit feedback',
        'Your meeting is safe',
        'No one can join a meeting unless invited or admitted by the host',
        'Returning to home screen 58',
    ].join('\n');
    assert.equal(classifyMeetScreenText(page)?.screen, 'refused');
    assert.deepEqual(meetPreJoinExit('refused'), { reason: 'never-joined', trigger: 'pre-join:meet-refused' });
});

test('a declined request is told apart from a refusal it shares a heading with', () => {
    const page = "You can't join this call\nSomeone in the call denied your request to join\nReturn to home screen";
    assert.equal(classifyMeetScreenText(page)?.screen, 'denied');
});

test('each screen is recognised by each of its phrases, typographic apostrophes included', () => {
    for (const [screen, phrases] of Object.entries(MEET_SCREEN_PHRASES)) {
        for (const phrase of phrases) {
            const typeset = phrase.replace(/'/g, '’').replace(/^./, (c) => c.toUpperCase());
            const found = classifyMeetScreenText(`Header\n${typeset}\nFooter`);
            // A phrase may belong to a more specific screen checked first; it
            // must never fall through to nothing.
            assert.ok(found, `"${phrase}" was not recognised`);
            if (screen !== 'refused') assert.equal(found.screen, screen, `"${phrase}"`);
        }
    }
});

test('ordinary pre-join and in-meeting text is no screen at all', () => {
    for (const text of [
        "What's your name?\nAsk to join\nOther ways to join",
        'Ready to join?\nNo one else is here\nJoin now\nPresent',
        'Alice Smith\nBob Jones\nLeave call',
        '',
    ]) {
        assert.equal(classifyMeetScreenText(text), null, text);
    }
});

test('the waiting screen does not end the join', () => {
    const found = classifyMeetScreenText("Asking to be let in…\nYou'll join the call when someone lets you in");
    assert.equal(found?.screen, 'waiting');
    assert.equal(meetPreJoinExit('waiting'), null);
});

test('normalizeMeetText folds apostrophes and whitespace', () => {
    assert.equal(normalizeMeetText("  You  CAN’T\n join "), "you can't join");
});

// ---------------------------------------------------------------- exits

test('every pre-join exit is never-joined, with a pre-join trigger', () => {
    for (const screen of Object.keys(MEET_SCREEN_PHRASES) as Array<keyof typeof MEET_SCREEN_PHRASES>) {
        const exit = meetPreJoinExit(screen);
        if (screen === 'waiting') continue;
        assert.ok(exit, screen);
        assert.equal(exit.reason, 'never-joined', screen);
        assert.match(exit.trigger ?? '', /^pre-join:/, screen);
    }
    assert.equal(meetPreJoinExit(null), null);
    assert.deepEqual(meetPreJoinExit('no-response'), { reason: 'never-joined', trigger: 'pre-join:not-admitted' });
});

test('in-meeting screens map to the reasons the other platforms use', () => {
    assert.deepEqual(meetInMeetingExit('removed'), { reason: 'removed-from-meeting', trigger: 'meet-removed-screen' });
    assert.deepEqual(meetInMeetingExit('ended'), { reason: 'host-ended', trigger: 'meet-ended-screen' });
    assert.deepEqual(meetInMeetingExit('left'), { reason: 'page-closed', trigger: 'meet-left-screen' });
    assert.equal(meetInMeetingExit('waiting'), null);
    assert.equal(meetInMeetingExit(null), null);
});

// ---------------------------------------------------------------- count, names, senders

test('the participant count is read from the People button', () => {
    assert.equal(parseMeetParticipantCount('people 3'), 3);
    assert.equal(parseMeetParticipantCount('people', 'People - 12 participants'), 12);
    assert.equal(parseMeetParticipantCount(' 1 '), 1);
});

test('an unreadable or ambiguous count is null, never a guess', () => {
    for (const [text, label] of [
        [null, null],
        ['people', 'People'],
        ['0', null],
        ['people 3 of 4', null],
    ] as Array<[string | null, string | null]>) {
        assert.equal(parseMeetParticipantCount(text, label), null, `${text} / ${label}`);
    }
});

test("Meet's decorations are stripped from a participant name", () => {
    assert.equal(cleanMeetParticipantName('Alice Smith (You)'), 'Alice Smith');
    assert.equal(cleanMeetParticipantName('Alice Smith (Presentation)'), 'Alice Smith');
    assert.equal(cleanMeetParticipantName('Alice Smith is presenting'), 'Alice Smith');
    assert.equal(cleanMeetParticipantName('  Alice   Smith '), 'Alice Smith');
    assert.equal(cleanMeetParticipantName(''), null);
    assert.equal(cleanMeetParticipantName('(You)'), null);
});

test('the text of an icon in a tile is not taken for a participant name', () => {
    assert.equal(cleanMeetParticipantName('visual_effects'), null);
    assert.equal(cleanMeetParticipantName('mic_off'), null);
    assert.equal(cleanMeetParticipantName('bob'), 'bob');
    assert.equal(cleanMeetParticipantName('Mary_Ann'), 'Mary_Ann');
});

test('a chat group header gives its sender', () => {
    assert.equal(senderFromMeetGroupHeader('Alice Smith 10:32 AM'), 'Alice Smith');
    assert.equal(senderFromMeetGroupHeader('Alice Smith 14:05'), 'Alice Smith');
    assert.equal(senderFromMeetGroupHeader('You 10:32 AM'), 'You');
    assert.equal(senderFromMeetGroupHeader('Alice Smith'), 'Alice Smith');
});

test('a header spanning two groups gives no sender, rather than the wrong one', () => {
    // A group selector that matched the whole list would put every header in
    // one text; crediting the first name would misattribute every message.
    assert.equal(senderFromMeetGroupHeader('Alice Smith 10:32 AM Bob Jones 10:33 AM'), null);
    assert.equal(senderFromMeetGroupHeader(''), null);
    assert.equal(senderFromMeetGroupHeader(null), null);
    assert.equal(senderFromMeetGroupHeader('10:32 AM'), null);
});

test("the VP's own name is matched whole, not as a prefix or substring", () => {
    const ids = [details.scribeIdentity, 'LMA'];
    assert.equal(isOwnMeetName('You', ids), true);
    assert.equal(isOwnMeetName('LMA', ids), true);
    assert.equal(isOwnMeetName(details.scribeIdentity, ids), true);
    assert.equal(isOwnMeetName(`${details.scribeIdentity} (You)`, ids), true);
    for (const other of ['ALMA GARCIA', 'LMA Smith', 'Youssef Ahmed', 'You Jin Park', null]) {
        assert.equal(isOwnMeetName(other, ids), false, String(other));
    }
});

test("the VP's own messages are recognised by text when no sender is read", () => {
    assert.equal(isOwnMeetMessage(details.introMessages[0]), true);
    assert.equal(isOwnMeetMessage(`  ${details.startMessages[0]} `), true);
    assert.equal(isOwnMeetMessage('LMA leave'), false);
    assert.equal(isOwnMeetMessage(''), false);
});

// ---------------------------------------------------------------- active speaker

test('the speaking tile is reported, never the VP itself', () => {
    const ids = ['LMA (bob)', 'LMA'];
    assert.equal(
        pickMeetActiveSpeaker(
            [
                { name: 'LMA (bob)', speaking: true },
                { name: 'Alice', speaking: false },
                { name: 'Bob', speaking: true },
            ],
            ids,
            null,
        ),
        'Bob',
    );
    assert.equal(pickMeetActiveSpeaker([{ name: 'Alice', speaking: false }], ids, 'Alice'), null);
    assert.equal(pickMeetActiveSpeaker([{ name: null, speaking: true }], ids, null), null);
});

test('overlapping speech keeps the current speaker while they are still heard', () => {
    const tiles = [
        { name: 'Alice', speaking: true },
        { name: 'Bob', speaking: true },
        { name: 'Carol', speaking: false },
    ];
    assert.equal(pickMeetActiveSpeaker(tiles, [], 'Bob'), 'Bob');
    assert.equal(pickMeetActiveSpeaker(tiles, [], 'Carol'), 'Alice');
});

test('an indicator that lights every tile reports nobody', () => {
    const tiles = ['Alice', 'Bob', 'Carol'].map((name) => ({ name, speaking: true }));
    assert.equal(pickMeetActiveSpeaker(tiles, [], null), null);
});

test('a participant shown in two tiles counts once', () => {
    const tiles = [
        { name: 'Alice', speaking: true },
        { name: 'Alice (Presentation)', speaking: false },
        { name: 'Bob', speaking: false },
    ];
    assert.equal(pickMeetActiveSpeaker(tiles, [], null), 'Alice');
});

// ---------------------------------------------------------------- selector guards

const allSelectors = (): string[] => [
    ...Object.values(MEET_SELECTORS).flat(),
    ...Object.values(MEET_IN_PAGE_SELECTORS).flatMap((v) => (Array.isArray(v) ? v : [v])),
];

const balanced = (selector: string, open: string, close: string): boolean => {
    let depth = 0;
    for (const ch of selector) {
        if (ch === open) depth += 1;
        if (ch === close) depth -= 1;
        if (depth < 0) return false;
    }
    return depth === 0;
};

test('every Meet selector is well formed', () => {
    const selectors = allSelectors();
    assert.ok(selectors.length > 25, `only ${selectors.length} selectors found`);
    for (const selector of selectors) {
        assert.ok(selector.trim().length > 0, 'an empty selector');
        assert.ok(!selector.includes('>>>'), `"${selector}" uses the removed >>> combinator`);
        assert.ok(!/,\s*,|^\s*,|,\s*$/.test(selector), `"${selector}" has an empty alternative`);
        assert.ok(balanced(selector, '[', ']'), `"${selector}" has unbalanced [ ]`);
        assert.ok(balanced(selector, '(', ')'), `"${selector}" has unbalanced ( )`);
        assert.equal((selector.match(/"/g) ?? []).length % 2, 0, `"${selector}" has an odd number of quotes`);
    }
});

test('selectors run inside the page are plain CSS', () => {
    // document.querySelector throws on Playwright's own pseudo-classes, and a
    // throw inside page.evaluate reads as "not in the meeting" on every poll.
    for (const value of Object.values(MEET_IN_PAGE_SELECTORS)) {
        for (const selector of Array.isArray(value) ? value : [value]) {
            assert.ok(!/:has-text\(|:contains\(|^role=|^text=/.test(selector), `"${selector}" is not plain CSS`);
        }
    }
});

test('the selectors avoid generated class names except for the speaking indicator', () => {
    // Meet's class names change between releases. Only the speaking indicator
    // has no accessible equivalent, and it is logged so a live run can check it.
    for (const [key, value] of Object.entries({ ...MEET_SELECTORS, ...MEET_IN_PAGE_SELECTORS })) {
        if (key === 'speakingIndicator' || key === 'tileName') continue;
        for (const selector of Array.isArray(value) ? value : [value]) {
            assert.ok(!/(^|[\s>+~])\.[A-Za-z]/.test(selector), `${key}: "${selector}" depends on a class name`);
        }
    }
});

test('the Leave call check cannot match the pre-join screen', () => {
    // The admission wait treats any of these as "in the meeting".
    for (const selector of MEET_IN_PAGE_SELECTORS.leaveCall) {
        assert.match(selector, /Leave call/);
    }
});
