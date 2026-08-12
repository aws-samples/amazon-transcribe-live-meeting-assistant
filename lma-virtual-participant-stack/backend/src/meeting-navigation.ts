/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Navigation helper for meeting-platform pages (GitHub #547).
 *
 * Every platform handler used a bare `page.goto(url)`, which inherits two
 * Playwright defaults that are both wrong for a meeting web client:
 *
 *   1. `waitUntil: 'load'` — waits for EVERY subresource. A meeting SPA pulls in
 *      analytics, ad and telemetry frames (doubleclick, company-target, …) that
 *      may never settle, so `load` can be unreachable even though the app is
 *      running.
 *   2. the page default timeout, set to 20s in index.ts — a sensible guard for
 *      in-meeting selector lookups, far too short for the initial app load.
 *
 * A live Zoom join failed on exactly this:
 *
 *     14:03:19  Getting Zoom meeting link.
 *     14:03:19  [meeting-page] navigated -> https://app.zoom.us/wc/975.../join
 *     14:03:39  Meeting join failed: page.goto: Timeout 20000ms exceeded
 *               (waiting until "load")
 *
 * The page had ALREADY arrived at the meeting URL when the timeout fired 20s
 * later. The navigation worked; only the wait condition failed.
 *
 * Why this surfaced now: the same defect was fixed for the Zoom SIGN-IN
 * navigation (gotoZoomSignin), and sign-in on a cold profile takes ~51s. Before
 * that fix, slow sessions failed AT sign-in and never reached the join. Fixing
 * the first 20s timeout in the chain made the next one reachable — so this is a
 * pre-existing bug that a previous fix uncovered rather than caused.
 *
 * `domcontentloaded` is the right condition: the handlers immediately proceed to
 * poll for UI elements with their own retries and AI fallback, so they do not need
 * a fully-quiesced page — they need the document.
 */
import type { Page } from 'playwright-core';

/**
 * Budget for the initial meeting-page load. Deliberately generous: the cost of
 * being too low is a failed meeting, and the cost of being too high is only a
 * slower failure on a genuinely broken navigation.
 */
export const MEETING_NAV_TIMEOUT_MS = 60_000;

/**
 * True when `current` looks like it belongs to `expectedHostPattern`.
 *
 * Pure and exported so the "did we actually arrive?" rule is unit-testable — the
 * whole point of this helper is that a timeout must not discard a navigation that
 * succeeded.
 */
export function arrivedAtExpectedHost(current: string, expectedHostPattern: RegExp): boolean {
    if (!current || current === 'about:blank') return false;
    return expectedHostPattern.test(current);
}

/**
 * Navigate to a meeting page, tolerating a timeout that still arrived.
 *
 * Throws only when the page genuinely did not reach the expected host — so a real
 * DNS/network failure still fails fast and loudly rather than proceeding into a
 * join that cannot work.
 *
 * @param expectedHostPattern matched against the settled URL. Should accept the
 *   platform's redirect targets too (e.g. Zoom sends zoom.us -> app.zoom.us).
 */
export async function gotoMeetingPage(
    page: Page,
    url: string,
    expectedHostPattern: RegExp,
    label = 'meeting-page',
): Promise<string> {
    try {
        await page.goto(url, {
            waitUntil: 'domcontentloaded',
            timeout: MEETING_NAV_TIMEOUT_MS,
        });
    } catch (err) {
        const current = page.url();
        if (arrivedAtExpectedHost(current, expectedHostPattern)) {
            console.warn(
                `[${label}] ${url} reported "${String(err).split('\n')[0]}" but the page ` +
                    `settled at ${current} — continuing.`,
            );
            return current;
        }
        throw err;
    }
    return page.url();
}

/** Host patterns per platform, including the redirect targets each one uses. */
export const MEETING_HOST_PATTERNS = {
    // Zoom redirects zoom.us/wc/<id>/join -> app.zoom.us/wc/<id>/join.
    zoom: /https:\/\/([a-z0-9-]+\.)*zoom\.us\//,
    // Teams uses teams.microsoft.com and teams.live.com (anonymous join).
    teams: /https:\/\/teams\.(microsoft|live)\.com\//,
    webex: /https:\/\/([a-z0-9-]+\.)*webex\.com\//,
    chime: /https:\/\/([a-z0-9-]+\.)*chime\.aws\//,
} as const;
