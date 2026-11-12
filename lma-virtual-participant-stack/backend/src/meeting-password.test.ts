/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The meeting password the Virtual Participant enters: the same rule as the
 * invitation parser's normalize_meeting_password, applied to every way a
 * password reaches it.
 */
import assert from 'node:assert/strict';
import test from 'node:test';

import { normalizeMeetingPassword } from './meeting-password.js';

test('a numeric Zoom passcode loses its grouping spaces, whatever the platform case', () => {
    assert.equal(normalizeMeetingPassword('1234 5678', 'ZOOM'), '12345678');
    assert.equal(normalizeMeetingPassword(' 123 456\t789 ', 'Zoom'), '123456789');
    assert.equal(normalizeMeetingPassword('12345678', 'ZOOM'), '12345678');
});

test('an alphanumeric Zoom passcode keeps its inner characters', () => {
    assert.equal(normalizeMeetingPassword(' aB3 x9 ', 'ZOOM'), 'aB3 x9');
});

test('other platforms are trimmed only', () => {
    assert.equal(normalizeMeetingPassword(' 1234 5678 ', 'WEBEX'), '1234 5678');
    assert.equal(normalizeMeetingPassword(' Xy7 ', 'TEAMS'), 'Xy7');
});

test('a missing or blank password is empty', () => {
    assert.equal(normalizeMeetingPassword(undefined, 'ZOOM'), '');
    assert.equal(normalizeMeetingPassword(null, 'ZOOM'), '');
    assert.equal(normalizeMeetingPassword('   ', 'ZOOM'), '');
    assert.equal(normalizeMeetingPassword('', undefined), '');
});
