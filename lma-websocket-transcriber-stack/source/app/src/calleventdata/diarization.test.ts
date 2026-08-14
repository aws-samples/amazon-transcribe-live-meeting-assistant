/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Tests for per-channel Transcribe diarization: label handling, and the
 * run-smoothing that recovers speaker turns from a single Transcribe result.
 *
 * Every rule here was derived from measured API behaviour (see
 * test/diarization-spike.ts), so these tests pin observations, not guesses:
 *
 *   - the label is a BARE INTEGER ("0"), not the `spk_0` the batch API returns;
 *   - only FINAL results carry labels — 0 of 85 partials did;
 *   - punctuation items never carry a label;
 *   - one result routinely spans SEVERAL speaker turns (30s / 4 turns is normal
 *     in natural conversation, because there is no pause to break on);
 *   - per-item labels are NOISY — single words flip to another speaker.
 *
 * The last two are in tension, and the resolution is empirical: run lengths are
 * bimodal (spurious 1-2 words / 0.1-0.9s; real turns 6-42 words / 1.2-13.4s), so
 * sub-threshold runs are absorbed and the rest are split.
 */
import { strict as assert } from 'node:assert';
import { test } from 'node:test';
import { Item } from '@aws-sdk/client-transcribe-streaming';
import {
    DEFAULT_MAX_SEGMENT_SECONDS,
    DEFAULT_MIN_RUN_SECONDS,
    DEFAULT_MIN_RUN_WORDS,
    SpeakerRun,
    anyChannelDiarized,
    appendSpeakerLabel,
    buildSpeakerRuns,
    buildSpeakerWindows,
    diarizationEnabledFor,
    formatSpeakerLabel,
    runTranscript,
    selectEmittableWindows,
    smoothSpeakerRuns,
    speakerWindowOrigin,
} from './diarization';
import { CHANNEL_MIC, CHANNEL_SYSTEM } from './eventtypes';

// --- helpers ---------------------------------------------------------------

/** A pronunciation item with an optional speaker label. */
const word = (content: string, speaker?: string, startTime = 0, endTime = 0): Item => ({
    Content: content,
    Type: 'pronunciation',
    Speaker: speaker,
    StartTime: startTime,
    EndTime: endTime,
});

/** Punctuation: never carries a speaker label, per the measured behaviour. */
const punct = (content = '.'): Item => ({ Content: content, Type: 'punctuation' });

/**
 * Synthesize the items for a run spec `[label, words, seconds]`, laying the words
 * out evenly across the duration starting at `from`. Reproduces the shape of a
 * real run without embedding a whole transcript.
 */
const runItems = (label: number, words: number, seconds: number, from: number): Item[] => {
    const items: Item[] = [];
    const per = words > 0 ? seconds / words : 0;
    for (let i = 0; i < words; i += 1) {
        const start = from + i * per;
        items.push(word(`w${i}`, String(label), start, start + per));
    }
    return items;
};

/** Build one result's items from a list of `[label, words, seconds]` run specs. */
const resultItems = (specs: Array<[number, number, number]>): Item[] => {
    const items: Item[] = [];
    let clock = 0;
    for (const [label, words, seconds] of specs) {
        items.push(...runItems(label, words, seconds, clock));
        clock += seconds;
    }
    return items;
};

const labelsOf = (runs: SpeakerRun[]): Array<string | undefined> => runs.map((r) => r.label);

// --- channel mapping -------------------------------------------------------
// Everything else keys off this: the desktop clients interleave system audio
// first on purpose (StereoMixer.swift / .cs), and the browser worklets land on
// the same order via an off-by-one in their interleave loop. If this mapping is
// ever flipped, every user's per-channel toggle silently inverts.

test('ch_0 is the system/meeting channel and ch_1 is the microphone', () => {
    assert.equal(CHANNEL_SYSTEM, 'ch_0');
    assert.equal(CHANNEL_MIC, 'ch_1');
});

