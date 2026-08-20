/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Regression tests for GitHub #547 — meeting-page navigation timeouts.
 *
 * A live Zoom join failed like this:
 *
 *     14:03:19  Getting Zoom meeting link.
 *     14:03:19  [meeting-page] navigated -> https://app.zoom.us/wc/975.../join
 *     14:03:39  Meeting join failed: page.goto: Timeout 20000ms exceeded
 *               (waiting until "load")
 *
 * The page had ALREADY arrived when the timeout fired 20s later. A bare
 * `page.goto(url)` inherits `waitUntil: 'load'` — which waits for every analytics
 * and ad subframe a meeting SPA pulls in — plus the 20s page default from
 * index.ts, which exists for in-meeting selector lookups, not app loads.
 *
 * Note this was uncovered rather than caused by an earlier fix: the same defect
 * was fixed for the Zoom SIGN-IN navigation, and sign-in takes ~51s on a cold
 * profile. Before that, slow sessions failed at sign-in and never reached the
 * join, so fixing the first 20s timeout in the chain made the next one reachable.
 */
import { strict as assert } from 'node:assert';
import { test } from 'node:test';
import { readFileSync } from 'node:fs';
import {
    arrivedAtExpectedHost,
    MEETING_HOST_PATTERNS,
    MEETING_NAV_TIMEOUT_MS,
} from './meeting-navigation.js';

test('the navigation budget is far above the 20s page default', () => {
    // 20s was not enough for a meeting SPA on a 2-vCPU host; the cost of being
    // generous is only a slower failure on a genuinely broken navigation.
    assert.ok(
        MEETING_NAV_TIMEOUT_MS >= 60_000,
        `expected >= 60s, got ${MEETING_NAV_TIMEOUT_MS}`,
    );
});

test('the exact URL from the failing join is recognised as arrived', () => {
    // Verbatim from the incident: Zoom redirects zoom.us -> app.zoom.us, so the
    // pattern must accept the redirect target or the fix does nothing.
    assert.equal(
        arrivedAtExpectedHost('https://app.zoom.us/wc/97500657921/join', MEETING_HOST_PATTERNS.zoom),
        true,
    );
});

test('each platform pattern accepts its own hosts and redirect targets', () => {
    const cases: Array<[RegExp, string[]]> = [
        [MEETING_HOST_PATTERNS.zoom, ['https://zoom.us/wc/1/join', 'https://app.zoom.us/wc/1/join']],
        [
            MEETING_HOST_PATTERNS.teams,
            ['https://teams.microsoft.com/v2/', 'https://teams.live.com/meet/1'],
        ],
        [MEETING_HOST_PATTERNS.webex, ['https://signin.webex.com/join', 'https://acme.webex.com/x']],
        [MEETING_HOST_PATTERNS.chime, ['https://app.chime.aws/meetings/1']],
    ];
    for (const [pattern, urls] of cases) {
        for (const url of urls) {
            assert.equal(arrivedAtExpectedHost(url, pattern), true, `${url} should match`);
        }
    }
});

test('platform patterns do not accept each other', () => {
    // A cross-match would let a navigation that landed on the wrong platform be
    // treated as success, hiding a real failure.
    assert.equal(arrivedAtExpectedHost('https://teams.microsoft.com/v2/', MEETING_HOST_PATTERNS.zoom), false);
    assert.equal(arrivedAtExpectedHost('https://app.zoom.us/wc/1/join', MEETING_HOST_PATTERNS.teams), false);
});

test('about:blank and empty are NOT treated as arrived', () => {
    // This is what a genuinely failed navigation leaves behind; treating it as
    // success would march into a join that cannot work.
    for (const url of ['about:blank', '']) {
        assert.equal(arrivedAtExpectedHost(url, MEETING_HOST_PATTERNS.zoom), false);
    }
});

test('a lookalike host is rejected', () => {
    // zoom.us.evil.com must not satisfy the zoom pattern.
    assert.equal(
        arrivedAtExpectedHost('https://zoom.us.evil.com/wc/1/join', MEETING_HOST_PATTERNS.zoom),
        false,
    );
});

test('no platform handler still uses a bare page.goto for a meeting URL', () => {
    // The defect was spread across four handlers; this asserts the whole class is
    // fixed rather than just the Zoom instance that was reported.
    for (const file of ['zoom.ts', 'teams.ts', 'webex.ts', 'chime.ts']) {
        const src = readFileSync(new URL(`./${file}`, import.meta.url).pathname.replace('/dist/', '/src/'), 'utf8');
        const bare = src
            .split('\n')
            .map((l, i) => [i + 1, l] as [number, string])
            .filter(([, l]) => /await page\.goto\(/.test(l))
            .filter(([, l]) => !/timeout/.test(l));
        assert.equal(
            bare.length,
            0,
            `${file} still has a bare page.goto: ${bare.map(([n, l]) => `${n}: ${l.trim()}`).join(' | ')}`,
        );
    }
});
