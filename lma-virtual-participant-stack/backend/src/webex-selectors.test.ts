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
 * in the same `node --test` pass as everything else. They are a heuristic, not a CSS
 * validator: they catch the malformations that have actually occurred here — a
 * stray bracket or quote, `>>>`, an empty alternative, a child combinator where a
 * descendant was meant — but a genuinely invalid construct such as `[data-test=]`
 * or `div > > span` needs a real parser and will pass.
 */
import { strict as assert } from 'node:assert';
import { test } from 'node:test';
import { readFileSync } from 'node:fs';
import {
    NAME_INPUT,
    WEBEX_CHAT_SELECTORS,
    isOwnWebexMessage,
    isOwnWebexSender,
    normalizeWebexSender,
} from './webex.js';
import { details } from './details.js';

/** Every quoted string literal in webex.ts, including template literals. */
const sourceLiterals = (): string[] => {
    // Resolved from this module so the suite works whether it is run from dist/
    // (the normal `node --test dist/*.test.js`) or from src/ under ts-node.
    const here = new URL('.', import.meta.url);
    const candidates = [new URL('../src/webex.ts', here), new URL('./webex.ts', here)];
    const src = candidates
        .map((url) => {
            try {
                return readFileSync(url, 'utf8');
            } catch {
                return '';
            }
        })
        .find((text) => text.length > 0);
    assert.ok(src, 'could not read webex.ts — the path in this test needs updating');
    // Comments are stripped first: they discuss selectors (including `>>>`, which
    // is documented here precisely because it must not be used) and markdown
    // backticks in a comment would otherwise read as template literals.
    const code = src.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '');
    // The backtick alternative deliberately allows newlines: a selector written as
    // a multi-line template literal would otherwise be invisible to every scan
    // below. Safe because comments are already gone.
    const literals = [...code.matchAll(/'([^'\n]*)'|"([^"\n]*)"|`([^`]*)`/g)].map(
        (m) => m[1] ?? m[2] ?? m[3],
    );
    // Anything attempting a shadow pierce is inspected whatever shape it is in,
    // as long as the three characters are contiguous in the source.
    if (/>>>/.test(code)) literals.push('<<source contains >>> outside any single literal>>');
    return literals;
};

/**
 * Does this literal look like a regular-expression source rather than a selector?
 *
 * A regex source legitimately contains unbalanced-looking brackets and quantifier
 * parens, so the balance check below has to skip them. Detected by the escape
 * sequences and anchors that a CSS selector never contains.
 */
