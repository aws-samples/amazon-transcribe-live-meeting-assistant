/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Recover speaker turns from Amazon Transcribe's per-item speaker labels.
 *
 * The Virtual Participant has always asked Transcribe for speaker partitioning
 * (`ShowSpeakerLabel: true`) and then thrown the labels away, attributing every
 * segment to whichever participant the meeting platform reported as the active
 * speaker. That is right for remote participants, whose roster name is exact,
 * and useless for a conference room: everybody sharing one speakerphone shares
 * one roster entry, so a whole room arrives as a single speaker.
 *
 * This recovers the turn structure the labels carry, so a room is separated the
 * same way the on-demand engine separates it, and composes the two signals
 * rather than choosing between them: the roster says WHO the platform thinks is
 * talking, the voice label says WHICH voice within that, giving
 * `Conference Room (spk_0)`.
 *
 * Three properties of Transcribe's labels drive everything here, and all three
 * are the same ones the WebSocket transcriber's diarization.ts documents from
 * measurement on real meeting audio:
 *
 *  1. Labels arrive ONLY on final results. Every item on a partial is
 *     unlabelled, so a partial is always a single unlabelled run.
 *  2. One result routinely spans several turns — in natural conversation there
 *     is no pause to break on — so the turn structure has to be recovered from
 *     the items rather than taken from the result boundary.
 *  3. Per-item labels are noisy. A single word flips mid-utterance, and
 *     punctuation carries no label at all, so splitting on every change would
 *     shatter utterances into fragments.
 *
 * The thresholds below are the transcriber's measured values rather than fresh
 * guesses: on a real two-speaker recording the spurious runs were all 1-2 words
 * and 0.1-0.9 s, against real turns of 6-42 words and 1.2-13.4 s.
 */

import { formatSpeakerLabel } from './asr-microvm-client.js';

/** The subset of a Transcribe result item this module needs. */
export type TranscribeItem = {
    Content?: string;
    Type?: string;
    StartTime?: number;
    EndTime?: number;
    Speaker?: string | null;
};

/** A contiguous stretch of items attributed to one Transcribe speaker label. */
export type SpeakerRun = {
    /** Formatted label (`spk_0`), or undefined when no item in the run carried one. */
    label: string | undefined;
    /** Every item in the run, punctuation included — no text is ever dropped. */
    items: TranscribeItem[];
    /** Count of LABELLED items, i.e. the words the label is actually based on. */
    words: number;
    startTime: number;
    endTime: number;
};

export type RunThresholds = {
    minWords: number;
    minSeconds: number;
};

/** A run must reach this many labelled words to stand as its own turn. */
export const DEFAULT_MIN_RUN_WORDS = 3;

/** And this many seconds. Both must hold; see the header for the measurements. */
export const DEFAULT_MIN_RUN_SECONDS = 0.5;

export const DEFAULT_RUN_THRESHOLDS: RunThresholds = {
    minWords: DEFAULT_MIN_RUN_WORDS,
    minSeconds: DEFAULT_MIN_RUN_SECONDS,
};

/** Timing for a run, preferring pronunciation items — punctuation timing is unreliable. */
const runTimes = (items: TranscribeItem[]): { startTime: number; endTime: number } => {
    const timed = items.filter((item) => item.Type === 'pronunciation');
    const source = timed.length > 0 ? timed : items;
    let startTime = Number.POSITIVE_INFINITY;
    let endTime = 0;
    for (const item of source) {
        startTime = Math.min(startTime, item.StartTime ?? 0);
        endTime = Math.max(endTime, item.EndTime ?? 0);
    }
    return { startTime: Number.isFinite(startTime) ? startTime : 0, endTime };
};

/** Recompute a run's derived fields after its item list has changed. */
const refreshRun = (run: SpeakerRun): SpeakerRun => {
    const { startTime, endTime } = runTimes(run.items);
    run.words = run.items.filter((item) => formatSpeakerLabel(item.Speaker) !== undefined).length;
    run.startTime = startTime;
    run.endTime = endTime;
    return run;
};

