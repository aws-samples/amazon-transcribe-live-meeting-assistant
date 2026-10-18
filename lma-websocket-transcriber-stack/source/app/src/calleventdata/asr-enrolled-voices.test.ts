/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
/**
 * Which enrolled voices reach a transcription session.
 *
 * A seed is a voiceprint somebody consented to, so the decision about whether it
 * may be used at all is made before this -- by the code that reads the
 * enrollments, which honours status, terms version and expiry. What is tested
 * here is the narrower question of what is safe to put on the wire: a malformed
 * seed must not fail the session, and a seed from a different embedding model
 * must not reach the engine at all.
 */
import assert from 'node:assert/strict';
import test from 'node:test';

import { MAX_ENROLLED_VOICES, enrolledVoicesForConfig } from './asr-microvm';

const voice = (label: string, vector: number[] = [0.6, 0.8]) => ({ label, vector });

test('no enrolled voices sends nothing', () => {
    assert.deepEqual(enrolledVoicesForConfig(undefined), []);
    assert.deepEqual(enrolledVoicesForConfig([]), []);
});

test('a usable voice is sent with a default weight', () => {
    const [seed] = enrolledVoicesForConfig([voice('Sarah Lee')]);
    assert.equal(seed.label, 'Sarah Lee');
    assert.deepEqual(seed.vector, [0.6, 0.8]);
    // Anchors the identity so one mis-assigned utterance cannot move it as far
    // as the whole enrollment did.
    assert.equal(seed.weight, 8);
});

test('an explicit weight is kept', () => {
    const [seed] = enrolledVoicesForConfig([{ ...voice('Sarah Lee'), weight: 3 }]);
    assert.equal(seed.weight, 3);
});

test('a non-positive weight falls back to the default rather than disabling the anchor', () => {
    for (const weight of [0, -1]) {
        const [seed] = enrolledVoicesForConfig([{ ...voice('Sarah Lee'), weight }]);
        assert.equal(seed.weight, 8);
    }
});

test('a label is trimmed, since it goes straight into a transcript', () => {
    const [seed] = enrolledVoicesForConfig([voice('  Sarah Lee  ')]);
    assert.equal(seed.label, 'Sarah Lee');
});

test('a malformed seed is dropped rather than failing the session', () => {
    // Losing a name is a far smaller harm than losing the transcript, and the
    // engine takes the same view of a seed it cannot use.
    const seeds = enrolledVoicesForConfig([
        voice(''),
        voice('   '),
        { label: 'No vector', vector: [] },
        { label: 'Not a vector', vector: undefined as unknown as number[] },
        { label: 'Not numbers', vector: [Number.NaN, 1] },
        { label: 'Infinite', vector: [Number.POSITIVE_INFINITY, 1] },
        voice('Sarah Lee'),
    ]);
    assert.deepEqual(
        seeds.map((s) => s.label),
        ['Sarah Lee']
    );
});

test('seeds from a different embedding model are not sent', () => {
    // Comparing vectors another model produced is meaningless, and the engine
    // refuses the whole seed; filtering here keeps the majority rather than
    // losing every name to one stale row.
    const seeds = enrolledVoicesForConfig([
        voice('Sarah Lee', [0.6, 0.8]),
        voice('Priya Raman', [0.1, 0.2]),
        voice('Stale', [0.1, 0.2, 0.3]),
    ]);
    assert.deepEqual(
        seeds.map((s) => s.label),
        ['Sarah Lee', 'Priya Raman']
    );
});

test('two people with the same display name are both sent', () => {
    // Dropping one would silently discard somebody's enrollment; the engine
    // keeps them apart by their vectors regardless of what they are called.
    const seeds = enrolledVoicesForConfig([
        voice('Chris', [1, 0]),
        voice('Chris', [0, 1]),
    ]);
    assert.equal(seeds.length, 2);
});

test('the number of seeds is bounded', () => {
    const many = Array.from({ length: MAX_ENROLLED_VOICES + 10 }, (_, i) => voice(`Person ${i}`));
    assert.equal(enrolledVoicesForConfig(many).length, MAX_ENROLLED_VOICES);
});

test('the vector is passed through untouched', () => {
    // It is the voiceprint; rounding or reordering it would change who it
    // recognises.
    const vector = [0.123456789, -0.987654321, 0];
    const [seed] = enrolledVoicesForConfig([voice('Sarah Lee', vector)]);
    assert.deepEqual(seed.vector, vector);
});