test('each channel flag routes to its own channel only', () => {
    const systemOnly = { diarizeSystemChannel: true, diarizeMicChannel: false };
    assert.equal(diarizationEnabledFor(CHANNEL_SYSTEM, systemOnly), true);
    assert.equal(diarizationEnabledFor(CHANNEL_MIC, systemOnly), false);
    const micOnly = { diarizeSystemChannel: false, diarizeMicChannel: true };
    assert.equal(diarizationEnabledFor(CHANNEL_SYSTEM, micOnly), false);
    assert.equal(diarizationEnabledFor(CHANNEL_MIC, micOnly), true);
});

test('all four per-channel combinations are honoured', () => {
    for (const [system, mic] of [[false, false], [true, false], [false, true], [true, true]]) {
        const settings = { diarizeSystemChannel: system, diarizeMicChannel: mic };
        assert.equal(diarizationEnabledFor(CHANNEL_SYSTEM, settings), system);
        assert.equal(diarizationEnabledFor(CHANNEL_MIC, settings), mic);
        assert.equal(anyChannelDiarized(settings), system || mic);
    }
});

test('anything other than an explicit true is off', () => {
    // The flags arrive as untrusted JSON, so a missing field, a string "true", or
    // a null must not switch diarization on by accident.
    assert.equal(diarizationEnabledFor(CHANNEL_SYSTEM, undefined), false);
    assert.equal(diarizationEnabledFor(CHANNEL_SYSTEM, {}), false);
    assert.equal(anyChannelDiarized(undefined), false);
    for (const bogus of ['true', 1, null, {}, []]) {
        assert.equal(
            diarizationEnabledFor(CHANNEL_SYSTEM, { diarizeSystemChannel: bogus } as never),
            false,
            `${JSON.stringify(bogus)} should not enable diarization`
        );
    }
});

test('an unknown channel id is treated as the microphone channel', () => {
    assert.equal(diarizationEnabledFor('ch_7', { diarizeMicChannel: true }), true);
    assert.equal(diarizationEnabledFor('ch_7', { diarizeSystemChannel: true }), false);
});

// --- label formatting ------------------------------------------------------

test('a bare integer label is formatted as spk_N', () => {
    assert.equal(formatSpeakerLabel('0'), 'spk_0');
    assert.equal(formatSpeakerLabel('29'), 'spk_29');
});

test('an already-prefixed label is passed through unchanged', () => {
    assert.equal(formatSpeakerLabel('spk_3'), 'spk_3');
});

test('an unusable label yields undefined rather than spk_undefined', () => {
    assert.equal(formatSpeakerLabel(undefined), undefined);
    assert.equal(formatSpeakerLabel(''), undefined);
    assert.equal(formatSpeakerLabel('   '), undefined);
});

test('the label is appended to the base speaker name, or omitted entirely', () => {
    assert.equal(appendSpeakerLabel('Other Participant', 'spk_0'), 'Other Participant (spk_0)');
    // Undefined label -> untouched base name. This is what makes an unlabelled
    // partial, and a channel with diarization off, byte-identical to the
    // pre-feature output.
    assert.equal(appendSpeakerLabel('Other Participant', undefined), 'Other Participant');
});

// --- run construction ------------------------------------------------------

test('contiguous items with the same label form one run', () => {
    const runs = buildSpeakerRuns([word('a', '0'), word('b', '0'), word('c', '0')]);
    assert.equal(runs.length, 1);
    assert.equal(runs[0].label, 'spk_0');
    assert.equal(runs[0].words, 3);
});

test('a label change starts a new run', () => {
    const runs = buildSpeakerRuns([word('a', '0'), word('b', '1'), word('c', '1')]);
    assert.deepEqual(labelsOf(runs), ['spk_0', 'spk_1']);
    assert.deepEqual(runs.map((r) => r.words), [1, 2]);
});

