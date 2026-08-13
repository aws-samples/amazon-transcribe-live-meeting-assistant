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
 *     not enter the segment id, or every utterance would leave an orphaned
 *     partial in the UI under a different id. The suffix goes on the `Speaker`
 *     string only; segment identity is untouched.
 *  4. Per-item labels are noisy: a stray word mid-utterance can flip to another
 *     speaker, and punctuation carries no label at all. Splitting a segment per
 *     item label would shatter utterances into fragments. Instead we TALLY the
 *     labels across a segment's items and resolve one winner by majority vote,
 *     which measured 14/14 correct against a two-voice fixture where naive
 *     per-item splitting mislabelled 6.
 *
 * These helpers are deliberately pure and read no environment at import time,
 * so they stay importable by the offline unit tests (same constraint as the
 * import note at the top of transcribe.ts).
 */
import { CHANNEL_MIC, CHANNEL_SYSTEM, DiarizationSettings } from './eventtypes';

/**
 * Ordered tally of Transcribe speaker labels seen within ONE transcript segment.
 *
 * A `Map` rather than a plain object on purpose: Transcribe's labels are numeric
 * strings ("0", "1", …) and JS objects iterate integer-like keys in NUMERIC
 * order, not insertion order — which would silently break the "ties resolve to
 * the first-seen label" rule.
 */
export type SpeakerLabelTally = Map<string, number>;

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

/**
 * Record one item's speaker label against a segment's tally.
 *
 * Unlabelled items are ignored, which covers both punctuation (never labelled)
 * and every item on a partial result (also never labelled) — so no caller needs
 * an explicit `IsPartial` check.
 */
export const tallySpeakerLabel = (tally: SpeakerLabelTally, raw: string | undefined): void => {
    const label = formatSpeakerLabel(raw);
    if (label === undefined) {
        return;
    }
    tally.set(label, (tally.get(label) ?? 0) + 1);
};

/**
 * The winning label for a segment: most items win, ties go to the first seen.
 * Undefined when nothing was tallied.
 */
export const dominantSpeakerLabel = (tally: SpeakerLabelTally): string | undefined => {
    let winner: string | undefined;
    let best = 0;
    // Map iteration is insertion-ordered, so a strict `>` keeps the first-seen
    // label on a tie.
    for (const [label, count] of tally) {
        if (count > best) {
            winner = label;
            best = count;
        }
    }
    return winner;
};

/**
 * The `Speaker` string for a KDS ADD_TRANSCRIPT_SEGMENT event: the channel's
 * base speaker name with the diarization label appended, e.g.
 * `Other Participant (spk_0)`.
 *
 * Returns `baseLabel` unchanged when nothing was tallied — which is what makes
 * the "diarization off" and "partial result" paths byte-identical to the
 * pre-feature output.
 */
export const resolveSpeakerLabel = (
    baseLabel: string,
    tally: SpeakerLabelTally | undefined
): string => {
    const label = tally ? dominantSpeakerLabel(tally) : undefined;
    return label === undefined ? baseLabel : `${baseLabel} (${label})`;
};
