/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * A Zoom join link given as the meeting ID is reduced to its meeting number,
 * and its pwd value becomes the password unless a passcode was given too. A
 * personal meeting link is resolved to its meeting number through the
 * redirect Zoom answers it with.
 */
import assert from 'node:assert/strict';
import test from 'node:test';

import { captureLogs, clearSecretValuesForTest } from './logger.js';
import {
    applyZoomPersonalLink,
    meetingFromZoomRedirect,
    parseZoomJoinLink,
    parseZoomPersonalLink,
    resolveZoomMeeting,
    resolveZoomPersonalLink,
    zoomSaysMeetingDoesNotExist,
    type ZoomInviteWithLink,
    type ZoomRedirectRequest,
} from './zoom-join-link.js';

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

// Personal meeting links. The Location values are shaped like the ones Zoom
// answered real requests with: the room's /j/<number> link, carrying the pwd
// value of the personal link when it had one, and two tracking parameters.
const PERSONAL = `https://acme.zoom.us/my/someone?pwd=${TOKEN}`;
const REDIRECT_WITH_PWD = `https://acme.zoom.us/j/6878620134?pwd=${TOKEN}&_x_zm_rtaid=AbC123dEf.1763040000000.0f1e2d3c&_x_zm_rhtaid=512`;
const REDIRECT_WITHOUT_PWD = 'https://zoom.us/j/6878620134?_x_zm_rtaid=AbC123dEf.1763040000000.0f1e2d3c&_x_zm_rhtaid=512';

/** A request that answers with the given status and Location, and records the URL it was asked for. */
const answering = (status: number, location: string | null, asked: string[] = []): ZoomRedirectRequest => async (url) => {
    asked.push(url);
    return { status, location };
};

test('a personal meeting link is read on any Zoom host, with or without pwd', () => {
    assert.deepEqual(parseZoomPersonalLink(` ${PERSONAL} `), {
        url: PERSONAL,
        host: 'acme.zoom.us',
        name: 'someone',
        pwd: TOKEN,
    });
    assert.deepEqual(parseZoomPersonalLink('https://zoom.us/my/someone/'), {
        url: 'https://zoom.us/my/someone/',
        host: 'zoom.us',
        name: 'someone',
    });
    for (const value of [
        'https://zoom.us/my/',
        'https://zoom.us/my/a/b',
        'https://zoom.us.example.com/my/someone',
        'https://zoom.us/j/81234567890',
        '81234567890',
        '',
        undefined,
    ]) {
        assert.equal(parseZoomPersonalLink(value), null, String(value));
    }
});

test('the meeting is read from a /j/<number> redirect on a Zoom host only', () => {
    assert.deepEqual(meetingFromZoomRedirect(REDIRECT_WITH_PWD, PERSONAL), { meetingId: '6878620134', pwd: TOKEN });
    assert.deepEqual(meetingFromZoomRedirect(REDIRECT_WITHOUT_PWD, 'https://zoom.us/my/someone'), {
        meetingId: '6878620134',
    });
    // A relative Location is read against the personal link.
    assert.deepEqual(meetingFromZoomRedirect('/j/6878620134', PERSONAL), { meetingId: '6878620134' });
    for (const location of [
        null,
        '',
        'https://acme.zoom.us/signin',
        'https://acme.zoom.us/my/someone',
        'https://zoom.us/wc/6878620134/join',
        'https://example.com/j/6878620134',
        'javascript:alert(1)',
    ]) {
        assert.equal(meetingFromZoomRedirect(location, PERSONAL), null, String(location));
    }
});

test('a personal link resolves to the meeting number with one request for the link itself', async () => {
    const asked: string[] = [];
    const link = parseZoomPersonalLink(PERSONAL)!;
    assert.deepEqual(await resolveZoomPersonalLink(link, '', answering(302, REDIRECT_WITH_PWD, asked)), {
        meetingId: '6878620134',
        meetingPassword: TOKEN,
        redirectPwd: TOKEN,
    });
    assert.deepEqual(asked, [PERSONAL]);
});

test('the password is the given passcode, else the link pwd, else the redirect pwd', async () => {
    const withPwd = parseZoomPersonalLink(PERSONAL)!;
    const withoutPwd = parseZoomPersonalLink('https://acme.zoom.us/my/someone')!;
    const otherToken = 'Zz9Yy8Xx7Ww6Vv5Uu4Tt3Ss2Rr1Qq0.1';
    const redirectWith = (pwd: string) => `https://acme.zoom.us/j/6878620134?pwd=${pwd}&_x_zm_rhtaid=512`;

    assert.equal(
        ((await resolveZoomPersonalLink(withPwd, '02061967', answering(302, REDIRECT_WITH_PWD))) as { meetingPassword: string })
            .meetingPassword,
        '02061967',
    );
    assert.equal(
        ((await resolveZoomPersonalLink(withPwd, '', answering(302, redirectWith(otherToken)))) as { meetingPassword: string })
            .meetingPassword,
        TOKEN,
    );
    assert.deepEqual(await resolveZoomPersonalLink(withoutPwd, '', answering(302, redirectWith(otherToken))), {
        meetingId: '6878620134',
        meetingPassword: otherToken,
        redirectPwd: otherToken,
    });
    assert.deepEqual(await resolveZoomPersonalLink(withoutPwd, '', answering(302, REDIRECT_WITHOUT_PWD)), {
        meetingId: '6878620134',
        meetingPassword: '',
    });
});

