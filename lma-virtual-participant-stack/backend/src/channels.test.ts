/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';

import { LOCAL_CHANNEL, REMOTE_CHANNEL } from './channels.js';

test('a Virtual Participant writes the meeting as REMOTE and its assistant as LOCAL', () => {
    assert.equal(REMOTE_CHANNEL, 'REMOTE');
    assert.equal(LOCAL_CHANNEL, 'LOCAL');
});
