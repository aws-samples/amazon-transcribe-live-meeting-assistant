/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

import assert from 'node:assert/strict';
import test from 'node:test';

import { sanitizeTeamsDisplayName } from './teams.js';

// Teams' pre-join name field allows exactly: letters, numbers, spaces,
// and - ' . _ @ — the earlier default identity template `LMA ({LMA_USER})`
// fails it on the parentheses and the join is blocked at the name screen. The
// default is now `LMA - {LMA_USER}`, which Teams accepts as it is, so the name
// shown on Teams is the configured one; what is still cleaned is the user name
// filled in for {LMA_USER}, and a name a profile saved under the earlier default.

test('the default display name is shown on Teams unchanged', () => {
    for (const name of ['LMA - Bob Strahan', 'LMA - strahanr@example.com', "LMA - Mary O'Brien", 'LMA Notetaker - a.b_c']) {
        assert.equal(sanitizeTeamsDisplayName(name), name);
    }
});

test('a user name Teams would refuse is cleaned inside the default', () => {
    assert.equal(sanitizeTeamsDisplayName('LMA - Strahan, Bob'), 'LMA - Strahan Bob');
    assert.equal(sanitizeTeamsDisplayName('LMA - José Müller'), 'LMA - Jose Muller');
});

test('the earlier parenthesised default becomes a form Teams accepts', () => {
    assert.equal(
        sanitizeTeamsDisplayName('LMA (jeremykf@amazon.com)'),
        'LMA - jeremykf@amazon.com',
    );
});

test('a name Teams already accepts passes through unchanged', () => {
    assert.equal(
        sanitizeTeamsDisplayName("Mary O'Brien-Smith. jr_2 @home"),
        "Mary O'Brien-Smith. jr_2 @home",
    );
});

test('every character outside the allowed set is removed', () => {
    assert.equal(sanitizeTeamsDisplayName('LMA <bot> #1 [test]!'), 'LMA bot 1 test');
});

test('whitespace never doubles up after removals', () => {
    assert.equal(sanitizeTeamsDisplayName('LMA   (  user  )  '), 'LMA - user');
});

test('a name that sanitizes to nothing falls back to LMA', () => {
    assert.equal(sanitizeTeamsDisplayName('()[]<>#!'), 'LMA');
});
