/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
/**
 * Per-channel Amazon Transcribe speaker partitioning (diarization) for
 * WebSocket transcriber sessions.
 *
 * Background — why this is not a straight pass-through of `Item.Speaker`:
 *
 *  1. `ShowSpeakerLabel` is a STREAM-level flag, and the transcriber sends ONE
 *     2-channel stream (`EnableChannelIdentification`). So "diarize the mic but
 *     not the shared tab" cannot be expressed to the API. We enable the flag
 *     when EITHER channel opts in and then apply the labels only to the
 *     channel(s) that asked — hence `diarizationEnabledFor`.
 *  2. Streaming returns a BARE INTEGER ("0", "1"), not the `spk_0` form the
 *     batch API uses. We format it (`formatSpeakerLabel`).
 *  3. Labels arrive ONLY on final results — never on partials. So the label must
 *     not be part of a segment's identity in a way that differs between the
 *     partial and the final, or every utterance would leave an orphaned partial
 *     in the UI. See the `ResultId`-based segment ids in transcribe.ts.
 *  4. A single Transcribe result routinely spans SEVERAL speaker turns. In
 *     natural conversation there is no pause to break on, so results of 30s
 *     containing four turns are normal. The turn structure is present in the
 *     per-item labels and has to be recovered by splitting the result.
 *  5. Per-item labels are NOISY: single words flip to another speaker
 *     mid-utterance, and punctuation carries no label at all. Splitting on every
 *     item-label change would shatter utterances into fragments.
 *
 * Points 4 and 5 pull in opposite directions, and the resolution is measured
 * rather than guessed. Against a real two-speaker recording (see
 * test/diarization-spike.ts) the run lengths are cleanly bimodal:
 *
 *     spurious runs   1-2 words    0.1-0.9 s   (6 observed)
 *     real turns      6-42 words   1.2-13.4 s  (13 observed)
 *
 * Nothing fell between 2 and 6 words, or between 0.9 s and 1.2 s. So we build
 * contiguous runs of the same label, ABSORB the sub-threshold ones into their
 * neighbour, and split on whatever survives. On that recording this turns 6
 * over-merged segments into 14 that track the actual conversation, and collapses
 * a single-speaker microphone channel — where Transcribe had invented two
 * speakers — back to exactly one segment.
 *
 * These helpers are deliberately pure and read no environment at import time,
 * so they stay importable by the offline unit tests (same constraint as the
 * import note at the top of transcribe.ts).
 */
import { Item } from '@aws-sdk/client-transcribe-streaming';

import { CHANNEL_MIC, CHANNEL_SYSTEM, DiarizationSettings } from './eventtypes';

/** Default minimum words for a speaker run to be treated as a real turn. */
export const DEFAULT_MIN_RUN_WORDS = 3;
/**
 * Default minimum seconds for a speaker run to be treated as a real turn.
 *
 * Measured against a real recording: 0.5 keeps two extra genuine-looking turns
 * per call versus 1.0 while introducing no single-word fragments (the noise runs
 * observed were all 1-2 words, so the WORD threshold is what filters them).
 */
export const DEFAULT_MIN_RUN_SECONDS = 0.5;

/**
 * Longest stretch of audio that may sit in one in-progress (partial) segment.
 *
 * Amazon Transcribe caps a result at ~30s — three of five results in a measured
 * call ended at exactly 29.9s — and NO partial result carries speaker labels. So
 * deferring entirely to Transcribe's result boundary means the live transcript
 * shows one unlabelled block for up to 30 seconds before it splits.
 *
 * Instead each result is chunked into windows of at most this many seconds. A
 * window whose audio is already in the past (i.e. a later window has started)
 * is emitted as FINAL even though Transcribe still calls the result partial, so
 * the live view settles and splits within this bound. Set to 0 to disable and
 * follow Transcribe's boundaries exactly.
 */
export const DEFAULT_MAX_SEGMENT_SECONDS = 20;

/** A contiguous stretch of items attributed to one Transcribe speaker label. */
export type SpeakerRun = {
    /** Formatted label (`spk_0`), or undefined when no item in the run carried one. */
    label: string | undefined;
    /** Every item in the run, including unlabelled punctuation — no text is dropped. */
    items: Item[];
    /** Count of LABELLED items, i.e. the words the label is actually based on. */
    words: number;
    startTime: number;
    endTime: number;
};

export type RunThresholds = {
    minWords: number;
    minSeconds: number;
};

/**
 * Is diarization enabled for this Transcribe channel?
 *
 * `ch_0` is the system / meeting audio, anything else (i.e. `ch_1`) is the
 * microphone. Anything other than an explicit `true` is off, so a client that
 * omits the flags — or sends a garbage value — gets exactly today's behaviour.
 */