const looksLikeRegexSource = (lit: string): boolean =>
    /\\[sdwSDWbB]|\(\?|^\^|\$$/.test(lit);

/** Does this literal look like a CSS selector rather than prose? */
const looksLikeSelector = (lit: string): boolean =>
    /\[[a-zA-Z-]+[\^~|*$]?=/.test(lit) ||
    /^[.#][a-zA-Z][\w-]*(\s+[.#>a-zA-Z][\w->]*)*$/.test(lit) ||
    /^[a-z][\w-]*\s+[.#a-z][\w-]*$/.test(lit) ||
    lit.includes('>>>');

/** Literals that look like CSS selectors rather than prose, ids or messages. */
const selectorLiterals = (): string[] => {
    const fromSource = sourceLiterals().filter(
        (lit) =>
            /\[[a-zA-Z-]+[\^~|*$]?=/.test(lit) || // an attribute selector
            /^[.#][a-zA-Z][\w-]*(\s+[.#>a-zA-Z][\w->]*)*$/.test(lit) || // class/id, possibly with descendants
            /^[a-z][\w-]*\s+[.#a-z][\w-]*$/.test(lit) || // 'tag .child' / 'tag tag'
            lit.includes('>>>'), // always inspect an attempted pierce
    );
    // The selectors that live in exported constants rather than inline, so moving
    // a selector out of the file body cannot quietly remove it from this scan.
    const { containers, senders, bodies, rows } = WEBEX_CHAT_SELECTORS;
    return [...fromSource, NAME_INPUT, containers.current, containers.legacy, senders, bodies, rows];
};

/**
 * Lower bound on what the scan finds.
 *
 * Every scanning test below is a loop over selectorLiterals(), so an extractor
 * that silently returned nothing would leave them all passing while checking
 * nothing at all — the worst failure mode a guard test can have.
 */
test('the selector scan finds the selectors it is supposed to check', () => {
    const found = selectorLiterals();
    assert.ok(
        found.length > 30,
        `scan found only ${found.length} selectors — the extractor is probably broken`,
    );
    for (const expected of ['input[data-test="Name (required)"]', '#activity-list mdc-list']) {
        assert.ok(
            found.some((sel) => sel.includes(expected)),
            `scan missed "${expected}"`,
        );
    }
});

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
    // Run over EVERY string literal, not just the selector-shaped ones: a
    // malformation often destroys the shape a classifier would recognise it by
    // ('button:has(span', 'mdc-input data-test="Name"] input'), so filtering first
    // would hide exactly the edits this test exists to catch. Verified not to
    // false-positive on any of the 271 literals currently in the file.
    for (const selector of [...sourceLiterals(), ...selectorLiterals()]) {
        if (looksLikeRegexSource(selector)) continue;
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
    // when adding a markup variant. Over EVERY literal, for the same reason the
    // balance check is: a class-only selector like '.a, , .b' is not recognised by
    // the shape classifier, so filtering first hid exactly this. The two ', ' join
    // separators in the file are the only literals that legitimately look like an
    // empty alternative.
    // Looks for the malformation itself — a doubled comma, or one at either end —
    // rather than splitting every literal, so ordinary prose that happens to end in
    // a comma does not fail a selector guard with a confusing message.
    for (const literal of [...sourceLiterals(), ...selectorLiterals()]) {
        if (literal.trim() === ',' || literal.trim() === ', ') continue;
        assert.ok(!/,\s*,/.test(literal), `"${literal}" has an empty alternative`);
        assert.ok(!/^\s*,/.test(literal), `"${literal}" starts with a comma`);
        assert.ok(!/,\s*$/.test(literal) || !looksLikeSelector(literal), `"${literal}" ends with a comma`);
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
    // costs nothing and survives a layer of DOM churn. Checks the combinator
    // specifically, so a '>' inside an attribute value would not trip it.
    const outsideBrackets = WEBEX_CHAT_SELECTORS.containers.current.replace(/\[[^\]]*\]/g, '');
    assert.ok(!/>/.test(outsideBrackets), 'chat container must not use a child combinator');
});

test('a legacy sender label is reduced to a display name', () => {
    // The legacy markup's label is not a bare name — it reads "from Alice to
    // everyone:", which is why the own-message filter matches a "from LMA" prefix.
    // Passed through unmodified it would post "Thanks from Alice to everyone: —
    // I'll head out now." into the meeting and store that on the meeting record.
    assert.equal(normalizeWebexSender('from Alice Smith to everyone:'), 'Alice Smith');
    assert.equal(normalizeWebexSender('from Alice to me:'), 'Alice');
});

test('a recipient clause is stripped only behind the legacy "from " prefix', () => {
    // Deliberate, and the safer of two imperfect options. Without the prefix a
    // name is not reliably separable from a recipient — "Van To Nguyen:" would
    // reduce to "Van" — and shortening a participant's name is worse than leaving
    // a label slightly long. The prefix is the form this file has always
    // recognised: the own-message filter matches "from LMA".
    assert.equal(normalizeWebexSender('Van To Nguyen:'), 'Van To Nguyen');
    assert.equal(normalizeWebexSender('To Kwok Keung:'), 'To Kwok Keung');
    assert.equal(normalizeWebexSender('Alice Smith:'), 'Alice Smith');
    assert.equal(normalizeWebexSender('Alice Smith to everyone:'), 'Alice Smith to everyone');
});

test('a bare sender name passes through unchanged', () => {
    // The Momentum markup supplies just the name.
    assert.equal(normalizeWebexSender('Alice Smith'), 'Alice Smith');
    assert.equal(normalizeWebexSender('  Alice  Smith  '), 'Alice Smith');
});

test('an absent or empty sender normalizes to null, not to an empty name', () => {
    // exitMessagesFor falls back to the generic goodbye on null; an empty string
    // would produce "Thanks  — I'll head out now."
    for (const empty of [null, undefined, '', '   ', 'from  to everyone:']) {
        assert.equal(normalizeWebexSender(empty), null, `"${empty}" should give null`);
    }
});

test('a name containing "to" is not truncated', () => {
    // The suffix match is anchored at the end and requires the colon form, so an
    // ordinary name that happens to contain the word survives.
    assert.equal(normalizeWebexSender('Toby Tolkien'), 'Toby Tolkien');
    assert.equal(normalizeWebexSender('from Toby Tolkien to everyone:'), 'Toby Tolkien');
});

test('a display name containing the word "to" is not truncated', () => {
    // The recipient clause is only stripped from a label that actually looks like
    // one (a "from " prefix or a trailing colon), because these are real surnames
    // and silently shortening or dropping a participant's name is worse than
    // leaving a legacy label slightly long.
    assert.equal(normalizeWebexSender('To Kwok Keung'), 'To Kwok Keung');
    assert.equal(normalizeWebexSender('Van To Nguyen'), 'Van To Nguyen');
    assert.equal(normalizeWebexSender('from To Kwok Keung to everyone:'), 'To Kwok Keung');
    assert.equal(normalizeWebexSender('from Van To Nguyen to me:'), 'Van To Nguyen');
});

test('a sender whose own name contains "to" survives the legacy label', () => {
    // The boundary match is greedy, so the LAST " to " is the recipient boundary.
    assert.equal(normalizeWebexSender('from Toby Tolkien to everyone:'), 'Toby Tolkien');
});

test("the VP's own messages are recognized however the label is decorated", () => {
    // The page used to compare the raw label, so anything decorative let our own
    // messages through — and an operator-customised start/stop message containing
    // START or PAUSE would then have made the VP toggle itself.
    const ids = ['LMA (bob@example.com)', 'LMA'];
    for (const own of [
        'LMA (bob@example.com)',
        'LMA (bob@example.com):',
        'LMA',
        'LMA:',
        '@LMA (bob@example.com)',
        'LMA (bob@example.com) 10:32',
        'You',
        'You:',
        'You (Host)',
        'from LMA (bob@example.com) to everyone:',
    ]) {
        assert.equal(
            isOwnWebexSender(normalizeWebexSender(own), ids),
            true,
            `"${own}" should be recognized as ours`,
        );
    }
});

test('a participant whose name merely contains our name is not silenced', () => {
    // The regression this replaced: a substring match dropped every message from
    // anyone called ALMA, SELMA or HOLMAN, and ignored their LMA leave.
    const ids = ['LMA (bob@example.com)', 'LMA'];
    for (const other of [
        'ALMA GARCIA',
        'Alma Garcia',
        'SELMA',
        'HOLMAN',
        'LMAO Corp',
        'Youssef Ahmed',
        'You Jin Park',
        'Bob (LMA)',
        'from ALMA GARCIA to everyone:',
    ]) {
        assert.equal(
            isOwnWebexSender(normalizeWebexSender(other), ids),
            false,
            `"${other}" must not be treated as ours`,
        );
    }
});

test('an unknown sender is not mistaken for ours', () => {
    assert.equal(isOwnWebexSender(null, ['LMA']), false);
});

test('a participant whose name merely STARTS with ours is not silenced', () => {
    // A space-delimited prefix match silenced "LMA Smith", the same false-positive
    // class as matching our name as a substring, just narrower. Only the
    // decorations Webex actually adds — a parenthesised role, a trailing timestamp
    // — are accepted around our own name.
    const ids = ['LMA (bob@example.com)', 'LMA'];
    for (const other of ['LMA Smith', 'LMA Team', 'LMA Bot Services', 'You Jin Park', 'Youssef']) {
        assert.equal(isOwnWebexSender(other, ids), false, `"${other}" must not be treated as ours`);
    }
    // ...while the decorated forms of our own name still are.
    for (const own of ['LMA', 'LMA (bob@example.com)', 'LMA 10:32', 'LMA (bob@example.com) 10:32 AM']) {
        assert.equal(isOwnWebexSender(own, ids), true, `"${own}" should be ours`);
    }
});

test('a short or generic configured identity does not silence participants', () => {
    // LMA_IDENTITY is operator-settable, so it can be short. A prefix match would
    // make "Bot" silence "Bot Smith" and "A" silence "A Team".
    assert.equal(isOwnWebexSender('Bot Smith', ['Bot']), false);
    assert.equal(isOwnWebexSender('A Team', ['A']), false);
    assert.equal(isOwnWebexSender('Bot', ['Bot']), true);
    assert.equal(isOwnWebexSender('Bot (Host)', ['Bot']), true);
});

test("the VP's own messages are recognized by text when no label resolves", () => {
    // The sender check cannot fire on a continuation row or in the fallback where
    // the row selector misses, so our own outgoing text is a second line of
    // defence — the start and stop messages are operator-settable and could
    // otherwise contain the literal START or PAUSE and toggle the VP.
    assert.equal(isOwnWebexMessage(details.startMessages[0]), true);
    assert.equal(isOwnWebexMessage(details.exitMessages[0]), true);
    assert.equal(isOwnWebexMessage(`  ${details.startMessages[0]}  `), true);
    assert.equal(isOwnWebexMessage('LMA leave'), false);
    assert.equal(isOwnWebexMessage(''), false);
});
