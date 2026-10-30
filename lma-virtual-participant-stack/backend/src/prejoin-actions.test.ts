/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import { strict as assert } from 'node:assert';
import { test } from 'node:test';
import type { ElementHandle, Page } from 'playwright-core';
import { clickWithDomFallback, humanClick } from './prejoin-actions.js';

/** Minimal ElementHandle double: records click options and evaluate calls. */
function fakeHandle(opts: { clickError?: Error; evaluateError?: Error } = {}) {
    const calls = { click: [] as unknown[], evaluate: 0 };
    const handle = {
        click: async (o: unknown) => {
            calls.click.push(o);
            if (opts.clickError) throw opts.clickError;
        },
        evaluate: async () => {
            calls.evaluate += 1;
            if (opts.evaluateError) throw opts.evaluateError;
        },
    };
    return { handle: handle as unknown as ElementHandle<Element>, calls };
}

test('clickWithDomFallback uses only the positional click when it succeeds', async () => {
    const { handle, calls } = fakeHandle();
    await clickWithDomFallback(handle, 'Join');
    assert.equal(calls.click.length, 1);
    assert.equal(calls.evaluate, 0);
});

test('clickWithDomFallback bounds the positional click with a timeout', async () => {
    const { handle, calls } = fakeHandle();
    await clickWithDomFallback(handle, 'Join');
    const o = calls.click[0] as { timeout?: number };
    assert.ok(typeof o?.timeout === 'number' && o.timeout > 0 && o.timeout <= 10_000);
});

test('clickWithDomFallback falls back to a DOM click when the positional click throws', async () => {
    const { handle, calls } = fakeHandle({ clickError: new Error('strict mode violation') });
    await clickWithDomFallback(handle, 'Join');
    assert.equal(calls.evaluate, 1);
});

test('clickWithDomFallback rethrows the positional error when the DOM click also fails', async () => {
    const original = new Error('element is not visible');
    const { handle } = fakeHandle({ clickError: original, evaluateError: new Error('detached') });
    await assert.rejects(clickWithDomFallback(handle, 'Join'), (e) => e === original);
});

test('humanClick on a handle force-clicks with a bounded timeout, then falls back to a DOM click', async () => {
    const { handle, calls } = fakeHandle({ clickError: new Error('Timeout 5000ms exceeded') });
    await humanClick({} as Page, handle);
    const o = calls.click[0] as { force?: boolean; timeout?: number };
    assert.equal(o.force, true);
    assert.ok(typeof o.timeout === 'number' && o.timeout > 0);
    assert.equal(calls.evaluate, 1);
});
