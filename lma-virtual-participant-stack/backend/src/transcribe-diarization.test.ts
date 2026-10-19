/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Tests for recovering speaker turns from Amazon Transcribe's per-item labels.
 *
 * The invariants worth pinning are the three that measurement on real meeting
 * audio established, and which a naive implementation gets wrong:
 *
 *  * no text is ever dropped or duplicated, however the runs are regrouped --
 *    smoothing moves items between runs and must never lose one;
 *  * a one- or two-word label flip is noise and must not split an utterance;
 *  * a partial result carries no labels at all, so it must come out as a single
 *    unlabelled run rather than as a phantom speaker.
 */
import { describe, it } from 'node:test';
import assert from 'node:assert';

import {
    DEFAULT_MIN_RUN_SECONDS,
    DEFAULT_MIN_RUN_WORDS,
    buildDiarizedSegments,
    buildSpeakerRuns,
    diarizedRuns,
    runTranscript,
    smoothSpeakerRuns,
    type TranscribeItem,
} from './transcribe-diarization.js';

/** A pronunciation item with an optional speaker label. */
const word = (
    content: string,
    speaker?: string,
    startTime = 0,
    endTime = 0,
): TranscribeItem => ({
    Content: content,
    Type: 'pronunciation',
    StartTime: startTime,
    EndTime: endTime,
    ...(speaker === undefined ? {} : { Speaker: speaker }),
});

/** Punctuation, which Transcribe never labels and which carries no timing. */
const punctuation = (content: string): TranscribeItem => ({
    Content: content,
    Type: 'punctuation',
});

/** `count` labelled words for one speaker, a second apart, starting at `from`. */
const turn = (speaker: string, count: number, from: number): TranscribeItem[] =>
    Array.from({ length: count }, (_unused, index) =>
        word(`w${speaker}${index}`, speaker, from + index, from + index + 0.9),
    );

describe('building runs from per-item labels', () => {
    it('groups contiguous items of the same speaker', () => {
        const runs = buildSpeakerRuns([...turn('0', 4, 0), ...turn('1', 4, 10)]);
        assert.deepEqual(
            runs.map((run) => run.label),
            ['spk_0', 'spk_1'],
        );
        assert.equal(runs[0]?.words, 4);
    });

    it('attaches punctuation to the run in progress rather than splitting on it', () => {
        // Punctuation carries no label; treating it as a change would split
        // every sentence in two.
        const runs = buildSpeakerRuns([
            ...turn('0', 3, 0),
            punctuation('.'),
            ...turn('0', 3, 4),
        ]);
        assert.equal(runs.length, 1);
        assert.equal(runs[0]?.label, 'spk_0');
    });

    it('gives leading unlabelled items to the first label that arrives', () => {
        // Rather than a phantom run with no speaker.
        const runs = buildSpeakerRuns([word('hello'), word('there'), ...turn('0', 3, 2)]);
        assert.equal(runs.length, 1);
        assert.equal(runs[0]?.label, 'spk_0');
        assert.equal(runs[0]?.items.length, 5);
    });

    it('yields one unlabelled run for a partial result', () => {
        // Transcribe labels nothing on a partial. This is the case that would
        // otherwise invent a speaker for every in-progress utterance.
        const runs = buildSpeakerRuns([word('still', undefined, 0, 1), word('talking', undefined, 1, 2)]);
        assert.equal(runs.length, 1);
        assert.equal(runs[0]?.label, undefined);
    });

    it('yields nothing for no items', () => {
        assert.deepEqual(buildSpeakerRuns([]), []);
    });

    it('reports a bare Transcribe index in the spk_N form', () => {
        // Transcribe streaming emits "0"; everything else in LMA says "spk_0".
        const runs = buildSpeakerRuns(turn('0', 3, 0));
        assert.equal(runs[0]?.label, 'spk_0');
    });
});

