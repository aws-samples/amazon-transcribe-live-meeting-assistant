/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * A Zoom join link given as the meeting ID is reduced to its meeting number,
 * and its pwd value becomes the password unless a passcode was given too.
 */
import assert from 'node:assert/strict';
import test from 'node:test';

import { parseZoomJoinLink, resolveZoomMeeting } from './zoom-join-link.js';

const TOKEN = 'Ab1Cd2Ef3Gh4Ij5Kl6Mn7Op8Qr9St0.1';

test('the invitation link form is read, with or without a subdomain and pwd', () => {
    assert.deepEqual(parseZoomJoinLink(`https://us05web.zoom.us/j/81234567890?pwd=${TOKEN}`), {
        meetingId: '81234567890',
        pwd: TOKEN,
    });
    assert.deepEqual(parseZoomJoinLink('https://zoom.us/j/81234567890'), { meetingId: '81234567890' });
    assert.deepEqual(parseZoomJoinLink(` https://acme.zoom.us/j/81234567890/?uname=x&pwd=${TOKEN} `), {
        meetingId: '81234567890',
        pwd: TOKEN,
    });
});

test('the web client link forms are read', () => {
    assert.deepEqual(parseZoomJoinLink(`https://app.zoom.us/wc/81234567890/join?pwd=${TOKEN}`), {
        meetingId: '81234567890',
        pwd: TOKEN,
    });
    assert.deepEqual(parseZoomJoinLink('https://zoom.us/wc/join/81234567890'), { meetingId: '81234567890' });
});

test('values that are not a readable Zoom link are not read', () => {
    assert.equal(parseZoomJoinLink('81234567890'), null);
    assert.equal(parseZoomJoinLink(''), null);
    assert.equal(parseZoomJoinLink(undefined), null);
    assert.equal(parseZoomJoinLink('https://zoom.us/my/someone'), null);
    assert.equal(parseZoomJoinLink('https://zoom.us.example.com/j/81234567890'), null);
    assert.equal(parseZoomJoinLink('https://teams.microsoft.com/meet/123?p=abc'), null);
    assert.equal(parseZoomJoinLink('https://zoom.us/j/not-a-number'), null);
});

test('a link gives its number and pwd value; a separately given passcode wins', () => {
    const link = `https://us05web.zoom.us/j/81234567890?pwd=${TOKEN}`;
    assert.deepEqual(resolveZoomMeeting(link, ''), { meetingId: '81234567890', meetingPassword: TOKEN });
    assert.deepEqual(resolveZoomMeeting(link, '02061967'), { meetingId: '81234567890', meetingPassword: '02061967' });
    // A cut-off token given alongside the link gives way to the link's own.
    assert.deepEqual(resolveZoomMeeting(link, TOKEN.slice(0, 20)), {
        meetingId: '81234567890',
        meetingPassword: TOKEN,
    });
    assert.deepEqual(resolveZoomMeeting('https://zoom.us/j/81234567890', '02061967'), {
        meetingId: '81234567890',
        meetingPassword: '02061967',
    });
});

test('a meeting ID that is not a Zoom link is unchanged', () => {
    assert.deepEqual(resolveZoomMeeting('81234567890', '02061967'), {
        meetingId: '81234567890',
        meetingPassword: '02061967',
    });
});