test('a personal link that does not redirect to a meeting, or whose request fails, does not resolve', async () => {
    const link = parseZoomPersonalLink(PERSONAL)!;
    assert.deepEqual(await resolveZoomPersonalLink(link, '', answering(200, null)), { failure: 'no-meeting', status: 200 });
    assert.deepEqual(await resolveZoomPersonalLink(link, '', answering(404, REDIRECT_WITH_PWD)), {
        failure: 'no-meeting',
        status: 404,
    });
    assert.deepEqual(await resolveZoomPersonalLink(link, '', answering(302, 'https://acme.zoom.us/signin')), {
        failure: 'no-meeting',
        status: 302,
    });
    const failing: ZoomRedirectRequest = async () => {
        throw new DOMException('The operation was aborted due to timeout', 'TimeoutError');
    };
    assert.deepEqual(await resolveZoomPersonalLink(link, '', failing), {
        failure: 'request-failed',
        detail: 'TimeoutError: The operation was aborted due to timeout',
    });
});

test('applying a personal link fills in the invite and logs only the host and meeting number', async () => {
    const invite: ZoomInviteWithLink = {
        meetingId: PERSONAL,
        meetingPassword: TOKEN,
        zoomPersonalLink: parseZoomPersonalLink(PERSONAL)!,
    };
    const captured = captureLogs();
    let ok: boolean;
    try {
        ok = await applyZoomPersonalLink(invite, answering(302, REDIRECT_WITH_PWD));
    } finally {
        captured.restore();
        clearSecretValuesForTest();
    }
    assert.equal(ok, true);
    assert.deepEqual(invite, { meetingId: '6878620134', meetingPassword: TOKEN });
    const lines = captured.messages().join('\n');
    assert.match(lines, /acme\.zoom\.us resolved to meeting 6878620134/);
    assert.doesNotMatch(lines, /_x_zm_|pwd|someone|Ab1Cd2/);
});

test('a personal link that does not resolve leaves the invite alone and says why without the link', async () => {
    const invite: ZoomInviteWithLink = { meetingId: PERSONAL, zoomPersonalLink: parseZoomPersonalLink(PERSONAL)! };
    const captured = captureLogs();
    let ok: boolean;
    try {
        ok = await applyZoomPersonalLink(invite, answering(200, null));
    } finally {
        captured.restore();
    }
    assert.equal(ok, false);
    assert.equal(invite.meetingId, PERSONAL);
    const lines = captured.messages().join('\n');
    assert.match(lines, /acme\.zoom\.us did not resolve: Zoom answered HTTP 200/);
    assert.doesNotMatch(lines, /pwd|someone|Ab1Cd2/);
});

test('an invite without a personal link is left as it is', async () => {
    const invite: ZoomInviteWithLink = { meetingId: '81234567890', meetingPassword: '02061967' };
    const request: ZoomRedirectRequest = async () => {
        throw new Error('no request expected');
    };
    assert.equal(await applyZoomPersonalLink(invite, request), true);
    assert.deepEqual(invite, { meetingId: '81234567890', meetingPassword: '02061967' });
});

test("Zoom's meeting-does-not-exist messages are recognised, and an ordinary join screen is not", () => {
    for (const text of [
        'Invalid meeting ID. Please check and try again.',
        'This meeting ID is not valid. Please check and try again.',
        'Meeting does not exist: 6878620134',
        'This meeting link is invalid (3,001)',
    ]) {
        assert.equal(zoomSaysMeetingDoesNotExist(text), true, text);
    }
    for (const text of [
        'Enter Meeting Info\nYour Name\nJoin',
        'Please wait, the meeting host will let you in soon.',
        'Meeting Passcode\nEnter the meeting ID',
        '',
        undefined,
    ]) {
        assert.equal(zoomSaysMeetingDoesNotExist(text), false, String(text));
    }
});

test('a personal meeting link given with http is requested over https', () => {
    const link = parseZoomPersonalLink('http://example.zoom.us/my/someone?pwd=abc123abc123abc123abc123abc123.1');
    assert.ok(link);
    assert.ok(link.url.startsWith('https://example.zoom.us/my/someone?pwd='), link.url);
});
