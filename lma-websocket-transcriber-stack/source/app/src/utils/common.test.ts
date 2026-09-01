/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Tests for the filename sanitizer and error normalizer.
 *
 * `posixifyFilename` builds the temp recording filenames from a meeting's call ID
 * (`<id>.raw` and `<id>.wav`). It is lossy by design — every character outside
 * `[a-zA-Z0-9_.]` collapses to `_` — which is exactly how two differently titled
 * meetings once normalized to the same string and overwrote each other's
 * recording in S3. That collision was fixed by timestamping the S3 key rather
 * than by making this function injective, so these tests pin the lossiness as
 * understood behaviour: it is safe for a local temp filename and NOT safe as a
 * unique key on its own.
 */
import { strict as assert } from 'node:assert';
import { test } from 'node:test';
import {
    isError,
    normalizeErrorForLogging,
    posixifyFilename,
    resolveShouldRecordCall
} from './common';

test('the deployment default decides when the client says nothing', () => {
    // The web UI never sends the flag, so this is the normal path.
    assert.equal(resolveShouldRecordCall(undefined, true), true);
    assert.equal(resolveShouldRecordCall(null, true), true);
    assert.equal(resolveShouldRecordCall(undefined, false), false);
});

test('an explicit client value wins over the deployment default', () => {
    assert.equal(resolveShouldRecordCall(false, true), false);
    assert.equal(resolveShouldRecordCall(true, false), true);
});

test('characters that are unsafe in a filename become underscores', () => {
    assert.equal(posixifyFilename('Weekly Sync: Q3/Q4'), 'Weekly_Sync__Q3_Q4');
});

test('dots are preserved so an extension survives', () => {
    // The callers append '.raw'/'.wav', but a call ID may itself contain dots and
    // must not have them mangled.
    assert.equal(posixifyFilename('call.2026.08.13'), 'call.2026.08.13');
});

test('leading and trailing underscores are stripped', () => {
    // Otherwise a call ID that starts with punctuation yields a hidden-ish or
    // ragged filename like '__meeting_.wav'.
    assert.equal(posixifyFilename(' leading'), 'leading');
    assert.equal(posixifyFilename('trailing '), 'trailing');
    assert.equal(posixifyFilename('!!both!!'), 'both');
});

test('path separators cannot escape the temp directory', () => {
    // The result is concatenated into a filesystem path, so a traversal sequence
    // must not survive. Note dots ARE preserved (an extension has to survive), so
    // the '..' remains — but with every '/' collapsed to '_' it is inert: the
    // whole thing is one filename component.
    const sanitized = posixifyFilename('../../etc/passwd');
    assert.ok(!sanitized.includes('/'), `slash survived: ${sanitized}`);
    assert.equal(sanitized, '.._.._etc_passwd');
});

test('the sanitizer is deliberately lossy — distinct inputs can collide', () => {
    // Pinning the known limitation rather than pretending it does not exist: this
    // is why S3 recording keys are timestamped instead of derived from this alone.
    assert.equal(posixifyFilename('a b'), posixifyFilename('a-b'));
});

test('a non-empty input can sanitize to the empty string', () => {
    // All-punctuation input leaves nothing behind, so callers must not assume a
    // usable filename comes back.
    assert.equal(posixifyFilename('///'), '');
});

test('isError narrows only genuine Error instances', () => {
    assert.equal(isError(new Error('boom')), true);
    assert.equal(isError(new TypeError('boom')), true);
    assert.equal(isError('boom'), false);
    assert.equal(isError({ message: 'boom' }), false);
    assert.equal(isError(undefined), false);
});

test('an Error is serialized with its own non-enumerable properties', () => {
    // A bare JSON.stringify of an Error yields '{}' because message and stack are
    // non-enumerable — which is how error logs end up saying nothing at all.
    const serialized = normalizeErrorForLogging(new Error('kinesis put failed'));
    const parsed = JSON.parse(serialized) as { message?: string; stack?: string };
    assert.equal(parsed.message, 'kinesis put failed');
    assert.ok(parsed.stack, 'the stack should be included');
});

test('a string is logged as itself', () => {
    assert.equal(normalizeErrorForLogging('plain failure'), 'plain failure');
});

test('a thrown non-Error still produces a diagnosable message', () => {
    // Nothing stops code from throwing an object or a number; the log must say so
    // rather than printing '{}' or 'undefined'.
    assert.match(normalizeErrorForLogging({ code: 500 }), /not extending Error.*object/i);
    assert.match(normalizeErrorForLogging(42), /not extending Error.*number/i);
});