test('punctuation attaches to the run in progress and is not counted', () => {
    // Measured: 22 of 22 punctuation items carried no label. They must not break
    // a run, and must not count towards the word threshold.
    const runs = buildSpeakerRuns([word('a', '0'), punct(), word('b', '0')]);
    assert.equal(runs.length, 1);
    assert.equal(runs[0].words, 2);
    assert.equal(runs[0].items.length, 3);
});

test('an entirely unlabelled result is one unlabelled run', () => {
    // This is every partial result: the transcript still has to be emitted, just
    // without a speaker label.
    const runs = buildSpeakerRuns([word('a'), word('b'), punct()]);
    assert.equal(runs.length, 1);
    assert.equal(runs[0].label, undefined);
    assert.equal(runs[0].words, 0);
});

test('leading unlabelled items join the first labelled run', () => {
    const runs = buildSpeakerRuns([punct('-'), word('a', '0')]);
    assert.equal(runs.length, 1);
    assert.equal(runs[0].label, 'spk_0');
});

test('run timing comes from the items, preferring pronunciations', () => {
    const runs = buildSpeakerRuns([word('a', '0', 1.5, 2.0), word('b', '0', 2.0, 3.25)]);
    assert.equal(runs[0].startTime, 1.5);
    assert.equal(runs[0].endTime, 3.25);
});

test('transcript text is preserved with Transcribe spacing', () => {
    const runs = buildSpeakerRuns([word('Hello', '0'), word('there', '0'), punct('.')]);
    assert.equal(runTranscript(runs[0]), 'Hello there.');
});

// --- smoothing -------------------------------------------------------------

test('a single run is returned untouched', () => {
    const runs = buildSpeakerRuns(resultItems([[0, 29, 11.3]]));
    assert.deepEqual(labelsOf(smoothSpeakerRuns(runs)), ['spk_0']);
});

test('a one-word interjection is absorbed into the preceding turn', () => {
    // The exact shape measured mid-utterance: 42 real words, then a single word
    // flips to another speaker, then the real next turn. Splitting on the flip
    // would fragment the utterance.
    const runs = buildSpeakerRuns(resultItems([[0, 42, 13.4], [1, 1, 0.7], [2, 20, 6.2]]));
    assert.deepEqual(labelsOf(smoothSpeakerRuns(runs)), ['spk_0', 'spk_2']);
});

test('absorbing a run keeps every word of the transcript', () => {
    // Smoothing only relabels — it must never drop text.
    const items = resultItems([[0, 4, 2.0], [1, 1, 0.2], [2, 5, 2.5]]);
    const smoothed = smoothSpeakerRuns(buildSpeakerRuns(items));
    const before = buildSpeakerRuns(items).map(runTranscript).join(' ');
    const after = smoothed.map(runTranscript).join(' ');
    assert.equal(after.split(/\s+/).length, before.split(/\s+/).length);
});

test('a weak run at the very start is absorbed into the following turn', () => {
    const runs = buildSpeakerRuns(resultItems([[0, 1, 0.3], [1, 20, 6.0]]));
    assert.deepEqual(labelsOf(smoothSpeakerRuns(runs)), ['spk_1']);
});

test('adjacent runs that share a label after absorption are coalesced', () => {
    const runs = buildSpeakerRuns(resultItems([[0, 10, 4.0], [1, 1, 0.2], [0, 10, 4.0]]));
    assert.deepEqual(labelsOf(smoothSpeakerRuns(runs)), ['spk_0']);
});

test('when every run is weak the result stays a single segment', () => {
    // A short result of noisy one-word flips must not become three segments.
    const runs = buildSpeakerRuns(resultItems([[0, 1, 0.3], [1, 1, 0.2], [0, 1, 0.2]]));
    assert.equal(smoothSpeakerRuns(runs).length, 1);
});

test('a run must clear BOTH thresholds to stand alone', () => {
    // Enough words but too fast -> absorbed.
    const fast = buildSpeakerRuns(resultItems([[0, 20, 6.0], [1, 5, 0.2], [0, 20, 6.0]]));
    assert.deepEqual(labelsOf(smoothSpeakerRuns(fast)), ['spk_0']);
    // Long enough but too few words -> absorbed.
    const sparse = buildSpeakerRuns(resultItems([[0, 20, 6.0], [1, 1, 3.0], [0, 20, 6.0]]));
    assert.deepEqual(labelsOf(smoothSpeakerRuns(sparse)), ['spk_0']);
});

