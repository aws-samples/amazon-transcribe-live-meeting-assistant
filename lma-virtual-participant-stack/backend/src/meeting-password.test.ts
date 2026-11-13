/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The meeting password the Virtual Participant enters: the rule of the
 * invitation parser's normalize_meeting_password (which also maps an empty
 * result to None and matches only "ZOOM"), applied to every way a
 * password reaches it.
 */
import assert from 'node:assert/strict';
import test from 'node:test';

import {
    normalizeMeetingPassword,
    ZOOM_PASSCODE_MAX_LENGTH,
    zoomPasswordKind,
    zoomWebJoinUrl,
} from './meeting-password.js';

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

test('a Zoom passcode of up to ten characters is a passcode', () => {
    assert.equal(ZOOM_PASSCODE_MAX_LENGTH, 10);
    assert.equal(zoomPasswordKind('02061967'), 'passcode');
    assert.equal(zoomPasswordKind('aB3x9'), 'passcode');
    assert.equal(zoomPasswordKind('ab@-_*1234'), 'passcode');
    assert.equal(zoomPasswordKind(''), 'passcode');
    assert.equal(zoomPasswordKind(undefined), 'passcode');
    assert.equal(zoomPasswordKind(null), 'passcode');
});

test('the pwd value of a Zoom link is a link token, whole or truncated', () => {
    assert.equal(zoomPasswordKind('Ab1Cd2Ef3Gh4Ij5Kl6Mn7Op8Qr9St0.1'), 'link-token');
    assert.equal(zoomPasswordKind('Ab1Cd2Ef3Gh4Ij5Kl6Mn'), 'link-token');
    assert.equal(zoomPasswordKind('abcdefghijk'), 'link-token');
    assert.equal(zoomPasswordKind('Ab1Cd.1'), 'link-token');
});

test('the Zoom web join URL carries a link token as pwd and never a passcode', () => {
    assert.equal(zoomWebJoinUrl('123456789', '02061967'), 'https://zoom.us/wc/123456789/join');
    assert.equal(zoomWebJoinUrl('123456789', ''), 'https://zoom.us/wc/123456789/join');
    assert.equal(zoomWebJoinUrl('123456789', undefined), 'https://zoom.us/wc/123456789/join');
    assert.equal(
        zoomWebJoinUrl('123456789', 'Ab1Cd2Ef3Gh4Ij5Kl6Mn7Op8Qr9St0.1'),
        'https://zoom.us/wc/123456789/join?pwd=Ab1Cd2Ef3Gh4Ij5Kl6Mn7Op8Qr9St0.1',
    );
    assert.equal(zoomWebJoinUrl('123', 'a+b/c=d&e?f.1'), 'https://zoom.us/wc/123/join?pwd=a%2Bb%2Fc%3Dd%26e%3Ff.1');
});
