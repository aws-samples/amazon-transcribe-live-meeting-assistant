/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Reading the participant list from the Teams People pane
 * (teams-people-pane.ts): the parser over the rows the page script returns,
 * in the shape measured in live meetings, and the safeguard that stops the
 * People pane being opened once the chat does not come back after it. The
 * clicks themselves need a live meeting.
 */
import { strict as assert } from 'node:assert';
import { test } from 'node:test';
import {
    parsePeopleTree,
    PEOPLE_READ_INTERVAL_MS,
    PEOPLE_TREE_WAIT_MS,
    peopleCloseVerdict,
    PeoplePaneGuard,
    PeopleTreeRow,
    TEAMS_PEOPLE_BUTTON_SELECTORS,
    TEAMS_PEOPLE_TREE_SELECTOR,
} from './teams-people-pane.js';

const header = (text: string): PeopleTreeRow => ({ level: 1, text, name: null });
const person = (name: string | null, text = name ?? ''): PeopleTreeRow => ({ level: 2, text, name });

const OWN = ['LMA (Ana Silva)', 'Claude probe 14'];

test('the selectors are the ones measured in live meetings', () => {
    assert.equal(TEAMS_PEOPLE_TREE_SELECTOR, '[role="tree"][data-tid="calling-roster-attendees"]');
    assert.deepEqual(TEAMS_PEOPLE_BUTTON_SELECTORS, ['#roster-button', '[data-tid="roster-button"]', 'button[aria-label*="People" i]']);
    assert.ok(PEOPLE_TREE_WAIT_MS <= 3000);
    assert.ok(PEOPLE_READ_INTERVAL_MS >= 60_000, 'at most about once a minute');
});

test('the names under "In this meeting (N)" are read, with N, and the VP left out', () => {
    // As measured with three people in the meeting.
    const rows = [
        header('In this meeting (3)'),
        person('Claude probe 14'),
        person('Strahan, Bob', 'Strahan, Bob Organizer'),
        person('Test browser'),
    ];
    assert.deepEqual(parsePeopleTree(rows, OWN), { ok: true, count: 3, names: ['Strahan, Bob', 'Test browser'] });
});

test("a participant's role badge is not part of the name", () => {
    // The page script takes the name from span[title], outside the badge; the row's text has the badge.
    const reading = parsePeopleTree([header('In this meeting (2)'), person('Strahan, Bob', 'Strahan, Bob Organizer')], OWN);
    assert.deepEqual(reading, { ok: true, count: 2, names: ['Strahan, Bob'] });
});

test('a participant row with no name gives no name, so the list is shorter than the count', () => {
    const reading = parsePeopleTree([header('In this meeting (3)'), person(null, 'Someone'), person('Test browser')], OWN);
    assert.deepEqual(reading, { ok: true, count: 3, names: ['Test browser'] });
});

test('whitespace in a name is tidied, and a long name is cut', () => {
    const reading = parsePeopleTree([header('  In this meeting   (2) '), person(`  Strahan,\n  Bob  `)], OWN);
    assert.deepEqual(reading, { ok: true, count: 2, names: ['Strahan, Bob'] });
    const long = parsePeopleTree([header('In this meeting (2)'), person('x'.repeat(300))]);
    assert.ok(long.ok && long.names[0].length === 100);
});

test('a list in another shape gives no names', () => {
    assert.equal(parsePeopleTree([]).ok, false, 'empty');
    assert.equal(parsePeopleTree([person('Strahan, Bob')]).ok, false, 'no section header');
    assert.equal(parsePeopleTree([header('In this meeting (3)'), person('Bo'), header('Waiting in lobby (1)'), person('Cy')]).ok, false, 'another section');
    assert.equal(parsePeopleTree([header('Presenters (2)'), person('Bo')]).ok, false);
    assert.equal(parsePeopleTree([header('In this meeting (2)'), person('Bo'), header('In this meeting (1)'), person('Cy')]).ok, false, 'two sections');
    assert.equal(parsePeopleTree([header('In this meeting (0)')]).ok, false, 'a count that does not include the VP');
    assert.equal(parsePeopleTree([header('In this meeting')]).ok, false, 'no count');
});

test('a name before the section header, or at another level, is not read', () => {
    const reading = parsePeopleTree([{ level: 3, text: 'x', name: 'Nested' }, header('In this meeting (2)'), person('Bo'), { level: 0, text: '', name: 'Zero' }]);
    assert.deepEqual(reading, { ok: true, count: 2, names: ['Bo'] });
});

// ─── The safeguard ──────────────────────────────────────────────────────────

test('after the People pane closes, a chat still there needs nothing', () => {
    assert.equal(peopleCloseVerdict({ composeVisible: true, chatTurnedOff: false }, false), 'chat-open');
    assert.equal(peopleCloseVerdict({ composeVisible: true, chatTurnedOff: false }, true), 'chat-open');
});

test('a chat Teams has turned off is not blamed on the People pane', () => {
    assert.equal(peopleCloseVerdict({ composeVisible: false, chatTurnedOff: true }, false), 'chat-turned-off');
    assert.equal(peopleCloseVerdict({ composeVisible: false, chatTurnedOff: true }, true), 'chat-turned-off');
});

test('a chat gone after the People pane is tried once, then the People pane is given up', () => {
    assert.equal(peopleCloseVerdict({ composeVisible: false, chatTurnedOff: false }, false), 'recover');
    assert.equal(peopleCloseVerdict({ composeVisible: false, chatTurnedOff: false }, true), 'give-up');
});

test('the People pane is read at most once a minute; the last list answers in between', () => {
    let now = 0;
    const guard = new PeoplePaneGuard(() => now);
    assert.equal(guard.next(), 'read');
    guard.noteRead(['Bo']);
    now += 10_000;
    assert.equal(guard.next(), 'cached');
    assert.deepEqual(guard.cached, ['Bo']);
    now += PEOPLE_READ_INTERVAL_MS;
    assert.equal(guard.next(), 'read');
    guard.noteRead(null);
    assert.equal(guard.cached, null, 'a reading that gave nothing is not answered with an older list');
    now += 1000;
    assert.equal(guard.next(), 'cached', 'a reading that gave nothing is not retried at once');
});

test('once disabled, the People pane is never opened again and no list is answered', () => {
    let now = 0;
    const guard = new PeoplePaneGuard(() => now);
    guard.noteRead(['Bo']);
    guard.disable();
    assert.equal(guard.disabled, true);
    assert.equal(guard.next(), 'off');
    assert.equal(guard.cached, null);
    now += 10 * PEOPLE_READ_INTERVAL_MS;
    assert.equal(guard.next(), 'off');
    guard.noteRead(['Cy']);
    assert.equal(guard.next(), 'off');
    assert.equal(guard.cached, null);
});
