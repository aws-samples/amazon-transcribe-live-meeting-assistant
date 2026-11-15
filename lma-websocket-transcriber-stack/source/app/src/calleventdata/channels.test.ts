/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */
import assert from 'node:assert/strict';
import test from 'node:test';

import { LOCAL_CHANNEL, REMOTE_CHANNEL, transcriptChannelFor } from './channels';
import { CHANNEL_MIC, CHANNEL_SYSTEM } from './eventtypes';

test('the server writes the two speech channels as REMOTE and LOCAL', () => {
    assert.equal(REMOTE_CHANNEL, 'REMOTE');
    assert.equal(LOCAL_CHANNEL, 'LOCAL');
});

test('the meeting audio channel is REMOTE and the microphone LOCAL', () => {
    assert.equal(transcriptChannelFor(CHANNEL_SYSTEM), 'REMOTE');
    assert.equal(transcriptChannelFor(CHANNEL_MIC), 'LOCAL');
});

test('a result without a channel id keeps the LOCAL channel it has always had', () => {
    assert.equal(transcriptChannelFor(undefined), 'LOCAL');
});
