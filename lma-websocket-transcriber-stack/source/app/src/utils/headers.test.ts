/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Tests for HTTP header canonicalization and client-IP extraction.
 *
 * `getClientIP` feeds the `[AUTH]` and `[NEW CONNECTION]` log lines in index.ts
 * and nothing else — it is not consulted for authorization, so the fact that it
 * trusts the leftmost `X-Forwarded-For` entry (which a client can set freely) is a
 * log-fidelity limitation rather than a security hole. These tests pin the
 * behaviour as it is, so that stays a deliberate choice: if this value is ever
 * used for a decision, taking the leftmost entry becomes a real vulnerability and
 * these tests are where that assumption is written down.
 */
import { strict as assert } from 'node:assert';
import { test } from 'node:test';
import { canonicalizeHeaderFieldValue, getClientIP, queryCanonicalizedHeaderField } from './headers';

test('surrounding whitespace is stripped from a header value', () => {
    assert.equal(canonicalizeHeaderFieldValue('  10.0.0.1  '), '10.0.0.1');
});

test('an obs-folded header is unfolded to a single space', () => {
    // RFC 9112 obsolete line folding: a CRLF followed by whitespace continues the
    // value. Left in place it would inject a newline into every log line built
    // from this value, splitting one entry into two in CloudWatch.
    assert.equal(canonicalizeHeaderFieldValue('first\r\n  second'), 'first second');
    assert.equal(canonicalizeHeaderFieldValue('a \r\n\tb'), 'a b');
});

test('a value with no folding is returned unchanged', () => {
    assert.equal(canonicalizeHeaderFieldValue('keep internal  spaces'), 'keep internal  spaces');
});

test('a repeated header arriving as an array is joined per RFC', () => {
    // Node hands repeated headers over as an array; they are semantically one
    // comma-separated field.
    assert.equal(
        queryCanonicalizedHeaderField({ 'x-forwarded-for': [' 10.0.0.1 ', '10.0.0.2'] }, 'x-forwarded-for'),
        '10.0.0.1, 10.0.0.2',
    );
});

test('a missing header queries as null rather than undefined', () => {
    // Callers branch on truthiness, so null and undefined behave the same here —
    // but the declared return type is `string | null` and this keeps it honest.
    assert.equal(queryCanonicalizedHeaderField({}, 'x-forwarded-for'), null);
});

test('the client IP is the first entry of X-Forwarded-For', () => {
    // With a proxy chain, the leftmost entry is the original client as reported by
    // the first proxy.
    assert.equal(getClientIP({ 'x-forwarded-for': '203.0.113.7, 10.0.0.1, 10.0.0.2' }), '203.0.113.7');
});

test('a single-entry X-Forwarded-For is returned as-is', () => {
    assert.equal(getClientIP({ 'x-forwarded-for': '203.0.113.7' }), '203.0.113.7');
});

test('an absent or empty X-Forwarded-For reports "unknown"', () => {
    // Direct connections (health checks, in-VPC callers) have no such header, and
    // the log line must still be well formed.
    assert.equal(getClientIP({}), 'unknown');
    assert.equal(getClientIP({ 'x-forwarded-for': '' }), 'unknown');
    assert.equal(getClientIP({ 'x-forwarded-for': undefined }), 'unknown');
});

test('only the whole field is trimmed, not each entry', () => {
    // Documenting a wart rather than asserting an ideal. Canonicalization trims the
    // FIELD, so leading padding goes; but the split on ',' does not trim each
    // entry, so padding before the separator survives into the logged value.
    // Harmless for a log line, which is the only consumer — but if this value ever
    // feeds a comparison or an allowlist, entries need trimming first.
    assert.equal(getClientIP({ 'x-forwarded-for': '  203.0.113.7 , 10.0.0.1' }), '203.0.113.7 ');
    // The common case is unaffected: ELB emits ", " so the first entry is clean.
    assert.equal(getClientIP({ 'x-forwarded-for': '203.0.113.7, 10.0.0.1' }), '203.0.113.7');
});