test('thresholds are configurable', () => {
    const runs = buildSpeakerRuns(resultItems([[0, 20, 6.0], [1, 5, 2.0], [0, 20, 6.0]]));
    // Default keeps the 5-word turn...
    assert.deepEqual(labelsOf(smoothSpeakerRuns(runs)), ['spk_0', 'spk_1', 'spk_0']);
    // ...a stricter threshold absorbs it.
    const strict = buildSpeakerRuns(resultItems([[0, 20, 6.0], [1, 5, 2.0], [0, 20, 6.0]]));
    assert.deepEqual(
        labelsOf(smoothSpeakerRuns(strict, { minWords: 8, minSeconds: 3.0 })),
        ['spk_0']
    );
});

test('the defaults are the measured ones', () => {
    // Spurious runs measured at 1-2 words, so the WORD threshold does the
    // filtering; 0.5s only guards against very fast fragments. If these move,
    // re-derive them from the [DIARIZATION] log lines.
    assert.equal(DEFAULT_MIN_RUN_WORDS, 3);
    assert.equal(DEFAULT_MIN_RUN_SECONDS, 0.5);
    // Transcribe caps a result near 30s and never labels a partial, so windows
    // bound how long the live view can hold one unlabelled block.
    assert.equal(DEFAULT_MAX_SEGMENT_SECONDS, 20);
});

// --- regression against the real recording ---------------------------------
// Run structures captured from an actual two-speaker recording that reproduced
// the over-merging bug (Kevin/Tommy, This Old House). Each entry is one FINAL
// result as [label, words, seconds] runs, exactly as Transcribe returned it.

test('the real over-merged recording splits into the correct turns', () => {
    const observedCh0: Array<Array<[number, number, number]>> = [
        [[0, 29, 11.3]],
        [[0, 2, 0.8]],
        [[0, 6, 1.2], [2, 15, 3.9]],
        [[2, 23, 6.9], [0, 42, 13.4], [1, 1, 0.7], [2, 20, 6.2], [0, 7, 1.8]],
        [[0, 23, 7.3], [2, 19, 6.2], [0, 24, 6.8], [1, 1, 0.1], [2, 6, 2.2], [1, 1, 0.6], [0, 14, 3.6]],
        [[0, 1, 0.7]],
    ];
    const emitted = observedCh0.flatMap((specs) =>
        labelsOf(smoothSpeakerRuns(buildSpeakerRuns(resultItems(specs))))
    );
    // 6 results -> 14 segments, alternating between the two real speakers.
    // Before smoothing+splitting these collapsed to 6 segments, three of which
    // were attributed to the wrong speaker — the reported bug.
    assert.deepEqual(emitted, [
        'spk_0',
        'spk_0',
        'spk_0', 'spk_2',
        'spk_2', 'spk_0', 'spk_2', 'spk_0',
        'spk_0', 'spk_2', 'spk_0', 'spk_2', 'spk_0',
        'spk_0',
    ]);
    // Every spurious spk_1 run (1 word, <=0.7s) is gone.
    assert.ok(!emitted.includes('spk_1'), 'spurious single-word label survived smoothing');
});

test('a single-speaker microphone channel does not get split', () => {
    // Same recording, ch_1: one person at the mic, but Transcribe invented two
    // speakers around a 10-word run. Without smoothing this is 3 bogus segments.
    const observedCh1: Array<[number, number, number]> = [[0, 1, 0.9], [1, 10, 2.7], [0, 1, 0.7]];
    const raw = buildSpeakerRuns(resultItems(observedCh1));
    assert.equal(raw.length, 3, 'precondition: Transcribe really did return three runs');
    assert.equal(smoothSpeakerRuns(raw).length, 1, 'a lone speaker must stay one segment');
});