export const diarizationEnabledFor = (
    channelId: string,
    settings: DiarizationSettings | undefined
): boolean => {
    if (!settings) {
        return false;
    }
    return channelId === CHANNEL_SYSTEM
        ? settings.diarizeSystemChannel === true
        : settings.diarizeMicChannel === true;
};

/** True when at least one channel wants diarization, i.e. set `ShowSpeakerLabel`. */
export const anyChannelDiarized = (settings: DiarizationSettings | undefined): boolean =>
    diarizationEnabledFor(CHANNEL_SYSTEM, settings) || diarizationEnabledFor(CHANNEL_MIC, settings);

/**
 * Normalize a raw `Item.Speaker` into the `spk_N` form used everywhere else in
 * LMA (the batch upload path and its docs both use `spk_0`).
 *
 * Streaming returns a bare integer; batch returns `spk_0`. Accept either so this
 * keeps working if the streaming API ever changes shape. Returns undefined for
 * anything unusable rather than emitting `spk_undefined`.
 */
export const formatSpeakerLabel = (raw: string | undefined): string | undefined => {
    const trimmed = (raw ?? '').trim();
    if (trimmed.length === 0) {
        return undefined;
    }
    return trimmed.startsWith('spk_') ? trimmed : `spk_${trimmed}`;
};

/** Append a diarization label to a channel's base speaker name, if there is one. */
export const appendSpeakerLabel = (baseLabel: string, label: string | undefined): string =>
    label === undefined ? baseLabel : `${baseLabel} (${label})`;

/** Timing for a run, preferring pronunciation items (punctuation timing is unreliable). */
const runTimes = (items: Item[]): { startTime: number; endTime: number } => {
    const timed = items.filter((i) => i.Type === 'pronunciation');
    const source = timed.length > 0 ? timed : items;
    let startTime = Number.POSITIVE_INFINITY;
    let endTime = 0;
    for (const item of source) {
        startTime = Math.min(startTime, item.StartTime ?? 0);
        endTime = Math.max(endTime, item.EndTime ?? 0);
    }
    return {
        startTime: Number.isFinite(startTime) ? startTime : 0,
        endTime,
    };
};

/** Recompute the derived fields of a run after its item list has changed. */
const refreshRun = (run: SpeakerRun): SpeakerRun => {
    const { startTime, endTime } = runTimes(run.items);
    run.words = run.items.filter((i) => formatSpeakerLabel(i.Speaker) !== undefined).length;
    run.startTime = startTime;
    run.endTime = endTime;
    return run;
};

/**
 * Group a result's items into contiguous runs of the same speaker label.
 *
 * Unlabelled items (all punctuation, and every item on a partial result) attach
 * to the run in progress, so no text is ever dropped. A run of leading
 * unlabelled items adopts the first label that arrives.
 */
export const buildSpeakerRuns = (items: Item[]): SpeakerRun[] => {
    const runs: SpeakerRun[] = [];
    for (const item of items) {
        const label = formatSpeakerLabel(item.Speaker);
        const current = runs.length > 0 ? runs[runs.length - 1] : undefined;
        if (current !== undefined && (label === undefined || current.label === label)) {
            current.items.push(item);
            continue;
        }
        if (current !== undefined && current.label === undefined) {
            // Leading unlabelled items join the first labelled run rather than
            // becoming a phantom run of their own.
            current.label = label;
            current.items.push(item);
            continue;
        }
        runs.push({ label, items: [item], words: 0, startTime: 0, endTime: 0 });
    }
    return runs.map(refreshRun);
};

/**
 * Absorb sub-threshold runs into a neighbour, then coalesce adjacent runs that
 * now share a label.
 *
 * A run must clear BOTH thresholds to stand on its own. Requiring both is the
 * conservative choice: it favours leaving an utterance whole over splitting it
 * on a label that only held for a word or two.
 *
 * Only relabelling happens here — items are moved between runs, never dropped,
 * so the concatenated transcript is unchanged.
 */
