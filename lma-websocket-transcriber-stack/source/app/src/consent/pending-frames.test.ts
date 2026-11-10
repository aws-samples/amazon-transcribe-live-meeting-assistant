/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Consent frames held while their connection's START is handled: held only
 * while a START is in progress, handed back in order once, bounded per
 * connection, and forgotten when the START is refused or the connection closes.
 */
import assert from 'node:assert/strict';
import test from 'node:test';

import { PendingConsentFrames } from './pending-frames';

test('frames behind a START in progress are held and released in order, once', () => {
    const pending = new PendingConsentFrames<string>();
    pending.begin('ws1');
    assert.equal(pending.hold('ws1', { callEvent: 'CONSENT_EVIDENCE', n: 1 }), 'held');
    assert.equal(pending.hold('ws1', { callEvent: 'CONSENT_OBJECTION', n: 2 }), 'held');
    assert.deepEqual(
        pending.release('ws1').map((f) => f['n']),
        [1, 2]
    );
    assert.deepEqual(pending.release('ws1'), []);
    assert.equal(pending.hold('ws1', { n: 3 }), 'no-start');
});

test('a connection with no START in progress holds nothing', () => {
    const pending = new PendingConsentFrames<string>();
    assert.equal(pending.hold('ws1', { n: 1 }), 'no-start');
    pending.begin('ws2');
    assert.equal(pending.hold('ws1', { n: 1 }), 'no-start');
});

test('each connection holds a bounded number of frames', () => {
    const pending = new PendingConsentFrames<string>(2);
    pending.begin('ws1');
    assert.equal(pending.hold('ws1', { n: 1 }), 'held');
    assert.equal(pending.hold('ws1', { n: 2 }), 'held');
    assert.equal(pending.hold('ws1', { n: 3 }), 'full');
    assert.equal(pending.release('ws1').length, 2);
});

test('a refused START or a closed connection forgets its frames', () => {
    const pending = new PendingConsentFrames<string>();
    pending.begin('ws1');
    pending.hold('ws1', { n: 1 });
    pending.drop('ws1');
    assert.deepEqual(pending.release('ws1'), []);
    assert.equal(pending.hold('ws1', { n: 2 }), 'no-start');
});
