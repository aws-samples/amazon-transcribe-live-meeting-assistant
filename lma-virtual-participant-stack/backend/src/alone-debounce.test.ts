/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

import { strict as assert } from 'node:assert';
import { test } from 'node:test';
import { AloneDebounce } from './alone-debounce.js';

const wait = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

test('a single reading of one attendee does not end the meeting; one sustained for the hold does', async () => {
    let ended = 0;
    const debounce = new AloneDebounce(() => (ended += 1), 40);
    debounce.note(1);
    assert.equal(debounce.pending, true);
    await wait(10);
    debounce.note(0); // a further low reading does not restart the hold
    await wait(60);
    assert.equal(ended, 1);
    assert.equal(debounce.pending, false);
});

test('a reading above one during the hold keeps the VP in the meeting', async () => {
    let ended = 0;
    const debounce = new AloneDebounce(() => (ended += 1), 40);
    debounce.note(1); // the counter re-rendered empty for a moment
    await wait(10);
    debounce.note(4);
    await wait(60);
    assert.equal(ended, 0);
    assert.equal(debounce.pending, false);
});

test('a reading that is not a number is ignored, and cancel stops a pending exit', async () => {
    let ended = 0;
    const debounce = new AloneDebounce(() => (ended += 1), 30);
    debounce.note(Number.NaN);
    assert.equal(debounce.pending, false);
    debounce.note(1);
    debounce.cancel();
    await wait(50);
    assert.equal(ended, 0);
});