export const smoothSpeakerRuns = (
    runs: SpeakerRun[],
    thresholds: RunThresholds = {
        minWords: DEFAULT_MIN_RUN_WORDS,
        minSeconds: DEFAULT_MIN_RUN_SECONDS,
    }
): SpeakerRun[] => {
    if (runs.length <= 1) {
        return runs;
    }
    const isWeak = (run: SpeakerRun): boolean =>
        run.words < thresholds.minWords || run.endTime - run.startTime < thresholds.minSeconds;

    const kept: SpeakerRun[] = [];
    // A weak run at the very front has no previous run to join, so it waits here
    // for the first strong run and is prepended to it.
    let pending: SpeakerRun | undefined;
    for (const run of runs) {
        if (isWeak(run)) {
            if (kept.length > 0) {
                kept[kept.length - 1].items.push(...run.items);
            } else if (pending !== undefined) {
                pending.items.push(...run.items);
            } else {
                pending = run;
            }
            continue;
        }
        if (pending !== undefined) {
            run.items = [...pending.items, ...run.items];
            pending = undefined;
        }
        kept.push(run);
    }
    if (pending !== undefined) {
        // Every run was weak: keep them as a single run under the first label.
        kept.push(pending);
    }

    const coalesced: SpeakerRun[] = [];
    for (const run of kept.map(refreshRun)) {
        const previous = coalesced.length > 0 ? coalesced[coalesced.length - 1] : undefined;
        if (previous !== undefined && previous.label === run.label) {
            previous.items.push(...run.items);
            refreshRun(previous);
            continue;
        }
        coalesced.push(run);
    }
    return coalesced;
};

/** Concatenate a run's items into transcript text, matching Transcribe spacing. */
export const runTranscript = (run: SpeakerRun): string => {
    let transcript = '';
    for (const item of run.items) {
        if (transcript.length > 0 && item.Type === 'pronunciation') {
            transcript += ' ';
        }
        transcript += item.Content ?? '';
    }
    return transcript;
};

/** One bounded slice of a result: the items in it, and its window index. */
export type SpeakerWindow = {
    /** 0-based index of this window within the result. */
    index: number;
    /** Speaker runs inside the window, already smoothed. */
    runs: SpeakerRun[];
    /** Runs in this window BEFORE smoothing, so "how many were absorbed" is reportable. */
    rawRunCount: number;
    /**
     * True when the window's audio is complete — either the whole result is
     * final, or a LATER window has already started, which means no more audio
     * will land in this one. Drives IsPartial per segment.
     */
    settled: boolean;
};

/**
 * Chunk a result's items into windows of at most `maxSeconds`, then split each
 * window into smoothed speaker runs.
 *
 * The windows are derived purely from item timestamps relative to the start of
 * the result, so the same item always lands in the same window whether it is
 * seen on a partial or on the final — which is what keeps segment ids stable as
 * a result is refined. `maxSeconds <= 0` disables chunking (one window).
 */
export const buildSpeakerWindows = (
    items: Item[],
    isFinalResult: boolean,
    thresholds: RunThresholds = {
        minWords: DEFAULT_MIN_RUN_WORDS,
        minSeconds: DEFAULT_MIN_RUN_SECONDS,
    },
    maxSeconds: number = DEFAULT_MAX_SEGMENT_SECONDS
): SpeakerWindow[] => {
    if (items.length === 0) {
        return [];
    }
    const grouped = new Map<number, Item[]>();
    if (maxSeconds <= 0) {
        grouped.set(0, [...items]);
    } else {
        // Anchor on the earliest item rather than result.StartTime: on a partial
        // the two can differ, and drifting the anchor would move items between
        // windows and so change segment ids mid-utterance.
        let origin = Number.POSITIVE_INFINITY;
        for (const item of items) {
            if (item.Type === 'pronunciation') {
                origin = Math.min(origin, item.StartTime ?? 0);
            }
        }
        if (!Number.isFinite(origin)) {
            origin = items[0].StartTime ?? 0;
        }
        let lastIndex = 0;
        for (const item of items) {
            // Punctuation timing is unreliable, so it stays with the window of
            // the word it follows instead of being placed on its own.
            const index =
                item.Type === 'pronunciation'
                    ? Math.max(0, Math.floor(((item.StartTime ?? 0) - origin) / maxSeconds))
                    : lastIndex;
            lastIndex = index;
            grouped.set(index, [...(grouped.get(index) ?? []), item]);
        }
    }
    const indices = [...grouped.keys()].sort((a, b) => a - b);
    const highest = indices[indices.length - 1];
    return indices.map((index) => {
        const rawRuns = buildSpeakerRuns(grouped.get(index) ?? []);
        return {
            index,
            rawRunCount: rawRuns.length,
            runs: smoothSpeakerRuns(rawRuns, thresholds),
            // The last window of a partial result is still accumulating audio;
            // everything before it cannot grow any further.
            settled: isFinalResult || index < highest,
        };
    });
};

/** Compact `spk_0x42w/13.4s` description of a run, for diagnostic logging. */
export const describeRun = (run: SpeakerRun): string =>
    `${run.label ?? 'unlabelled'}x${run.words}w/${(run.endTime - run.startTime).toFixed(1)}s`;

/** Compact description of a run list, for diagnostic logging. */
export const describeRuns = (runs: SpeakerRun[]): string =>
    runs.length === 0 ? '(none)' : runs.map(describeRun).join(', ');
