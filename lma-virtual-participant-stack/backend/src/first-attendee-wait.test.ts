/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

import { strict as assert } from 'node:assert';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { test } from 'node:test';
import { fileURLToPath } from 'node:url';
import { AloneDebounce } from './alone-debounce.js';
import { FIRST_ATTENDEE_WAIT_MS, FirstAttendeeWait } from './first-attendee-wait.js';

const wait = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

function clock(start = 1_000_000) {
    let now = start;
    return { now: () => now, advance: (ms: number) => (now += ms) };
}

test('the first-attendee wait is five minutes', () => {
    assert.equal(FIRST_ATTENDEE_WAIT_MS, 5 * 60_000);
});

test('a VP that joined first may not leave as alone until the wait is over', () => {
    const c = clock();
    const firstAttendee = new FirstAttendeeWait(FIRST_ATTENDEE_WAIT_MS, c.now);
    firstAttendee.note(1);
    assert.equal(firstAttendee.mayLeaveAlone(), false);
    c.advance(60_000);
    assert.equal(firstAttendee.remainingMs(), 4 * 60_000);
    c.advance(4 * 60_000 - 1);
    assert.equal(firstAttendee.mayLeaveAlone(), false);
    c.advance(1);
    assert.equal(firstAttendee.mayLeaveAlone(), true);
});

test('once another attendee has been counted the VP may leave as alone at once', () => {
    const c = clock();
    const firstAttendee = new FirstAttendeeWait(FIRST_ATTENDEE_WAIT_MS, c.now);
    firstAttendee.note(3);
    firstAttendee.note(1); // they left again
    assert.equal(firstAttendee.othersSeen, true);
    assert.equal(firstAttendee.mayLeaveAlone(), true);
});

test('readings of one or fewer, and readings that are not numbers, do not count as somebody else', () => {
    const c = clock();
    const firstAttendee = new FirstAttendeeWait(FIRST_ATTENDEE_WAIT_MS, c.now);
    for (const count of [0, 1, -1, Number.NaN, Number.POSITIVE_INFINITY]) firstAttendee.note(count);
    assert.equal(firstAttendee.othersSeen, false);
});

test('the alone hold lasts until the first-attendee wait is over when nobody else has joined', async () => {
    let ended = 0;
    const debounce = new AloneDebounce(() => (ended += 1), 20, new FirstAttendeeWait(80));
    debounce.note(1);
    await wait(50);
    assert.equal(ended, 0, 'the 20 ms hold is over but the 80 ms wait is not');
    assert.equal(debounce.pending, true);
    await wait(70);
    assert.equal(ended, 1);
});

test('somebody joining during the wait ends it, and their leaving starts the ordinary hold', async () => {
    let ended = 0;
    const debounce = new AloneDebounce(() => (ended += 1), 20, new FirstAttendeeWait(10_000));
    debounce.note(1);
    debounce.note(2);
    assert.equal(debounce.pending, false);
    debounce.note(1);
    await wait(50);
    assert.equal(ended, 1);
});

test('every platform that leaves when it is alone waits for the first attendee', () => {
    const here = dirname(fileURLToPath(import.meta.url));
    // Tests run from dist/; the sources sit beside them in src/.
    const src = (name: string) => readFileSync(join(here, '..', 'src', name), 'utf8');
    for (const name of ['zoom.ts', 'chime.ts', 'teams.ts', 'google-meet.ts', 'zoom-sdk.ts', 'teams-sdk.ts']) {
        const source = src(name);
        assert.match(source, /new FirstAttendeeWait\(\)/, `${name} creates the wait`);
        assert.match(source, /firstAttendee\.note\(|AloneDebounce\([\s\S]*?firstAttendee\)/, `${name} records counts`);
    }
    for (const name of ['zoom.ts', 'teams.ts', 'google-meet.ts', 'zoom-sdk.ts', 'teams-sdk.ts']) {
        assert.match(src(name), /firstAttendee\.mayLeaveAlone\(\)/, `${name} consults the wait before leaving as alone`);
    }
    for (const name of ['zoom.ts', 'chime.ts']) {
        assert.match(src(name), /AloneDebounce\([\s\S]*?ALONE_HOLD_MS, firstAttendee\)/, `${name} gives the debounce the wait`);
    }
});
