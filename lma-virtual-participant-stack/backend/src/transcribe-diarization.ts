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

/** A bare voice index, as Amazon Transcribe reports it ("0", "29"). */
const BARE_VOICE_INDEX = /^\d+$/;

/**
 * Normalise a voice id to the `spk_N` form used everywhere else in LMA.
 *
 * Only a bare index is prefixed. Amazon Transcribe reports "0"; the on-demand
 * engine reports "spk_0" already; and with voice enrollment on it reports the
 * enrolled person's NAME, which must not become "spk_Sarah Lee".
 *
 * Defined here rather than imported from the engine client so this module pulls
 * in no AWS SDK and is genuinely testable on its own -- the client reads twenty
 * environment variables and three SDKs at import time.
 */
export const formatSpeakerLabel = (raw: string | null | undefined): string | undefined => {
    const trimmed = (raw ?? '').trim();
    if (trimmed.length === 0) {
        return undefined;
    }
    return BARE_VOICE_INDEX.test(trimmed) ? `spk_${trimmed}` : trimmed;
};

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
 * the only things it needs from the caller are who the platform showed over a
 * span of the meeting timeline and how to compose the row's speaker string,
 * which it takes as functions.
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
        /**
         * The participant the platform showed for most of an absolute span of
         * the meeting timeline. Called per word, with the word's own start and
         * end, so a row is named after who was speaking during its audio.
         */
        rosterNameFor: (absoluteStart: number, absoluteEnd: number) => string;
        thresholds?: RunThresholds;
        /** The row's speaker string, given its participant, voice label and length in seconds. */
        nameFor: (rosterName: string, label: string | undefined, seconds: number) => string;
        /** True for a partial result: Transcribe labels nothing on one. */
        isPartial?: boolean;
        /**
         * False when voice separation is off: a final is then one run, split
         * only where the participant changes, and no label is read.
         */
        useLabels?: boolean;
    },
): Record<string, DiarizedSegment> => {
    const useLabels = options.useLabels ?? true;
    const runs = options.isPartial || !useLabels
        ? // Transcribe labels nothing on a partial. Ignoring any label that
          // does arrive makes the one-run-at-index-0 shape hold by
          // construction rather than by trusting the service to keep behaving
          // as measured -- and if it ever did label a partial, the extra
          // pieces would be orphaned in the transcript for ever, marked
          // in-progress, visible on screen and missing from summaries.
          [{ label: undefined, items, words: 0, startTime: 0, endTime: 0 }].map(unlabelledRun)
        : diarizedRuns(items, options.thresholds ?? DEFAULT_RUN_THRESHOLDS);

    const segments: Record<string, DiarizedSegment> = {};
    let pieceIndex = 0;
    for (const run of runs) {
        if (options.isPartial) {
            // A partial is exactly one piece, at index 0, whatever the roster
            // did under it. That is what keeps it aligned with its final.
            //
            // Splitting a partial by roster breaks the alignment, in the one
            // direction that cannot be recovered: the participant changes the
            // platform reports keep arriving while a result is in progress, so
            // a partial could emit r0 AND r1 while its final, cut differently,
            // emitted only r0. The leftover r1 then sits in the live
            // transcript for ever marked in-progress: a duplicate of words the
            // final already carried, excluded from summaries and absent from
            // the Speakers panel where it could have been corrected.
            //
            // The cost is that the live transcript shows one name -- whoever
            // the platform showed for most of the partial -- until the final
            // arrives and is split.
            const segmentId = `${options.resultId}-r0`;
            const start = firstSpokenTime(run.items, options.timeOffsetSeconds, run.startTime);
            const end = Math.max(run.endTime + options.timeOffsetSeconds, start);
            segments[segmentId] = {
                SegmentId: segmentId,
                Speaker: options.nameFor(options.rosterNameFor(start, end), undefined, end - start),
                StartTime: run.startTime + options.timeOffsetSeconds,
                EndTime: run.endTime + options.timeOffsetSeconds,
                Transcript: runTranscript(run),
            };
            return segments;
        }
        // A run says which VOICE is talking. The roster says which PARTICIPANT
        // the platform reports. Composing them means intersecting both
        // boundaries, not letting one win: a label that spans a roster change
        // -- which is what diarization under-segmentation looks like on a
        // downmixed meeting stream -- would otherwise attribute a whole turn to
        // whoever happened to be talking when the run began, losing an
        // attribution the roster had right. A turn split in two can be put
        // right with "Only this segment" on the meeting page; a turn merged
        // under one name cannot be separated at all. Splitting here can only
        // ever ADD boundaries relative to the run structure, never remove one.
        for (const piece of splitRunByRoster(run, options.timeOffsetSeconds, options.rosterNameFor)) {
            const segmentId = `${options.resultId}-r${pieceIndex}`;
            segments[segmentId] = {
                SegmentId: segmentId,
                Speaker: options.nameFor(piece.roster, run.label, piece.endTime - piece.startTime),
                StartTime: piece.startTime,
                EndTime: piece.endTime,
                Transcript: runTranscript({ ...run, items: piece.items }),
            };
            pieceIndex += 1;
        }
    }
    return segments;
};

