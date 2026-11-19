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
import { readFileSync } from 'node:fs';
import { beforeEach, describe, it } from 'node:test';

process.env.LOCAL_TEST = 'true';
delete process.env.CALL_DATA_STREAM_NAME;

// eslint-disable-next-line import/first
import { kinesisStreamManager } from './kinesis-stream.js';
// eslint-disable-next-line import/first
import { callSessionFromVpRow } from './call-session.js';
// eslint-disable-next-line import/first
import { ChatAttachmentCapture } from './chat-attachments.js';

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

    it('names session 1 on its recording links', async () => {
        await kinesisStreamManager.sendCallRecording('https://bucket/recordings/a.wav');
        await kinesisStreamManager.sendCallVideoRecording('https://bucket/video/a.mp4');
        assert.deepEqual(
            sent.map((record) => record.SessionNumber),
            [1, 1],
        );
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

    it('names its session on its recording links, so each is played at its place', async () => {
        kinesisStreamManager.useSession(3, 640);
        await kinesisStreamManager.sendCallRecording('https://bucket/recordings/session-3.wav');
        await kinesisStreamManager.sendCallVideoRecording('https://bucket/video/session-3.mp4');
        assert.deepEqual(
            sent.map((record) => [record.EventType, record.SessionNumber]),
            [
                ['ADD_S3_RECORDING_URL', 3],
                ['ADD_S3_VIDEO_RECORDING_URL', 3],
            ],
        );
    });

    it('ignores a session number that is not one', () => {
        assert.equal(kinesisStreamManager.useSession(0, 10), false);
        assert.equal(kinesisStreamManager.useSession(2.5, 10), false);
        assert.equal(kinesisStreamManager.sessionOffsetSeconds, 0);
    });
});

// ---------------------------------------------------------------------------
// Every event that names a segment or a transcript time
// ---------------------------------------------------------------------------

// A 1x1 PNG, enough for the attachment capture to read its type.
const PNG = Buffer.from(
    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==',
    'base64',
);

/**
 * The attachment capture wired as chat-attachment-runtime.ts wires it, to the
 * manager under test, with the real id rule; uploads are kept.
 */
const attachmentCapture = (keys: string[]) =>
    new ChatAttachmentCapture({
        settingOn: () => true,
        summariesOn: () => true,
        capturing: () => true,
        callId: () => kinesisStreamManager.currentCallId,
        upload: async (key) => {
            keys.push(key);
        },
        emit: (event) => kinesisStreamManager.sendChatAttachment(event),
        onMeetingTimeline: (segmentId, timelineSeconds) => ({
            segmentId: kinesisStreamManager.meetingSegmentId(segmentId),
            timelineSeconds: kinesisStreamManager.meetingSeconds(timelineSeconds),
        }),
    });

/** A chat line and its image, sent as meeting-chat-runtime.ts sends them. */
async function chatLineWithImage(keys: string[]): Promise<{ line: Record_; attachment: Record_ }> {
    const capture = attachmentCapture(keys);
    await kinesisStreamManager.sendChatSegment(
        segment({ segmentId: 'chat-zoom-3-81467AC7', startTime: 44.98, endTime: 44.98, channel: 'MEETING_CHAT' }) as any,
    );
    capture.capture('chat-zoom-3-81467AC7', 'zoom', 44.98, [
        { kind: 'image', name: 'board.png', fetch: async () => ({ ok: true, bytes: PNG }) },
    ]);
    await capture.idle();
    const line = sent.find((r) => r.EventType === 'ADD_TRANSCRIPT_SEGMENT')!;
    const attachment = sent.find((r) => r.EventType === 'ADD_CHAT_ATTACHMENT')!;
    return { line, attachment };
}

