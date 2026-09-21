/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Guards on the Webex DOM selectors.
 *
 * `tsc` cannot check a selector string, and Webex reshuffles its pre-join and
 * chat markup often enough that these strings are edited more than any other part
 * of webex.ts. Two failure modes have real history behind them:
 *
 *  - A malformed selector does not time out, it THROWS on sight — and an invalid
 *    alternative anywhere in a comma list poisons the whole selector, so it throws
 *    even when the other alternative is present on the page. That surfaces to the
 *    user as "Meeting join failed: Unexpected token ..." rather than as a missing
 *    field, on whichever code path happens to evaluate it.
 *  - Playwright removed the `>>>` shadow-piercing combinator. It now parses as
 *    `>>` plus a `:scope > input` CSS part, which matches only a direct child of
 *    a shadow root — so it silently fails to reach a nested input inside a web
 *    component, where the plain descendant form works (Playwright's CSS engine
 *    pierces open shadow roots by itself).
 *
 * These tests are string-level and need no browser, which is the point: they run
 * in the same `node --test` pass as everything else.
 */
import { strict as assert } from 'node:assert';
import { test } from 'node:test';
import { readFileSync } from 'node:fs';
import { NAME_INPUT, WEBEX_CHAT_SELECTORS } from './webex.js';

/** Every single- or double-quoted string literal in webex.ts. */
const sourceLiterals = (): string[] => {
    const src = readFileSync(new URL('../src/webex.ts', import.meta.url), 'utf8');
    return [...src.matchAll(/'([^'\n]*)'|"([^"\n]*)"/g)].map((m) => m[1] ?? m[2]);
};

/** Literals that look like CSS selectors rather than prose, ids or messages. */
const selectorLiterals = (): string[] =>
    sourceLiterals().filter(
        (lit) =>
            /\[[a-zA-Z-]+[\^~|*$]?=/.test(lit) || // an attribute selector
            /^[.#][a-zA-Z][\w-]*$/.test(lit), // a bare class or id selector
    );

const balanced = (selector: string, open: string, close: string): boolean => {
    let depth = 0;
    for (const ch of selector) {
        if (ch === open) depth += 1;
        if (ch === close) depth -= 1;
        if (depth < 0) return false;
    }
    return depth === 0;
};

test('no selector in webex.ts uses the removed >>> combinator', () => {
    for (const selector of selectorLiterals()) {
        assert.ok(
            !selector.includes('>>>'),
            `"${selector}" uses >>>, which no longer pierces shadow roots — use a descendant selector`,
        );
    }
});

test('every selector in webex.ts has balanced brackets and quotes', () => {
    // The literal defect this catches: a selector copied out of a bug report with
    // a stray `"]` on the end, which threw InvalidSelectorError on every path that
    // evaluated it.
    for (const selector of selectorLiterals()) {
        assert.ok(balanced(selector, '[', ']'), `"${selector}" has unbalanced [ ]`);
        assert.ok(balanced(selector, '(', ')'), `"${selector}" has unbalanced ( )`);
        assert.equal(
            (selector.match(/"/g) ?? []).length % 2,
            0,
            `"${selector}" has an odd number of double quotes`,
        );
    }
});

test('no selector alternative is left empty by a stray comma', () => {
    // 'a, , b' parses as an error, and a trailing comma is an easy edit to make
    // when adding a markup variant.
    for (const selector of selectorLiterals()) {
        if (!selector.includes(',')) continue;
        for (const part of selector.split(',')) {
            assert.notEqual(part.trim(), '', `"${selector}" has an empty alternative`);
        }
    }
});

test('the name field selector covers both Webex markups', () => {
    // Webex serves a plain input on the classic client and an <mdc-input> custom
    // element on newer builds; the VP has to join either.
    assert.ok(NAME_INPUT.includes('input[data-test="Name (required)"]'), 'classic markup missing');
    assert.ok(NAME_INPUT.includes('mdc-input[data-test="Name"]'), 'Momentum markup missing');
});

test('the name field selector resolves the inner input, not the custom element', () => {
    // fill() rejects the mdc-input host with "Element is not an <input> ...", so
    // the selector must descend into the shadow root's input.
    const momentum = NAME_INPUT.split(',')
        .map((part) => part.trim())
        .find((part) => part.startsWith('mdc-input'));
    assert.ok(momentum, 'no Momentum alternative found');
    assert.match(momentum, /mdc-input\[[^\]]+\]\s+input$/, `"${momentum}" must end at a descendant input`);
});

test('the chat container selectors are separate, not one comma list', () => {
    // A selector list resolves by document order, so a single list cannot express
    // "prefer the current markup over the legacy one" — they have to be queried
    // in order of preference instead.
    assert.ok(!WEBEX_CHAT_SELECTORS.containers.current.includes(','));
    assert.ok(!WEBEX_CHAT_SELECTORS.containers.legacy.includes(','));
    assert.notEqual(
        WEBEX_CHAT_SELECTORS.containers.current,
        WEBEX_CHAT_SELECTORS.containers.legacy,
    );
});

test('the chat selectors cover both Webex markups', () => {
    const { containers, senders, bodies, rows } = WEBEX_CHAT_SELECTORS;
    assert.ok(containers.current.includes('mdc-list'), 'current chat list missing');
    assert.ok(containers.legacy.includes('style-chat-box'), 'legacy chat list missing');
    for (const selector of [senders, bodies, rows]) {
        const parts = selector.split(',').map((p) => p.trim());
        assert.ok(parts.length >= 2, `"${selector}" should cover both markups`);
        assert.ok(
            parts.some((p) => p.includes('style-chat') || p.includes('activity')),
            `"${selector}" covers neither markup`,
        );
    }
});

test('the chat container selector does not require a direct child', () => {
    // '#activity-list > mdc-list' returns null the moment Webex wraps the list one
    // level deeper; the observer uses subtree:true anyway, so the descendant form
    // costs nothing and survives a layer of DOM churn.
    assert.ok(!WEBEX_CHAT_SELECTORS.containers.current.includes('>'));
});