/**
 * Group a result's items into contiguous runs of the same speaker label.
 *
 * Unlabelled items attach to the run in progress, so no text is dropped, and a
 * run of leading unlabelled items adopts the first label that arrives rather
 * than becoming a phantom run of its own.
 */
export const buildSpeakerRuns = (items: TranscribeItem[]): SpeakerRun[] => {
    const runs: SpeakerRun[] = [];
    for (const item of items) {
        const label = formatSpeakerLabel(item.Speaker);
        const current = runs.length > 0 ? runs[runs.length - 1] : undefined;
        if (current !== undefined && (label === undefined || current.label === label)) {
            current.items.push(item);
            continue;
        }
        if (current !== undefined && current.label === undefined) {
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
 * A run must clear BOTH thresholds to stand on its own, which is the
 * conservative choice: it favours leaving an utterance whole over splitting it
 * on a label that only held for a word or two. Items are moved between runs and
 * never dropped, so the concatenated transcript is unchanged.
 */
export const smoothSpeakerRuns = (
    runs: SpeakerRun[],
    thresholds: RunThresholds = DEFAULT_RUN_THRESHOLDS,
): SpeakerRun[] => {
    if (runs.length <= 1) {
        return runs;
    }
    const isWeak = (run: SpeakerRun): boolean =>
        run.words < thresholds.minWords || run.endTime - run.startTime < thresholds.minSeconds;

    const kept: SpeakerRun[] = [];
    // A weak run at the very front has no previous run to join, so it waits
    // here for the first strong run and is prepended to it.
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
        // Every run was weak: keep them as one run under the first label.
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

/** Concatenate a run's items into transcript text, matching Transcribe's spacing. */
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

/** Runs for a result, built and smoothed in one step. */
export const diarizedRuns = (
    items: TranscribeItem[],
    thresholds: RunThresholds = DEFAULT_RUN_THRESHOLDS,
): SpeakerRun[] => smoothSpeakerRuns(buildSpeakerRuns(items), thresholds);

/** One transcript segment, in the shape the Kinesis writer already emits. */
export type DiarizedSegment = {
    SegmentId: string;
    Speaker: string;
    StartTime: number;
    EndTime: number;
    Transcript: string;
};

/**
 * Segments for one Transcribe result, one per recovered speaker turn.
 *
 * Kept here rather than in the Kinesis writer so it can be tested without AWS:
 * the only thing it needs from the caller is how to resolve a roster name at a
 * point in time, which it takes as a function.
 *
 * Segment ids are keyed on Transcribe's own `ResultId` plus the run index, and
 * deliberately NOT on the speaker name. That is what keeps a partial aligned
 * with its final: a partial carries no labels, so it is always one run at index
 * 0 and is cleanly replaced by the final's first piece, while later pieces
 * arrive as new segments. Keying on anything derived from the label would
 * rename the segment between partial and final, orphaning the partial, which
 * would then sit in the UI for ever marked in-progress and be dropped from
 * summaries.
 */
export const buildDiarizedSegments = (
    items: TranscribeItem[],
    options: {
        resultId: string;
        timeOffsetSeconds: number;
        rosterNameAt: (absoluteStart: number) => string;
        thresholds?: RunThresholds;
        nameFor: (rosterName: string, label: string | undefined) => string;
    },
): Record<string, DiarizedSegment> => {
    const segments: Record<string, DiarizedSegment> = {};
    diarizedRuns(items, options.thresholds ?? DEFAULT_RUN_THRESHOLDS).forEach((run, runIndex) => {
        const startTime = run.startTime + options.timeOffsetSeconds;
        const segmentId = `${options.resultId}-r${runIndex}`;
        segments[segmentId] = {
            SegmentId: segmentId,
            Speaker: options.nameFor(options.rosterNameAt(startTime), run.label),
            StartTime: startTime,
            EndTime: run.endTime + options.timeOffsetSeconds,
            Transcript: runTranscript(run),
        };
    });
    return segments;
};