// --- windowing: bounding how long a live segment stays unlabelled ----------
// Amazon Transcribe caps a result near 30s (three of five results in a measured
// call ended at exactly 29.9s) and never labels a partial, so without windows the
// live transcript holds one unlabelled block for that long.

test('a short result is a single window', () => {
    const windows = buildSpeakerWindows(resultItems([[0, 10, 5.0]]), true);
    assert.equal(windows.length, 1);
    assert.equal(windows[0].index, 0);
    assert.equal(windows[0].settled, true);
});

test('a result longer than the bound is chunked', () => {
    // 45s of one speaker -> three 20s windows.
    const windows = buildSpeakerWindows(resultItems([[0, 90, 45.0]]), true);
    assert.equal(windows.length, 3);
    assert.deepEqual(windows.map((w) => w.index), [0, 1, 2]);
});

test('on a partial, past windows settle and the last stays in progress', () => {
    // This is the whole point: the live view can close out everything except the
    // window still receiving audio, instead of waiting up to 30s for the final.
    const windows = buildSpeakerWindows(resultItems([[0, 90, 45.0]]), false);
    assert.deepEqual(windows.map((w) => w.settled), [true, true, false]);
});

test('on a final result every window is settled', () => {
    const windows = buildSpeakerWindows(resultItems([[0, 90, 45.0]]), true);
    assert.ok(windows.every((w) => w.settled));
});

test('windowing preserves every word', () => {
    const items = resultItems([[0, 90, 45.0]]);
    const windows = buildSpeakerWindows(items, true);
    const emitted = windows.flatMap((w) => w.runs).reduce((n, r) => n + r.items.length, 0);
    assert.equal(emitted, items.length);
});

test('a window is still split by speaker turns inside it', () => {
    // Windowing bounds latency; it does not replace turn splitting.
    const windows = buildSpeakerWindows(resultItems([[0, 20, 6.0], [1, 20, 6.0]]), true);
    assert.equal(windows.length, 1);
    assert.deepEqual(labelsOf(windows[0].runs), ['spk_0', 'spk_1']);
});

test('punctuation stays in the window of the word it follows', () => {
    // Punctuation timing is unreliable, so placing it by its own timestamp could
    // strand a full stop in the next window.
    const items = [...resultItems([[0, 40, 19.5]]), punct('.')];
    const windows = buildSpeakerWindows(items, true);
    const last = windows[windows.length - 1];
    assert.equal(last.runs[last.runs.length - 1].items.slice(-1)[0].Type, 'punctuation');
});

test('setting the bound to zero disables windowing', () => {
    // Escape hatch: follow Transcribe's own result boundaries exactly.
    const windows = buildSpeakerWindows(
        resultItems([[0, 200, 90.0]]),
        true,
        { minWords: DEFAULT_MIN_RUN_WORDS, minSeconds: DEFAULT_MIN_RUN_SECONDS },
        0
    );
    assert.equal(windows.length, 1);
});

test('an empty result yields no windows', () => {
    assert.deepEqual(buildSpeakerWindows([], true), []);
});

test('the real 30s-capped results are bounded to 20s windows', () => {
    // Verbatim from the [DIARIZATION] logs of a real call: Transcribe returned a
    // 101-word run spanning 29.9s, which is what made the live view sit on one
    // unlabelled block. It must now break into two windows.
    const windows = buildSpeakerWindows(resultItems([[1, 101, 29.9]]), false);
    assert.equal(windows.length, 2);
    assert.deepEqual(windows.map((w) => w.settled), [true, false]);
});

// --- window anchor stability -----------------------------------------------
// Windows are measured from an anchor. If that anchor moves between partials, a
// boundary-adjacent item lands in a different window, its segment id changes, and
// the previous id is never written again — leaving an orphan that still shows in
// the live transcript but is dropped from summaries, because
// fetch_transcript filters on IsPartial == false.