describe('smoothing away label noise', () => {
    it('absorbs a one-word flip into the surrounding turn', () => {
        const runs = diarizedRuns([
            ...turn('0', 6, 0),
            ...turn('1', 1, 6),
            ...turn('0', 6, 7),
        ]);
        assert.equal(runs.length, 1, 'a single flipped word must not split the utterance');
        assert.equal(runs[0]?.label, 'spk_0');
    });

    it('keeps a real turn that clears both thresholds', () => {
        const runs = diarizedRuns([...turn('0', 6, 0), ...turn('1', 6, 10)]);
        assert.deepEqual(
            runs.map((run) => run.label),
            ['spk_0', 'spk_1'],
        );
    });

    it('requires BOTH thresholds, not either', () => {
        // Enough words but over in a fraction of a second: still noise.
        const fast = [
            ...turn('0', 6, 0),
            word('a', '1', 6, 6.1),
            word('b', '1', 6.1, 6.2),
            word('c', '1', 6.2, 6.3),
            ...turn('0', 6, 8),
        ];
        assert.equal(diarizedRuns(fast).length, 1);

        // Long enough, but only two words.
        const sparse = [
            ...turn('0', 6, 0),
            word('a', '1', 6, 7),
            word('b', '1', 7, 8),
            ...turn('0', 6, 10),
        ];
        assert.equal(diarizedRuns(sparse).length, 1);
    });

    it('prepends a weak leading run to the first strong one', () => {
        const runs = diarizedRuns([word('um', '1', 0, 0.2), ...turn('0', 6, 1)]);
        assert.equal(runs.length, 1);
        assert.equal(runs[0]?.label, 'spk_0');
        assert.ok(runTranscript(runs[0]!).startsWith('um'));
    });

    it('keeps everything as one run when every run is weak', () => {
        const runs = diarizedRuns([word('a', '0', 0, 0.1), word('b', '1', 0.1, 0.2)]);
        assert.equal(runs.length, 1);
    });

    it('coalesces runs that share a label once the gap between them is absorbed', () => {
        const runs = diarizedRuns([
            ...turn('0', 6, 0),
            ...turn('1', 1, 6),
            ...turn('0', 6, 7),
            ...turn('1', 6, 20),
        ]);
        assert.deepEqual(
            runs.map((run) => run.label),
            ['spk_0', 'spk_1'],
        );
    });

    it('leaves a single run alone', () => {
        const single = buildSpeakerRuns(turn('0', 2, 0));
        assert.equal(smoothSpeakerRuns(single).length, 1);
    });

    it('respects thresholds supplied by the caller', () => {
        const items = [...turn('0', 6, 0), ...turn('1', 2, 6), ...turn('0', 6, 9)];
        assert.equal(diarizedRuns(items).length, 1);
        assert.equal(
            smoothSpeakerRuns(buildSpeakerRuns(items), { minWords: 2, minSeconds: 0.5 }).length,
            3,
        );
    });

    it('uses the documented default thresholds', () => {
        // Measured values, not guesses -- see the module header.
        assert.equal(DEFAULT_MIN_RUN_WORDS, 3);
        assert.equal(DEFAULT_MIN_RUN_SECONDS, 0.5);
    });
});

describe('the text is never altered', () => {
    const everyShape: TranscribeItem[] = [
        word('Right', '0', 0, 0.5),
        punctuation(','),
        ...turn('0', 5, 1),
        word('yes', '1', 7, 7.2),
        ...turn('0', 5, 8),
        punctuation('.'),
        ...turn('1', 6, 20),
        punctuation('?'),
    ];

    const concatenated = (runs: { items: TranscribeItem[] }[]): string =>
        runs.map((run) => run.items.map((item) => item.Content ?? '').join('|')).join('|');

    it('smoothing moves items between runs and drops none', () => {
        const before = concatenated(buildSpeakerRuns(everyShape));
        const after = concatenated(diarizedRuns(everyShape));
        assert.equal(after, before);
    });

    it('every item appears exactly once across the runs', () => {
        const runs = diarizedRuns(everyShape);
        const seen = runs.flatMap((run) => run.items);
        assert.equal(seen.length, everyShape.length);
        for (const item of everyShape) {
            assert.equal(seen.filter((candidate) => candidate === item).length, 1);
        }
    });

    it('spaces words and hugs punctuation, as Transcribe does', () => {
        const [run] = diarizedRuns([word('Hello', '0', 0, 1), word('there', '0', 1, 2), punctuation('.')]);
        assert.equal(runTranscript(run!), 'Hello there.');
    });

    it('a run of punctuation alone yields its text and no crash', () => {
        const [run] = diarizedRuns([punctuation('.')]);
        assert.equal(runTranscript(run!), '.');
    });
});

