/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * A rejoined VP's capture session (docs/virtual-participant.md#rejoin): its
 * events name the session, and its transcript segments follow the earlier
 * sessions' on the meeting's timeline instead of starting again at 0.
 */
import { strict as assert } from 'node:assert';
import { beforeEach, describe, it } from 'node:test';

process.env.LOCAL_TEST = 'true';
delete process.env.CALL_DATA_STREAM_NAME;

// eslint-disable-next-line import/first
import { kinesisStreamManager } from './kinesis-stream.js';
// eslint-disable-next-line import/first
import { callSessionFromVpRow } from './call-session.js';

type Record_ = Record<string, any>;
const sent: Record_[] = [];
const manager = kinesisStreamManager as unknown as {
    sendRecord: (record: Record_) => Promise<void>;
    startSent: boolean;
};

const segment = (overrides: Record_ = {}) => ({
    segmentId: 'vp-remote-r0-g0-s0',
    startTime: 1.5,
    endTime: 4.25,
    transcript: 'hello again',
    isPartial: false,
    channel: 'REMOTE',
    speaker: 'Alice',
    ...overrides,
});

beforeEach(() => {
    sent.length = 0;
    manager.sendRecord = async (record: Record_) => {
        sent.push(record);
    };
    manager.startSent = false;
    kinesisStreamManager.useSession(1, 0);
});

describe('a VP that was not rejoined', () => {
    it('names session 1 in START and END, and sends its segments as they are', async () => {
        await kinesisStreamManager.sendStartMeeting();
        await kinesisStreamManager.sendAsrTranscriptSegment(segment() as any);
        await kinesisStreamManager.sendEndMeeting();
        const [start, row, end] = sent;
        // So a late END of this session is dropped once a rejoined one has started.
        assert.equal(start.SessionNumber, 1);
        assert.equal(end.SessionNumber, 1);
        assert.equal('SessionNumber' in row, false);
        assert.equal(row.SegmentId, 'vp-remote-r0-g0-s0');
        assert.equal(row.StartTime, 1.5);
        assert.equal(row.EndTime, 4.25);
        assert.equal(kinesisStreamManager.sessionOffsetSeconds, 0);
    });
});

describe('a rejoined VP', () => {
    it('names its session in START and END', async () => {
        assert.equal(kinesisStreamManager.useSession(2, 1802), true);
        await kinesisStreamManager.sendStartMeeting();
        await kinesisStreamManager.sendEndMeeting();
        assert.equal(sent[0].EventType, 'START');
        assert.equal(sent[0].SessionNumber, 2);
        assert.ok(sent[0].VirtualParticipantId, 'START names the VP, so it can be ended if the session is refused');
        assert.equal(sent[1].EventType, 'END');
        assert.equal(sent[1].SessionNumber, 2);
    });

    it('moves every segment past the earlier sessions, under an id of its own', async () => {
        kinesisStreamManager.useSession(2, 1802);
        await kinesisStreamManager.sendAsrTranscriptSegment(segment() as any);
        await kinesisStreamManager.sendChatSegment(segment({ segmentId: 'chat-1', startTime: 9, endTime: 9 }) as any);
        const [row, chat] = sent;
        assert.equal(row.SegmentId, 's2-vp-remote-r0-g0-s0');
        assert.equal(row.StartTime, 1803.5);
        assert.equal(row.EndTime, 1806.25);
        assert.equal(chat.SegmentId, 's2-chat-1');
        assert.equal(chat.StartTime, 1811);
        assert.equal(kinesisStreamManager.sessionOffsetSeconds, 1802);
    });

    it('moves Amazon Transcribe results too, and reports their end on its own clock', async () => {
        kinesisStreamManager.useSession(3, 100);
        const result = {
            ResultId: 'result-1',
            IsPartial: false,
            StartTime: 2,
            EndTime: 3,
            Alternatives: [
                {
                    Transcript: 'hi',
                    Items: [{ Type: 'pronunciation', Content: 'hi', StartTime: 2, EndTime: 3 }],
                },
            ],
        };
        const end = await kinesisStreamManager.sendTranscriptSegment('Bob', result);
        assert.ok(sent.length > 0);
        for (const row of sent) {
            assert.match(row.SegmentId, /^s3-result-1/);
            assert.ok(row.StartTime >= 100 && row.EndTime >= 100, JSON.stringify(row));
        }
        assert.ok(end < 100, 'the reconnect offset the caller keeps is on the session clock');
    });

    it('keeps its session once START is sent', async () => {
        kinesisStreamManager.useSession(2, 50);
        manager.startSent = true;
        assert.equal(kinesisStreamManager.useSession(3, 70), false);
        assert.equal(kinesisStreamManager.sessionOffsetSeconds, 50);
        assert.equal(kinesisStreamManager.useSession(2, 50), true, 'the same session again is fine');
    });

    it('ignores a session number that is not one', () => {
        assert.equal(kinesisStreamManager.useSession(0, 10), false);
        assert.equal(kinesisStreamManager.useSession(2.5, 10), false);
        assert.equal(kinesisStreamManager.sessionOffsetSeconds, 0);
    });
});

describe('the session on the VP row', () => {
    it('is read with the CallId', () => {
        assert.deepEqual(callSessionFromVpRow({ CallId: 'call-1', sessionNumber: 2, sessionTimeOffset: 1802 }), {
            callId: 'call-1',
            sessionNumber: 2,
            sessionTimeOffset: 1802,
        });
    });

    it('is the first, at 0, on a row that was never rejoined', () => {
        assert.deepEqual(callSessionFromVpRow({ CallId: 'call-1' }), {
            callId: 'call-1',
            sessionNumber: 1,
            sessionTimeOffset: 0,
        });
        assert.deepEqual(callSessionFromVpRow({ CallId: 'call-1', sessionNumber: 'x', sessionTimeOffset: -3 }), {
            callId: 'call-1',
            sessionNumber: 1,
            sessionTimeOffset: 0,
        });
    });

    it('is nothing without a CallId', () => {
        assert.equal(callSessionFromVpRow({ sessionNumber: 2 }), null);
        assert.equal(callSessionFromVpRow(null), null);
    });
});