test('the anchor is the earliest pronunciation start time', () => {
    assert.equal(speakerWindowOrigin([word('a', '0', 4.5, 5.0), word('b', '0', 1.5, 2.0)]), 1.5);
});

test('the anchor ignores punctuation, whose timing is unreliable', () => {
    assert.equal(speakerWindowOrigin([punct('.'), word('a', '0', 3.0, 3.5)]), 3.0);
});

test('an all-punctuation result still yields a usable anchor', () => {
    assert.equal(speakerWindowOrigin([punct('.')]), 0);
    assert.equal(speakerWindowOrigin([]), 0);
});

test('a drifting anchor WOULD move a boundary-adjacent item between windows', () => {
    // Demonstrates the hazard the pinning exists to prevent: the same item, with
    // only the anchor differing, lands in a different window.
    const boundaryWord = [word('start', '0', 0.0, 0.1), word('edge', '0', 20.1, 20.6)];
    const withZeroAnchor = buildSpeakerWindows(boundaryWord, false, undefined, 20, 0.0);
    const withDriftedAnchor = buildSpeakerWindows(boundaryWord, false, undefined, 20, 0.3);
    assert.equal(withZeroAnchor.length, 2, 'anchor 0.0 puts the edge word in window 1');
    assert.equal(withDriftedAnchor.length, 1, 'anchor 0.3 pulls it back into window 0');
});

test('passing a pinned anchor keeps window assignment identical across events', () => {
    // The real protection: the caller pins the anchor once, so a later event that
    // would have derived a different one still groups items the same way.
    const pinned = 0.0;
    const firstSighting = [word('start', '0', 0.0, 0.1), word('edge', '0', 20.1, 20.6)];
    // Same audio, but Transcribe has revised the first word's start time later.
    const revised = [word('start', '0', 0.3, 0.4), word('edge', '0', 20.1, 20.6)];
    const a = buildSpeakerWindows(firstSighting, false, undefined, 20, pinned);
    const b = buildSpeakerWindows(revised, false, undefined, 20, pinned);
    assert.deepEqual(
        a.map((w) => w.index),
        b.map((w) => w.index),
        'a pinned anchor must survive a revised first timestamp'
    );
});

// --- emitting settled windows exactly once ---------------------------------
// An earlier revision of this logic re-emitted every settled window on every
// subsequent partial — roughly twenty Kinesis records per segment per result.

test('a settled window is emitted once, not on every later partial', () => {
    const windows = buildSpeakerWindows(resultItems([[0, 90, 45.0]]), false);
    assert.deepEqual(windows.map((w) => w.settled), [true, true, false]);

    // First partial that crosses into window 2: everything is new.
    const first = selectEmittableWindows(windows, true, 0);
    assert.deepEqual(first.emit.map((w) => w.index), [0, 1, 2]);
    assert.equal(first.settledEmitted, 2);

    // Next partial for the same result: the two settled windows are already
    // written and must not go out again.
    const second = selectEmittableWindows(windows, true, first.settledEmitted);
    assert.deepEqual(second.emit.map((w) => w.index), [2], 'only the in-progress window');
});

test('the final result re-emits every window so labels can be applied', () => {
    // Labels only exist on the final, so the settled windows have to be written
    // once more to gain their (spk_N) suffix.
    const windows = buildSpeakerWindows(resultItems([[0, 90, 45.0]]), true);
    const selection = selectEmittableWindows(windows, false, 2);
    assert.deepEqual(selection.emit.map((w) => w.index), [0, 1, 2]);
});

test('the settled high-water mark never goes backwards', () => {
    // If Transcribe revises a result's text shorter, a previously settled window
    // must not be re-emitted (which would flip an already-final segment back to
    // partial, and fetch_transcript drops partials from summaries).
    const shrunk = buildSpeakerWindows(resultItems([[0, 10, 5.0]]), false);
    const selection = selectEmittableWindows(shrunk, true, 2);
    assert.equal(selection.settledEmitted, 2, 'mark must not decrease');
});
