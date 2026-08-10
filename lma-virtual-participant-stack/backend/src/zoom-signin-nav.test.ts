/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import { strict as assert } from 'node:assert';
import { test } from 'node:test';
import type { Page } from 'playwright-core';
import { gotoZoomSignin, SIGNIN_NAV_TIMEOUT_MS } from './zoom-login.js';

/**
 * Minimal Page stand-in: records the goto options and reports a settled URL.
 */
function fakePage(opts: { throwOn?: Error; settlesAt: string }): {
    page: Page;
    gotos: Array<{ url: string; options?: { timeout?: number; waitUntil?: string } }>;
} {
    const gotos: Array<{ url: string; options?: { timeout?: number; waitUntil?: string } }> = [];
    const page = {
        async goto(url: string, options?: { timeout?: number; waitUntil?: string }) {
            gotos.push({ url, options });
            if (opts.throwOn) throw opts.throwOn;
            return null;
        },
        url() {
            return opts.settlesAt;
        },
    } as unknown as Page;
    return { page, gotos };
}

test('the sign-in navigation gets a budget far above the 20s page default', () => {
    // index.ts sets page.setDefaultTimeout(20_000), which also governs page.goto.
    // zoom.us/signin is the full marketing shell plus analytics subframes, and a
    // live MicroVM join failed with "Timeout 20000ms exceeded" while Chromium
    // logged "Slow network is detected" — after which the page loaded anyway.
    assert.ok(
        SIGNIN_NAV_TIMEOUT_MS >= 60_000,
        `expected >= 60s for the sign-in page, got ${SIGNIN_NAV_TIMEOUT_MS}`,
    );
});

test('gotoZoomSignin passes its own explicit timeout rather than inheriting 20s', async () => {
    const { page, gotos } = fakePage({ settlesAt: 'https://zoom.us/signin' });
    await gotoZoomSignin(page);
    assert.equal(gotos.length, 1);
    assert.equal(gotos[0].url, 'https://zoom.us/signin');
    assert.equal(gotos[0].options?.timeout, SIGNIN_NAV_TIMEOUT_MS);
    assert.equal(gotos[0].options?.waitUntil, 'domcontentloaded');
});

test('a timeout is tolerated when the page actually reached Zoom', async () => {
    // The exact observed failure: goto threw, yet the very next log line was
    // "[meeting-page] navigated → https://zoom.us/myhome". Treating that as
    // fatal discarded a working signed-in session and failed the meeting.
    const { page } = fakePage({
        throwOn: new Error('page.goto: Timeout 20000ms exceeded.'),
        settlesAt: 'https://zoom.us/myhome',
    });
    assert.equal(await gotoZoomSignin(page), 'https://zoom.us/myhome');
});

test('a late-rendering /signin page is also tolerated', async () => {
    const { page } = fakePage({
        throwOn: new Error('page.goto: Timeout exceeded.'),
        settlesAt: 'https://zoom.us/signin',
    });
    assert.equal(await gotoZoomSignin(page), 'https://zoom.us/signin');
});

test('app.zoom.us counts as having arrived', async () => {
    const { page } = fakePage({
        throwOn: new Error('page.goto: Timeout exceeded.'),
        settlesAt: 'https://app.zoom.us/wc/123/join',
    });
    assert.equal(await gotoZoomSignin(page), 'https://app.zoom.us/wc/123/join');
});

test('a genuine navigation failure still throws', async () => {
    // If we never reached Zoom at all (DNS/proxy failure, about:blank), the
    // caller must fail rather than proceed as if signed in — silently
    // continuing would degrade to a guest join, which Zoom bot-blocks.
    const { page } = fakePage({
        throwOn: new Error('page.goto: net::ERR_NAME_NOT_RESOLVED'),
        settlesAt: 'about:blank',
    });
    await assert.rejects(() => gotoZoomSignin(page), /ERR_NAME_NOT_RESOLVED/);
});

test('a non-Zoom destination is treated as failure', async () => {
    const { page } = fakePage({
        throwOn: new Error('page.goto: Timeout exceeded.'),
        settlesAt: 'https://evil.example.com/phish',
    });
    await assert.rejects(() => gotoZoomSignin(page), /Timeout/);
});
