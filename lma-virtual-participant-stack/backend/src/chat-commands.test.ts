/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The pause and resume chat commands (`details.pauseCommand`,
 * `details.startCommand`) act only on a message that is the command and
 * nothing else — the same whole-message rule the dismissal and assistant-mode
 * commands follow — on every platform.
 *
 * What is NOT covered here: what each platform's DOM scraper actually hands the
 * node side in a live meeting. Zoom's aria-label split is tested below because
 * it is node-side; the Teams, Webex and Chime scrapers run in the page, and are
 * relied on here only to deliver the same string matchesEndCommand already
 * matches against.
 */
import { strict as assert } from 'node:assert';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import { details, matchesChatCommand, matchesEndCommand } from './details.js';
import { parseAssistantChatCommand } from './assistant-toggles.js';
import { splitZoomChatLabel } from './zoom.js';

// ---------------------------------------------------------------------------
// The matcher
// ---------------------------------------------------------------------------

test('the pause command matches as the whole message, in any case, with or without the LMA addressee', () => {
    for (const message of ['PAUSE', 'pause', 'Pause', '  PAUSE  ', 'PAUSE.', 'pause!', 'LMA pause', '@LMA, PAUSE!', 'pause LMA', 'lma: Pause']) {
        assert.equal(matchesChatCommand(message, details.pauseCommand), true, `no longer pauses: "${message}"`);
    }
});

test('the resume command matches as the whole message, in any case, with or without the LMA addressee', () => {
    for (const message of ['START', 'start', 'Start.', 'LMA start', '@LMA START', 'start LMA']) {
        assert.equal(matchesChatCommand(message, details.startCommand), true, `no longer resumes: "${message}"`);
    }
});

test('a message that only contains the pause command is ordinary chat', () => {
    for (const message of [
        'PAUSED',
        'UNPAUSE',
        "please don't PAUSE",
        'PAUSE the recording',
        'Type PAUSE to stop the transcript',
        'type "PAUSE" in chat',
        'LMA PAUSE now',
        'LMA',
        '',
        '   ',
        '!!!',
    ]) {
        assert.equal(matchesChatCommand(message, details.pauseCommand), false, `pauses on prose: "${message}"`);
    }
});

test('a message that only contains the resume command is ordinary chat', () => {
    for (const message of ['STARTED', 'RESTART', "let's START now", 'START over', 'START time is 3pm', 'LMA START please']) {
        assert.equal(matchesChatCommand(message, details.startCommand), false, `resumes on prose: "${message}"`);
    }
});

test('an empty command never matches', () => {
    assert.equal(matchesChatCommand('PAUSE', ''), false);
    assert.equal(matchesChatCommand('', ''), false);
    assert.equal(matchesChatCommand('LMA', ''), false);
});

// ---------------------------------------------------------------------------
// The VP's own chat messages cannot trigger the commands
// ---------------------------------------------------------------------------

test("the VP's own introduction and status messages are neither a pause nor a resume command", () => {
    // Several scrapers read the VP's own outgoing chat back (Teams polls every
    // rendered row), so a default message that matched would toggle the VP.
    const own = [...details.introMessages, ...details.startMessages, ...details.pauseMessages, ...details.exitMessages];
    assert.ok(own.length >= 4);
    for (const message of own) {
        assert.equal(matchesChatCommand(message, details.pauseCommand), false, `own message pauses: "${message}"`);
        assert.equal(matchesChatCommand(message, details.startCommand), false, `own message resumes: "${message}"`);
    }
});

test('an introduction message that explains the commands does not trigger them', () => {
    const explained =
        'Hello. I am an AI Live Meeting Assistant (LMA). Type "PAUSE" to pause the transcript, ' +
        '"START" to resume it, or "LMA leave" to dismiss me.';
    assert.equal(matchesChatCommand(explained, details.pauseCommand), false);
    assert.equal(matchesChatCommand(explained, details.startCommand), false);
    assert.equal(matchesEndCommand(explained), false);
});

