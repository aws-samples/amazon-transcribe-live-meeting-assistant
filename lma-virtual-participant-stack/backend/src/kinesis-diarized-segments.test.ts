/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * What the Kinesis writer actually emits, diarized and not.
 *
 * The pure run-building logic is covered in transcribe-diarization.test.ts.
 * This drives the REAL writer, because the things that go wrong live in the
 * seam rather than in either side of it: the roster timeline, the segment ids,
 * the composed speaker string, and whether the undiarized path still behaves
 * exactly as it does today. Source-text assertions cannot carry that weight --
 * they pass while the feature is dead.
 *
 * `sendRecord` is the only thing stubbed, so everything above it is the
 * shipping code path.
 */
import { strict as assert } from 'node:assert';
import { beforeEach, describe, it } from 'node:test';

process.env.LOCAL_TEST = 'true';
delete process.env.CALL_DATA_STREAM_NAME;

// eslint-disable-next-line import/first
import { kinesisStreamManager } from './kinesis-stream.js';

type Record_ = { EventType: string; SegmentId?: string; Speaker?: string; Transcript?: string; StartTime?: number; EndTime?: number; IsPartial?: boolean };

const sent: Record_[] = [];

/** A pronunciation item, optionally carrying a Transcribe speaker label. */
const word = (content: string, speaker: string | undefined, start: number, end: number) => ({
    Content: content,
    Type: 'pronunciation',
    StartTime: start,
    EndTime: end,
    ...(speaker === undefined ? {} : { Speaker: speaker }),
});

const result = (
    items: ReturnType<typeof word>[],
    options: { isPartial?: boolean; resultId?: string; startTime?: number } = {},
) => ({
    IsPartial: options.isPartial ?? false,
    ResultId: options.resultId,
    StartTime: options.startTime,
    Alternatives: [{ Items: items, Transcript: items.map((i) => i.Content).join(' ') }],
});

/** `count` labelled words a second apart, from `from`. */
const turn = (label: string | undefined, count: number, from: number) =>
    Array.from({ length: count }, (_u, i) => word(`w${label ?? 'x'}${i}`, label, from + i, from + i + 0.9));

const manager = kinesisStreamManager as unknown as {
    sendRecord: (record: Record_) => Promise<void>;
    setDiarization: (on: boolean) => void;
    currentSpeakerName: string | null;
    speakers: string[];
    startTimes: number[];
    sendTranscriptSegment: (speaker: string, result: unknown, offset?: number) => Promise<number>;
};

const emit = async (speaker: string, res: unknown, offset = 0) => {
    sent.length = 0;
    await manager.sendTranscriptSegment(speaker, res, offset);
    return sent.filter((record) => record.EventType === 'ADD_TRANSCRIPT_SEGMENT');
};

beforeEach(() => {
    manager.sendRecord = async (record: Record_) => {
        sent.push(record);
    };
    manager.setDiarization(false);
    manager.currentSpeakerName = null;
    manager.speakers = [];
    manager.startTimes = [];
});

describe('with voice separation off', () => {
    it('emits exactly one segment named by the roster alone', async () => {
        // What every deployment gets today. The id and times come from the last
        // item's start time, which is odd and long-standing; pinned so the
        // diarized branch cannot change it by accident.
        const records = await emit('Conference Room', result(turn('0', 6, 0)));
        assert.equal(records.length, 1);
        assert.equal(records[0]?.Speaker, 'Conference Room');
        assert.ok(!records[0]?.SegmentId?.includes('spk_'));
    });

    it('ignores the speaker labels entirely', async () => {
        const records = await emit('Conference Room', result([...turn('0', 6, 0), ...turn('1', 6, 10)]));
        assert.equal(records.length, 1);
        assert.equal(records[0]?.Speaker, 'Conference Room');
    });
});

