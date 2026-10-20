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
import { afterEach, beforeEach, describe, it } from 'node:test';

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

    it('resolves a roster tie to the PREVIOUS speaker, as it does today', async () => {
        // The diarized path deliberately uses the opposite tie-break, and the
        // two searches sit next to each other in the writer. Pinned so that
        // unifying them -- the natural tidy-up to reach for -- cannot silently
        // change the attribution every deployment has now. If this needs
        // changing, it is a behaviour change for existing meetings and belongs
        // in its own commit with its own note, not in a refactor.
        manager.speakers = ['Alex', 'Beth'];
        manager.startTimes = [0, 10];
        manager.currentSpeakerName = 'Beth';
        const records = await emit('Beth', result(turn(undefined, 4, 10), { isPartial: true }));
        // The word at exactly 10 goes to Alex and the rest to Beth: a
        // lower-bound search resolves the tie backwards, which is why the
        // diarized path uses the upper bound instead.
        assert.deepEqual(
            records.map((record) => record.Speaker),
            ['Alex', 'Beth'],
        );
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

describe('every segment the writer starts, it finishes', () => {
    // The property that keeps the live transcript clean, asserted end to end
    // rather than per-call: a row emitted as in-progress must eventually be
    // emitted again as final, or it sits there for ever showing words the
    // final already carried, attributed to the wrong participant, excluded
    // from summaries and absent from the Speakers panel.
    beforeEach(() => manager.setDiarization(true));

    const partialThenFinal = async (rosterPerCall: string[]) => {
        const all: Record_[] = [];
        const push = async (speaker: string, res: unknown) => {
            sent.length = 0;
            await manager.sendTranscriptSegment(speaker, res, 0);
            all.push(...sent.filter((record) => record.EventType === 'ADD_TRANSCRIPT_SEGMENT'));
        };
        const base = [word('hello', undefined, 10, 10.5), word('everyone', undefined, 10.6, 11.2)];
        await push(rosterPerCall[0]!, result(base, { isPartial: true, resultId: 'res-x' }));
        await push(
            rosterPerCall[1]!,
            result([...base, word('so', undefined, 11.3, 11.7)], { isPartial: true, resultId: 'res-x' }),
        );
        // The final MUST end with punctuation. That item carries no time, so
        // the roster entry this result pushes is stamped at zero, which is what
        // makes the roster timeline non-monotonic and is the whole mechanism
        // under test. An earlier version of this test ended the final with a
        // timestamped word, which left the timeline monotonic -- so it passed
        // with the defect present and guarded nothing.
        await push(
            rosterPerCall[2]!,
            result(
                [
                    word('hello', '0', 10, 10.5),
                    word('everyone', '0', 10.6, 11.2),
                    word('so', '0', 11.3, 11.7),
                    word('yes', '0', 13, 13.6),
                    { Content: '.', Type: 'punctuation' } as ReturnType<typeof word>,
                ],
                { resultId: 'res-x' },
            ),
        );
        const finalised = new Set(all.filter((r) => !r.IsPartial).map((r) => r.SegmentId));
        return [...new Set(all.filter((r) => r.IsPartial).map((r) => r.SegmentId))].filter(
            (id) => !finalised.has(id as string),
        );
    };

    it('leaves nothing in progress when the active speaker changes mid-utterance', async () => {
        const orphans = await partialThenFinal(['Conference Room', 'Alex', 'Conference Room']);
        assert.deepEqual(orphans, []);
    });

    it('leaves nothing in progress when the active speaker is steady', async () => {
        // A happy-path check, NOT a guard: with one roster entry the splitter
        // produces one piece whether or not the partial branch guards against
        // splitting, so reverting the fix leaves this green. The test above is
        // the one that detects it; this one catches a different class of
        // breakage, where even the simple case stops finalising.
        const orphans = await partialThenFinal(['Conference Room', 'Conference Room', 'Conference Room']);
        assert.deepEqual(orphans, []);
    });

    it('passes the partial flag through, so a labelled partial cannot split', async () => {
        // Transcribe does not label partials today. If it ever did, the writer
        // must still emit one piece -- which only holds if it tells the builder
        // the result is partial.
        sent.length = 0;
        await manager.sendTranscriptSegment(
            'Conference Room',
            result([...turn('0', 6, 0), ...turn('1', 6, 10)], { isPartial: true, resultId: 'res-y' }),
            0,
        );
        const ids = sent
            .filter((record) => record.EventType === 'ADD_TRANSCRIPT_SEGMENT')
            .map((record) => record.SegmentId);
        assert.deepEqual(ids, ['res-y-r0']);
    });

    it('gives two sessions distinct fallback ids across a reconnect', async () => {
        // StartTime resets to 0 on reconnect, so the session offset has to be
        // part of the key or session 2's first result overwrites session 1's.
        const first = await emit('Conference Room', result(turn('0', 6, 0), { startTime: 0 }), 0);
        const second = await emit('Conference Room', result(turn('0', 6, 0), { startTime: 0 }), 300);
        assert.notEqual(first[0]?.SegmentId, second[0]?.SegmentId);
    });
});

describe('the separation diagnostic can be switched on in a deployment', () => {
    // It was gated on a DEBUG variable the VP container never receives, so the
    // one tool for working out why a room separated badly was unreachable
    // exactly where rooms exist. The stack sets LOG_LEVEL.
    const saved = { debug: process.env.DEBUG, level: process.env.LOG_LEVEL };

    afterEach(() => {
        if (saved.debug === undefined) delete process.env.DEBUG;
        else process.env.DEBUG = saved.debug;
        if (saved.level === undefined) delete process.env.LOG_LEVEL;
        else process.env.LOG_LEVEL = saved.level;
    });

    it('logs the per-result breakdown when the deployment log level is DEBUG', async () => {
        delete process.env.DEBUG;
        process.env.LOG_LEVEL = 'DEBUG';
        manager.setDiarization(true);
        const lines: string[] = [];
        const original = console.log;
        console.log = (...args: unknown[]) => lines.push(args.join(' '));
        try {
            await emit('Conference Room', result(turn('0', 6, 0), { resultId: 'R-log' }));
        } finally {
            console.log = original;
        }
        assert.ok(
            lines.some((line) => line.includes('[DIARIZATION]')),
            'no [DIARIZATION] line was emitted at LOG_LEVEL=DEBUG',
        );
    });

    it('stays quiet at the default log level', async () => {
        delete process.env.DEBUG;
        process.env.LOG_LEVEL = 'INFO';
        manager.setDiarization(true);
        const lines: string[] = [];
        const original = console.log;
        console.log = (...args: unknown[]) => lines.push(args.join(' '));
        try {
            await emit('Conference Room', result(turn('0', 6, 0), { resultId: 'R-quiet' }));
        } finally {
            console.log = original;
        }
        assert.ok(!lines.some((line) => line.includes('[DIARIZATION]')));
    });
});

describe('a partial after a reconnect', () => {
    // The partial branch computes its own StartTime/EndTime, and nothing
    // exercised them with a non-zero offset -- the existing offset test uses a
    // final. Transcribe resets item times to 0 on every reconnect, so a partial
    // stamped on the session-relative timeline sorts to the wrong place in the
    // live transcript.
    beforeEach(() => manager.setDiarization(true));

    it('is stamped on the meeting timeline, not the session timeline', async () => {
        const records = await emit(
            'Conference Room',
            result(turn(undefined, 4, 0), { isPartial: true, resultId: 'res-rc' }),
            300,
        );
        assert.equal(records.length, 1);
        assert.ok(
            (records[0]?.StartTime ?? 0) >= 300,
            `partial stamped at ${records[0]?.StartTime}, before the session offset`,
        );
        assert.ok((records[0]?.EndTime ?? 0) >= (records[0]?.StartTime ?? 0), 'span inverted');
    });

    it('does not collide with the same result id in a later session', async () => {
        const first = await emit(
            'Conference Room',
            result(turn(undefined, 4, 0), { isPartial: true }),
            0,
        );
        const second = await emit(
            'Conference Room',
            result(turn(undefined, 4, 0), { isPartial: true }),
            300,
        );
        assert.notEqual(first[0]?.SegmentId, second[0]?.SegmentId);
    });
});

describe('results that arrive with no id of their own', () => {
    // Transcribe always sends a ResultId, so this is near-dead code -- but it
    // has to satisfy BOTH requirements at once, and the SDK hands over a fresh
    // object per TranscriptEvent, so every test here uses DISTINCT objects.
    // An earlier version memoised an id on the result object and passed only
    // because its test mutated one object and emitted it twice, which is the
    // one shape production cannot produce.
    beforeEach(() => manager.setDiarization(true));

    it('keeps a partial and its final on the same row, as separate objects', async () => {
        // The final repeats the partial's items, so the first item's start time
        // is the thing both deliveries share.
        const partial = await emit(
            'Conference Room',
            result(turn(undefined, 4, 7), { isPartial: true }),
            0,
        );
        const final = await emit(
            'Conference Room',
            result([...turn('0', 4, 7), { Content: '.', Type: 'punctuation' } as ReturnType<typeof word>], {}),
            0,
        );
        assert.equal(partial.length, 1);
        assert.equal(
            partial[0]?.SegmentId,
            final[0]?.SegmentId,
            'the final must replace the partial, not orphan it',
        );
    });

    it('separates two results that start at different moments', async () => {
        const first = await emit('Conference Room', result(turn('0', 6, 0), {}), 0);
        const second = await emit('Conference Room', result(turn('0', 6, 40), {}), 0);
        assert.notEqual(first[0]?.SegmentId, second[0]?.SegmentId);
    });

    it('separates the same moment in two different sessions', async () => {
        // Transcribe resets item times to 0 on reconnect, so the session
        // offset is what tells the two apart.
        const first = await emit('Conference Room', result(turn('0', 6, 0), {}), 0);
        const second = await emit('Conference Room', result(turn('0', 6, 0), {}), 300);
        assert.notEqual(first[0]?.SegmentId, second[0]?.SegmentId);
    });

    it('prefers the start time the result reports, when it has one', async () => {
        const records = await emit('Conference Room', result(turn('0', 6, 10), { startTime: 99 }), 0);
        assert.ok(records[0]?.SegmentId?.includes('99'), records[0]?.SegmentId ?? '(no id)');
    });
});