// ---------------------------------------------------------------------------
// The vocabularies stay disjoint
// ---------------------------------------------------------------------------

test('the pause and resume commands are neither a dismissal nor an assistant-mode toggle', () => {
    for (const message of ['PAUSE', 'LMA pause', 'START', 'LMA start']) {
        assert.equal(matchesEndCommand(message), false, `"${message}" claimed as a dismissal`);
        assert.equal(parseAssistantChatCommand(message), null, `"${message}" claimed as a toggle`);
    }
    assert.equal(matchesChatCommand('PAUSE', details.startCommand), false);
    assert.equal(matchesChatCommand('START', details.pauseCommand), false);
});

// ---------------------------------------------------------------------------
// Zoom: the command is matched against the body, not the aria-label
// ---------------------------------------------------------------------------

test('a Zoom chat label in the colon form is split into sender and body', () => {
    assert.deepEqual(splitZoomChatLabel('Alice Smith to Everyone: PAUSE'), { sender: 'Alice Smith', body: 'PAUSE' });
});

test('a Zoom chat label in the timestamp form is split into sender and body', () => {
    assert.deepEqual(splitZoomChatLabel('Alice Smith to Everyone, 10:42 AM, PAUSE'), {
        sender: 'Alice Smith',
        body: 'PAUSE',
    });
    assert.deepEqual(splitZoomChatLabel('Bob to Everyone, 14:05, LMA start'), { sender: 'Bob', body: 'LMA start' });
});

test('a Zoom label whose body has its own colon keeps the whole body', () => {
    assert.deepEqual(splitZoomChatLabel('Alice to Everyone: note: PAUSE later'), {
        sender: 'Alice',
        body: 'note: PAUSE later',
    });
});

test('a Zoom label in neither shape is returned whole with no sender', () => {
    assert.deepEqual(splitZoomChatLabel('PAUSE'), { sender: null, body: 'PAUSE' });
});

test('a Zoom pause command matches through the label split, and prose around it does not', () => {
    const matches = (label: string) => matchesChatCommand(splitZoomChatLabel(label).body, details.pauseCommand);
    assert.equal(matches('Alice to Everyone: PAUSE'), true);
    assert.equal(matches('Alice to Everyone, 10:42 AM, pause'), true);
    assert.equal(matches('Alice to Everyone: PAUSED for lunch'), false);
    // A sender whose name is the command word does not make their message one.
    assert.equal(matches('PAUSE to Everyone: see you after the break'), false);
});

// ---------------------------------------------------------------------------
// Every platform uses the shared matcher
// ---------------------------------------------------------------------------

function readSource(file: string): string {
    // Resolved from this module so the suite works whether it is run from dist/
    // (the normal `node --test dist/*.test.js`) or from src/ under ts-node.
    const here = new URL('.', import.meta.url);
    for (const url of [new URL(`../src/${file}`, here), new URL(`./${file}`, here)]) {
        try {
            return readFileSync(url, 'utf8');
        } catch {
            /* try the next location */
        }
    }
    assert.fail(`could not read ${file} — the path in this test needs updating`);
}

test('every platform handler matches pause and resume with matchesChatCommand', () => {
    for (const file of ['zoom.ts', 'zoom-sdk.ts', 'teams.ts', 'teams-sdk.ts', 'webex.ts', 'chime.ts']) {
        const src = readSource(file);
        for (const command of ['pauseCommand', 'startCommand']) {
            assert.match(
                src,
                new RegExp(`matchesChatCommand\\(\\w+, details\\.${command}\\)`),
                `${file} does not match details.${command} with matchesChatCommand`,
            );
            assert.doesNotMatch(
                src,
                new RegExp(`(includes\\(details\\.${command}\\)|=== details\\.${command})`),
                `${file} still matches details.${command} some other way`,
            );
        }
    }
});
