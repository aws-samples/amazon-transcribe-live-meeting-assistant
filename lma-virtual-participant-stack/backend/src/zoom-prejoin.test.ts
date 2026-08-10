/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import { strict as assert } from 'node:assert';
import { test } from 'node:test';

/**
 * The URL predicate behind hasLeftPrejoin / isOffMeetingPage.
 *
 * Kept in lockstep with the regex in zoom.ts. A VP was reported ACTIVE while
 * sitting on app.zoom.us/signin behind a reCAPTCHA — it transcribed silence for
 * the entire meeting because "the prejoin controls are gone" was treated as
 * proof of a successful join. Leaving the web client is a FAILURE.
 */
const OFF_MEETING = /\/signin|\/oauth|\/postattendee|recaptcha/i;

test('a Zoom sign-in redirect is recognised as off-meeting', () => {
    // The exact URL observed in the failing run.
    assert.equal(
        OFF_MEETING.test(
            'https://app.zoom.us/signin?_x_zm_rtaid=QzlVIvKBRVuu1km0SXxo3g.1786371813576.81c62f&_x_zm_rhtaid=592',
        ),
        true,
    );
});

test('reCAPTCHA and other bail-out pages are off-meeting', () => {
    for (const url of [
        'https://www.google.com/recaptcha/enterprise/anchor?ar=1&k=abc',
        'https://app.zoom.us/oauth/authorize?client_id=x',
        'https://app.zoom.us/postattendee?meeting=123',
        'https://APP.ZOOM.US/SignIn',
    ]) {
        assert.equal(OFF_MEETING.test(url), true, `${url} should be off-meeting`);
    }
});

test('the meeting web client and prejoin are NOT off-meeting', () => {
    // These must keep working: a false positive here would break every join.
    for (const url of [
        'https://app.zoom.us/wc/6878620134/join?_x_zm_rtaid=abc',
        'https://app.zoom.us/wc/6878620134/start',
        'https://app.zoom.us/wc/join?x=1',
        'https://zoom.us/j/6878620134',
    ]) {
        assert.equal(OFF_MEETING.test(url), false, `${url} should NOT be off-meeting`);
    }
});