/** One contiguous stretch of a run attributed to a single roster participant. */
type RosterPiece = {
    roster: string;
    items: TranscribeItem[];
    startTime: number;
    endTime: number;
};

const unlabelledRun = (run: SpeakerRun): SpeakerRun => {
    const { startTime, endTime } = runTimes(run.items);
    return { ...run, label: undefined, startTime, endTime };
};

/**
 * Cut a run wherever the roster participant changes under it.
 *
 * Unlabelled and untimed items (punctuation) stay with the piece in progress,
 * so no text moves and no piece is created for them alone.
 */
const splitRunByRoster = (
    run: SpeakerRun,
    timeOffsetSeconds: number,
    rosterNameFor: (absoluteStart: number, absoluteEnd: number) => string,
): RosterPiece[] => {
    // Where a leading item has no time of its own -- punctuation, or any item
    // Transcribe did not timestamp -- the piece takes the run's first SPOKEN
    // time. Reading `?? 0` as a real time instead resolved the roster at the
    // start of the SESSION, and seeded the piece with the whole run's span, so
    // a run beginning with punctuation produced a segment that overlapped the
    // next one entirely and could be attributed to the wrong participant.
    const anchor = firstSpokenTime(run.items, timeOffsetSeconds, run.startTime);
    const pieces: RosterPiece[] = [];
    for (const item of run.items) {
        const timed = item.Type === 'pronunciation' && item.StartTime !== undefined;
        const start = timed ? (item.StartTime as number) + timeOffsetSeconds : anchor;
        const end = timed ? (item.EndTime ?? item.StartTime ?? 0) + timeOffsetSeconds : anchor;
        const current = pieces.length > 0 ? pieces[pieces.length - 1] : undefined;
        if (current !== undefined && !timed) {
            current.items.push(item);
            continue;
        }
        // Each word goes to whoever the platform showed for most of the word.
        const roster = rosterNameFor(start, end);
        if (current !== undefined && roster === current.roster) {
            current.items.push(item);
            current.endTime = Math.max(current.endTime, end);
            continue;
        }
        pieces.push({ roster, items: [item], startTime: start, endTime: end });
    }
    if (pieces.length === 0) {
        // Only reachable for a run with no items at all.
        return [
            {
                roster: rosterNameFor(anchor, anchor),
                items: run.items,
                startTime: run.startTime + timeOffsetSeconds,
                endTime: run.endTime + timeOffsetSeconds,
            },
        ];
    }
    return pieces;
};

/**
 * Absolute start of the first item that carries a time.
 *
 * With no timed item at all it returns `fallback + timeOffsetSeconds`, so the
 * caller decides what "unknown" means -- and every caller must pass the run's
 * own start, because the default of 0 would mean the start of the SESSION.
 * Treating a missing timestamp that way is what put punctuation-led pieces at
 * the beginning of a meeting and resolved their speaker from the wrong end of
 * the roster.
 */
const firstSpokenTime = (
    items: TranscribeItem[],
    timeOffsetSeconds: number,
    fallback: number,
): number => {
    for (const item of items) {
        if (item.Type === 'pronunciation' && item.StartTime !== undefined) {
            return item.StartTime + timeOffsetSeconds;
        }
    }
    return fallback + timeOffsetSeconds;
};

/**
 * Which rows carry Amazon Transcribe's voice label, and so the rule for a
 * label that disagrees with the meeting platform.
 *
 * The row's NAME is always the participant the platform showed during the
 * row's audio. For remote participants that is exact -- each has their own
 * roster entry -- while Transcribe's labels on the mixed meeting audio are
 * not: on a three-person Zoom standup one label coincided with all three
 * participants. The label is therefore only a hint, and it is kept where it
 * adds something: telling apart several voices behind ONE participant, such
 * as a conference room's speakerphone.
 *
 * At any moment a label is shown under one name only: the participant it has
 * coincided with for the most speech so far in this Transcribe session. Under
 * any other participant the row is written with the bare name. So `Ana (spk_3)`
 * is not written while spk_3 belongs to Sam, while a room's voices
 * -- every one of which coincides mostly with the room -- stay `Room (spk_0)`,
 * `Room (spk_1)`.
 *
 * Labels restart with each Transcribe session, so a new instance is used per
 * session. A row already written is not renamed if a label's majority later
 * moves to someone else.
 */
export class VoiceLabelNames {
    private readonly seconds = new Map<string, Map<string, number>>();

    /** The speaker string for one row, recording its speech against the label first. */
    nameFor(rosterName: string, label: string | undefined, seconds: number): string {
        if (label === undefined) {
            return rosterName;
        }
        let perName = this.seconds.get(label);
        if (perName === undefined) {
            perName = new Map<string, number>();
            this.seconds.set(label, perName);
        }
        perName.set(rosterName, (perName.get(rosterName) ?? 0) + Math.max(seconds, 0));
        return this.ownerOf(label) === rosterName ? `${rosterName} (${label})` : rosterName;
    }

    /** The participant a label has coincided with for the most speech; the earliest wins a tie. */
    ownerOf(label: string): string | undefined {
        let owner: string | undefined;
        let most = -1;
        for (const [name, total] of this.seconds.get(label) ?? []) {
            if (total > most) {
                owner = name;
                most = total;
            }
        }
        return owner;
    }
}
