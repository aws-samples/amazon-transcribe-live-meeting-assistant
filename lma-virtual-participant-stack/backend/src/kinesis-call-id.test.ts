/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

import { strict as assert } from 'node:assert';
import { beforeEach, describe, it } from 'node:test';

process.env.LOCAL_TEST = 'true';
delete process.env.CALL_DATA_STREAM_NAME;

// eslint-disable-next-line import/first
import { kinesisStreamManager } from './kinesis-stream.js';

type Record_ = Record<string, unknown>;
const sent: Record_[] = [];
const manager = kinesisStreamManager as unknown as {
    sendRecord: (record: Record_) => Promise<void>;
    sendStartMeeting: () => Promise<void>;
    startSent: boolean;
};

beforeEach(() => {
    sent.length = 0;
    manager.sendRecord = async (record: Record_) => {
        sent.push(record);
    };
    manager.startSent = false;
});

describe('the CallId on the VP record', () => {
    it('is the CallId every event of the meeting is sent under', async () => {
        assert.equal(kinesisStreamManager.useCallId('Weekly sync-2026-11-16-15-57-50-308'), true);
        assert.equal(kinesisStreamManager.getCallId(), 'Weekly sync-2026-11-16-15-57-50-308');
        assert.equal(kinesisStreamManager.currentCallId, 'Weekly sync-2026-11-16-15-57-50-308');
        await manager.sendStartMeeting();
        assert.ok(sent.length > 0, 'START was sent');
        for (const record of sent) assert.equal(record.CallId, 'Weekly sync-2026-11-16-15-57-50-308');
    });

    it('is not changed once the meeting has started under another', () => {
        kinesisStreamManager.useCallId('first-call');
        manager.startSent = true;
        assert.equal(kinesisStreamManager.useCallId('second-call'), false);
        assert.equal(kinesisStreamManager.getCallId(), 'first-call');
        assert.equal(kinesisStreamManager.useCallId('first-call'), true, 'the same CallId again is fine');
    });

    it('ignores an empty CallId', () => {
        kinesisStreamManager.useCallId('kept');
        assert.equal(kinesisStreamManager.useCallId('  '), false);
        assert.equal(kinesisStreamManager.getCallId(), 'kept');
    });
});