describe('timing', () => {
    it('takes a run span from its pronunciation items, ignoring punctuation', () => {
        const [run] = buildSpeakerRuns([punctuation(','), ...turn('0', 3, 5), punctuation('.')]);
        assert.equal(run?.startTime, 5);
        assert.equal(run?.endTime, 7.9);
    });

    it('falls back to the items it has when none is a pronunciation', () => {
        const [run] = buildSpeakerRuns([punctuation('.')]);
        assert.equal(run?.startTime, 0);
        assert.equal(run?.endTime, 0);
    });
});

describe('segments for a result', () => {
    const nameFor = (roster: string, label: string | undefined) =>
        label === undefined ? roster : `${roster} (${label})`;
    const build = (items: TranscribeItem[], resultId = 'res-1', offset = 0, roster = 'Conference Room') =>
        buildDiarizedSegments(items, {
            resultId,
            timeOffsetSeconds: offset,
            rosterNameAt: () => roster,
            nameFor,
        });

    it('names a room voice as the roster participant plus the voice id', () => {
        // The whole point: everyone on one speakerphone shares one roster entry,
        // so the voice id is the only thing that tells them apart.
        const segments = build([...turn('0', 6, 0), ...turn('1', 6, 10)]);
        assert.deepEqual(
            Object.values(segments).map((segment) => segment.Speaker),
            ['Conference Room (spk_0)', 'Conference Room (spk_1)'],
        );
    });

    it('leaves a partial named by the roster alone, with no voice id', () => {
        // Transcribe labels nothing on a partial, and inventing a voice id for
        // one would make the live transcript disagree with the final.
        const segments = build([word('still', undefined, 0, 1), word('talking', undefined, 1, 2)]);
        assert.deepEqual(
            Object.values(segments).map((segment) => segment.Speaker),
            ['Conference Room'],
        );
    });

    it('aligns a partial with the first piece of its final', () => {
        // THE property that keeps the UI clean. A partial is one run at index 0;
        // the final's first piece must reuse that id, or the partial is orphaned
        // and sits in the transcript for ever marked in-progress.
        const partial = build([word('Right', undefined, 0, 0.5), word('so', undefined, 0.6, 1)]);
        const final = build([...turn('0', 6, 0), ...turn('1', 6, 10)]);
        const partialIds = Object.keys(partial);
        const finalIds = Object.keys(final);
        assert.equal(partialIds.length, 1);
        assert.equal(partialIds[0], finalIds[0], 'the final must replace the partial, not orphan it');
        assert.equal(finalIds.length, 2);
    });

    it('keys ids on the result and the run index, never on the speaker', () => {
        // A label-derived id would rename the segment between partial and final.
        const segments = build([...turn('0', 6, 0), ...turn('1', 6, 10)], 'abc-123');
        assert.deepEqual(Object.keys(segments), ['abc-123-r0', 'abc-123-r1']);
        for (const id of Object.keys(segments)) {
            assert.ok(!id.includes('spk_'), `${id} embeds the voice label`);
            assert.ok(!id.includes('Conference'), `${id} embeds the roster name`);
        }
    });

    it('shifts times by the reconnect offset so ordering survives a restart', () => {
        // Transcribe resets item times to 0 on every reconnect.
        const segments = build(turn('0', 6, 0), 'res-2', 120);
        const [segment] = Object.values(segments);
        assert.equal(segment?.StartTime, 120);
        assert.ok((segment?.EndTime ?? 0) > 120);
    });

    it('attributes each turn to the roster name in effect when it began', () => {
        // Roster boundary coincides with the label boundary here, so the two
        // signals agree and there are two turns.
        const segments = buildDiarizedSegments([...turn('0', 6, 0), ...turn('1', 6, 10)], {
            resultId: 'res-3',
            timeOffsetSeconds: 0,
            rosterNameAt: (start) => (start < 10 ? 'Alex' : 'Conference Room'),
            nameFor,
        });
        assert.deepEqual(
            Object.values(segments).map((segment) => segment.Speaker),
            ['Alex (spk_0)', 'Conference Room (spk_1)'],
        );
    });

    it('honours a roster change that falls inside one voice run', () => {
        // Both signals are real and they can disagree about where a turn ends.
        // Taking the intersection means the finer boundary wins, which is the
        // conservative choice: an over-split turn can be merged in the Speakers
        // panel, a merged one cannot be separated.
        const segments = buildDiarizedSegments([...turn('0', 6, 0), ...turn('1', 6, 10)], {
            resultId: 'res-3b',
            timeOffsetSeconds: 0,
            rosterNameAt: (start) => (start < 5 ? 'Alex' : 'Conference Room'),
            nameFor,
        });
        assert.deepEqual(
            Object.values(segments).map((segment) => segment.Speaker),
            ['Alex (spk_0)', 'Conference Room (spk_0)', 'Conference Room (spk_1)'],
        );
        // Still no text lost.
        assert.equal(
            Object.values(segments)
                .map((segment) => segment.Transcript)
                .join(' ')
                .split(' ')
                .filter(Boolean).length,
            12,
        );
    });

    it('splits one voice label across a roster change rather than merging it', () => {
        // THE case the first draft got wrong. Transcribe under-segmenting two
        // remote attendees onto one label is its normal failure on a downmixed
        // stream; letting the label win attributed the second person's whole
        // turn to the first -- an attribution the roster had right, and which
        // the undiarized path gets right today.
        const segments = buildDiarizedSegments(
            [...turn('0', 6, 0), ...turn('0', 6, 10)],
            {
                resultId: 'res-4',
                timeOffsetSeconds: 0,
                rosterNameAt: (start) => (start < 10 ? 'Alex' : 'Beth'),
                nameFor,
            },
        );
        assert.deepEqual(
            Object.values(segments).map((segment) => segment.Speaker),
            ['Alex (spk_0)', 'Beth (spk_0)'],
        );
        assert.deepEqual(
            Object.values(segments).map((segment) => segment.Transcript.split(' ')[0]),
            ['w00', 'w00'],
        );
    });

    it('subdividing by roster can only add boundaries, never lose text', () => {
        const items = [...turn('0', 6, 0), punctuation('.'), ...turn('0', 6, 10)];
        const merged = buildDiarizedSegments(items, {
            resultId: 'res-5',
            timeOffsetSeconds: 0,
            rosterNameAt: () => 'Room',
            nameFor,
        });
        const split = buildDiarizedSegments(items, {
            resultId: 'res-5',
            timeOffsetSeconds: 0,
            rosterNameAt: (start) => (start < 10 ? 'Alex' : 'Beth'),
            nameFor,
        });
        assert.equal(Object.keys(merged).length, 1);
        assert.equal(Object.keys(split).length, 2);
        const text = (segments: Record<string, { Transcript: string }>) =>
            Object.values(segments)
                .map((segment) => segment.Transcript)
                .join(' ');
        assert.equal(text(split), text(merged));
    });

    it('ignores any label on a partial, so it stays one piece', () => {
        // Transcribe does not label partials. If it ever did, the extra pieces
        // would be orphaned in the transcript for ever, marked in-progress.
        const segments = buildDiarizedSegments([...turn('0', 6, 0), ...turn('1', 6, 10)], {
            resultId: 'res-6',
            timeOffsetSeconds: 0,
            rosterNameAt: () => 'Conference Room',
            nameFor,
            isPartial: true,
        });
        assert.deepEqual(Object.keys(segments), ['res-6-r0']);
        assert.equal(Object.values(segments)[0]?.Speaker, 'Conference Room');
    });

    it('a partial still splits on a roster change', () => {
        // The roster signal is available on a partial even though labels are not.
        const segments = buildDiarizedSegments(turn('0', 12, 0), {
            resultId: 'res-7',
            timeOffsetSeconds: 0,
            rosterNameAt: (start) => (start < 6 ? 'Alex' : 'Beth'),
            nameFor,
            isPartial: true,
        });
        assert.deepEqual(
            Object.values(segments).map((segment) => segment.Speaker),
            ['Alex', 'Beth'],
        );
    });

    it('carries the full text of each turn', () => {
        const segments = build([...turn('0', 3, 0), punctuation('.'), ...turn('1', 6, 10)]);
        const transcripts = Object.values(segments).map((segment) => segment.Transcript);
        assert.equal(transcripts.length, 2);
        assert.ok(transcripts[0]?.endsWith('.'), 'punctuation stays with the turn it closed');
        assert.equal(transcripts.join(' ').split(' ').filter(Boolean).length, 9);
    });

    it('yields nothing for a result with no items', () => {
        assert.deepEqual(build([]), {});
    });

    it('produces one segment when a flip was smoothed away', () => {
        const segments = build([...turn('0', 6, 0), ...turn('1', 1, 6), ...turn('0', 6, 7)]);
        assert.equal(Object.keys(segments).length, 1);
        assert.equal(Object.values(segments)[0]?.Speaker, 'Conference Room (spk_0)');
    });
});