describe('the events that name a segment or a time, in the first session', () => {
    it('are sent on the VP’s own clock and ids', async () => {
        assert.equal(kinesisStreamManager.meetingSegmentId('chat-zoom-1'), 'chat-zoom-1');
        assert.equal(kinesisStreamManager.meetingSeconds(44.98), 44.98);
        const keys: string[] = [];
        const { line, attachment } = await chatLineWithImage(keys);
        assert.equal(line.SegmentId, 'chat-zoom-3-81467AC7');
        assert.equal(line.StartTime, 44.98);
        assert.equal(attachment.SegmentId, line.SegmentId);
        assert.equal(attachment.Timestamp, 44.98);
        assert.match(attachment.AttachmentId, /^0000044980-[0-9a-f]{8}$/);
        assert.ok(keys[0].includes(`/${attachment.AttachmentId}/`));
    });

    it('sends key frames as the sampler stamped them', async () => {
        await kinesisStreamManager.sendVideoKeyframe({ FrameId: '0000044980-ab', Timestamp: 44.98 } as any);
        assert.equal(sent[0].FrameId, '0000044980-ab');
        assert.equal(sent[0].Timestamp, 44.98);
    });
});

describe('the events that name a segment or a time, in a rejoined session', () => {
    beforeEach(() => {
        kinesisStreamManager.useSession(2, 317);
    });

    it('map ids and times onto the meeting', () => {
        assert.equal(kinesisStreamManager.meetingSegmentId('chat-zoom-1'), 's2-chat-zoom-1');
        assert.equal(kinesisStreamManager.meetingSeconds(44.98), 361.98);
    });

    it('send a chat attachment naming its chat line as the meeting stores it', async () => {
        const keys: string[] = [];
        const { line, attachment } = await chatLineWithImage(keys);
        assert.equal(line.SegmentId, 's2-chat-zoom-3-81467AC7');
        assert.equal(line.StartTime, 361.98);
        // The meeting page matches the attachment to its chat line by these two.
        assert.equal(attachment.SegmentId, line.SegmentId);
        assert.equal(attachment.Timestamp, line.StartTime);
    });

    it('give an attachment an id, and an object key, no earlier session can have', async () => {
        const keys: string[] = [];
        const { attachment } = await chatLineWithImage(keys);
        // From the moved time: session 2 starts after everything session 1
        // recorded, so session 1's attachment at 44.98 s had 0000044980-….
        assert.match(attachment.AttachmentId, /^0000361980-[0-9a-f]{8}$/);
        assert.ok(keys[0].includes(`/${attachment.AttachmentId}/`));
        assert.equal(attachment.S3Key, keys[0]);
    });

    it('send assistant and speech rows under the session’s ids and times', async () => {
        await kinesisStreamManager.sendAsrTranscriptSegment(segment({ channel: 'AGENT_ASSISTANT' }) as any);
        assert.equal(sent[0].SegmentId, 's2-vp-remote-r0-g0-s0');
        assert.equal(sent[0].StartTime, 318.5);
        assert.equal(sent[0].EndTime, 321.25);
    });

    it('leave key frames as the sampler stamped them, already on the meeting’s timeline', async () => {
        // index.ts gives the sampler the session offset plus the VP's clock, so
        // its Timestamp and its time-based FrameId are moved at the source.
        await kinesisStreamManager.sendVideoKeyframe({ FrameId: '0000361980-ab', Timestamp: 361.98 } as any);
        assert.equal(sent[0].FrameId, '0000361980-ab');
        assert.equal(sent[0].Timestamp, 361.98);
        const index = readFileSync(new URL('../src/index.ts', import.meta.url), 'utf8');
        assert.match(index, /kinesisStreamManager\.sessionOffsetSeconds \+ transcriptionService\.timelineSeconds\(\)/);
    });
});

describe('the attachment capture as the VP wires it', () => {
    it('moves each chat line onto the meeting’s timeline', () => {
        const runtime = readFileSync(new URL('../src/chat-attachment-runtime.ts', import.meta.url), 'utf8');
        assert.match(runtime, /onMeetingTimeline:[\s\S]*kinesisStreamManager\.meetingSegmentId\(segmentId\)/);
        assert.match(runtime, /kinesisStreamManager\.meetingSeconds\(timelineSeconds\)/);
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