describe('with voice separation on', () => {
    beforeEach(() => manager.setDiarization(true));

    it('separates the voices behind one roster participant', async () => {
        // The whole purpose: a room on one speakerphone is one roster entry.
        const records = await emit(
            'Conference Room',
            result([...turn('0', 6, 0), ...turn('1', 6, 10)], { resultId: 'R1' }),
        );
        assert.deepEqual(
            records.map((record) => record.Speaker),
            ['Conference Room (spk_0)', 'Conference Room (spk_1)'],
        );
        assert.deepEqual(
            records.map((record) => record.SegmentId),
            ['R1-r0', 'R1-r1'],
        );
    });

    it('replaces its own partial rather than orphaning it', async () => {
        // A partial left behind sits in the transcript for ever marked
        // in-progress: visible on screen, missing from the summary.
        const partial = await emit(
            'Conference Room',
            result(turn(undefined, 4, 0), { isPartial: true, resultId: 'R2' }),
        );
        assert.equal(partial.length, 1);
        assert.equal(partial[0]?.IsPartial, true);

        const final = await emit(
            'Conference Room',
            result([...turn('0', 6, 0), ...turn('1', 6, 10)], { resultId: 'R2' }),
        );
        assert.equal(partial[0]?.SegmentId, final[0]?.SegmentId);
        assert.equal(final[0]?.IsPartial, false);
    });

    it('keeps a roster attribution the labels would have merged away', async () => {
        // Transcribe under-segmenting two remote attendees onto one label is
        // its normal failure on a downmixed stream. The undiarized path gets
        // this right because the roster drives the split, so the diarized path
        // must not lose it.
        manager.speakers = ['Alex', 'Beth'];
        manager.startTimes = [0, 10];
        manager.currentSpeakerName = 'Beth';
        const records = await emit(
            'Beth',
            result([...turn('0', 6, 0), ...turn('0', 6, 10)], { resultId: 'R3' }),
        );
        assert.deepEqual(
            records.map((record) => record.Speaker),
            ['Alex (spk_0)', 'Beth (spk_0)'],
        );
    });

    it('names a turn starting exactly at a roster change with the NEW speaker', async () => {
        // An exact tie used to resolve to the previous speaker, so every turn
        // after a roster change carried the name of the person before it.
        manager.speakers = ['Alex', 'Conference Room'];
        manager.startTimes = [0, 10];
        manager.currentSpeakerName = 'Conference Room';
        const records = await emit('Conference Room', result(turn('0', 6, 10), { resultId: 'R4' }));
        assert.equal(records[0]?.Speaker, 'Conference Room (spk_0)');
    });

    it('resets the roster timeline on a final result', async () => {
        // The undiarized path resets after its item loop; the diarized branch
        // returns early and has to do the same, or the timeline grows for the
        // whole meeting.
        await emit('Conference Room', result(turn('0', 6, 0), { resultId: 'R5' }));
        assert.deepEqual(manager.startTimes, []);
        assert.equal(manager.currentSpeakerName, null);
    });

    it('keeps the roster timeline across a partial', async () => {
        await emit('Conference Room', result(turn(undefined, 4, 0), { isPartial: true, resultId: 'R6' }));
        assert.equal(manager.startTimes.length, 1);
    });

    it('gives two results distinct ids when ResultId is absent', async () => {
        // The fallback must vary per result. Keyed on the session offset it did
        // not, so a whole meeting collapsed onto one transcript row.
        const first = await emit('Conference Room', result(turn('0', 6, 0), { startTime: 0 }), 0);
        const second = await emit('Conference Room', result(turn('0', 6, 40), { startTime: 40 }), 0);
        assert.notEqual(first[0]?.SegmentId, second[0]?.SegmentId);
    });

    it('shifts times by the reconnect offset', async () => {
        const records = await emit('Conference Room', result(turn('0', 6, 0), { resultId: 'R7' }), 120);
        assert.ok((records[0]?.StartTime ?? 0) >= 120);
    });

    it('loses no text when a result splits into several segments', async () => {
        const items = [...turn('0', 6, 0), ...turn('1', 6, 10)];
        const records = await emit('Conference Room', result(items, { resultId: 'R8' }));
        const emitted = records.map((record) => record.Transcript).join(' ').split(' ').filter(Boolean);
        assert.deepEqual(emitted, items.map((item) => item.Content));
    });
});
