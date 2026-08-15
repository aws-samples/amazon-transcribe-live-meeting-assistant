/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import assert from 'node:assert/strict';
import test from 'node:test';

import { resolveShouldRecordCall } from './common';

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
