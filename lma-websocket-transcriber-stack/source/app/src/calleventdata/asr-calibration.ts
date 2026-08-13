/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Derive the diarization operating point from a deployment's own audio, using
 * audio channel as the same-speaker / different-speaker label.
 *
 * See docs/microvm-asr.md, "Calibrating the operating point", for why the
 * threshold cannot be a shipped constant and how the placement rule was chosen.
 */

export const CALIBRATION_SAMPLE_RATE = 16000;

export interface WavFormat {
    sampleRate: number;
    channels: number;
    bitsPerSample: number;
    dataOffset: number;
    dataLength: number;
}

/** A stretch of one channel where that channel's speaker is talking. */
export interface CalibrationSegment {
    channel: 'ch_0' | 'ch_1';
    startSec: number;
    endSec: number;
    pcm: Buffer;
}

export interface CalibrationDistribution {
    /** Pair counts behind the percentiles, so a thin sample is visible. */
    sameSpeakerPairs: number;
    differentSpeakerPairs: number;
    sameSpeakerP5: number;
    sameSpeakerMedian: number;
    differentSpeakerMedian: number;
    differentSpeakerP95: number;
    differentSpeakerMax: number;
}

export interface CalibrationResult extends CalibrationDistribution {
    /** Recommended threshold, or undefined when the sample cannot support one. */
    speakerThreshold?: number;
    /** Recommended minimum utterance length, when the data shows one. */
    minSegmentMs?: number;
    /** Margin between the distributions; <= 0 means they overlap. */
    separation: number;
    /** Plain-language verdict for the UI. */
    confidence: 'good' | 'weak' | 'unusable';
    notes: string[];
}

const BYTES_PER_SAMPLE = 2;

const FRAME_MS = 20;
// Without a hangover this long, normal speech never forms a one-second run: the
// sub-200 ms gaps between words end it.
const HANGOVER_MS = 300;
const MIN_SEGMENT_SEC = 1.0;
const MAX_SEGMENT_SEC = 12.0;
const DOMINANCE_RATIO = 3;

const rms = (pcm: Buffer, from: number, to: number): number => {
    let total = 0;
    let count = 0;
    for (let offset = from; offset + 1 < to; offset += BYTES_PER_SAMPLE) {
        const sample = pcm.readInt16LE(offset);
        total += sample * sample;
        count += 1;
    }
    return count === 0 ? 0 : Math.sqrt(total / count);
};

const median = (values: number[]): number => {
    if (values.length === 0) {
        return 0;
    }
    const sorted = [...values].sort((a, b) => a - b);
    return sorted[Math.floor(sorted.length / 2)];
};

const percentile = (values: number[], fraction: number): number => {
    if (values.length === 0) {
        return NaN;
    }
    const sorted = [...values].sort((a, b) => a - b);
    const index = Math.min(sorted.length - 1, Math.max(0, Math.round(fraction * (sorted.length - 1))));
    return sorted[index];
};

export const cosineSimilarity = (a: number[], b: number[]): number => {
    let dot = 0;
    let normA = 0;
    let normB = 0;
    for (let i = 0; i < Math.min(a.length, b.length); i += 1) {
        dot += a[i] * b[i];
        normA += a[i] * a[i];
        normB += b[i] * b[i];
    }
    if (normA === 0 || normB === 0) {
        return 0;
    }
    return dot / (Math.sqrt(normA) * Math.sqrt(normB));
};

/**
 * Dominance rather than silence: both channels carry room noise, and a segment
 * containing both voices would corrupt both distributions.
 */
export const findDominantSegments = (
    channelPcm: Buffer,
    otherPcm: Buffer,
    channel: 'ch_0' | 'ch_1',
    sampleRate = CALIBRATION_SAMPLE_RATE
): CalibrationSegment[] => {
    const frameBytes = (sampleRate * FRAME_MS) / 1000 * BYTES_PER_SAMPLE;
    const frames = Math.floor(Math.min(channelPcm.length, otherPcm.length) / frameBytes);
    if (frames === 0) {
        return [];
    }

    const own: number[] = [];
    const other: number[] = [];
    for (let frame = 0; frame < frames; frame += 1) {
        const from = frame * frameBytes;
        own.push(rms(channelPcm, from, from + frameBytes));
        other.push(rms(otherPcm, from, from + frameBytes));
    }

    const speaking = own.filter((value) => value > 150);
    if (speaking.length === 0) {
        return [];
    }
    const floor = median(speaking) * 0.5;

    const segments: CalibrationSegment[] = [];
    let runStart: number | null = null;
    let quietFrames = 0;
    const hangoverFrames = HANGOVER_MS / FRAME_MS;

    const closeRun = (endFrame: number): void => {
        if (runStart === null) {
            return;
        }
        const startSec = (runStart * FRAME_MS) / 1000;
        const endSec = Math.min((endFrame * FRAME_MS) / 1000, startSec + MAX_SEGMENT_SEC);
        if (endSec - startSec >= MIN_SEGMENT_SEC) {
            segments.push({
                channel,
                startSec,
                endSec,
                pcm: channelPcm.subarray(
                    Math.floor(startSec * sampleRate) * BYTES_PER_SAMPLE,
                    Math.floor(endSec * sampleRate) * BYTES_PER_SAMPLE
                ),
            });
        }
        runStart = null;
        quietFrames = 0;
    };

    for (let frame = 0; frame < frames; frame += 1) {
        const dominant = own[frame] > floor && own[frame] > DOMINANCE_RATIO * Math.max(other[frame], 1);
        if (dominant) {
            if (runStart === null) {
                runStart = frame;
            }
            quietFrames = 0;
        } else if (runStart !== null) {
            quietFrames += 1;
            if (quietFrames > hangoverFrames) {
                closeRun(frame - quietFrames);
            }
        }
    }
    closeRun(frames);
    return segments;
};

export const segmentDurationSec = (segment: CalibrationSegment): number =>
    segment.endSec - segment.startSec;

/** At most `limit` items, taken evenly across the list rather than from the front. */
export const selectSpread = <T>(items: T[], limit: number): T[] => {
    if (limit <= 0) {
        return [];
    }
    if (items.length <= limit) {
        return items;
    }
    const picked: T[] = [];
    for (let i = 0; i < limit; i += 1) {
        picked.push(items[Math.round((i * (items.length - 1)) / (limit - 1))]);
    }
    return picked;
};

/**
 * The segments worth embedding from a recording's two 16 kHz mono channels.
 *
 * Capped per channel because embedding is the expensive step and because the
 * statistics stop improving well before a long meeting runs out of speech.
 */
export const collectCalibrationSegments = (
    channel0: Buffer,
    channel1: Buffer,
    maxPerChannel: number
): CalibrationSegment[] => [
    ...selectSpread(findDominantSegments(channel0, channel1, 'ch_0'), maxPerChannel),
    ...selectSpread(findDominantSegments(channel1, channel0, 'ch_1'), maxPerChannel),
];

/**
 * Read a RIFF/WAVE header, walking the chunk list rather than assuming the
 * canonical 44-byte layout, since a writer may put LIST or fact chunks before the
 * audio. Returns undefined when the buffer does not hold the whole header yet, so
 * a caller streaming the object can feed it more bytes and ask again.
 */
export const parseWavHeader = (buffer: Buffer): WavFormat | undefined => {
    if (buffer.length < 12) {
        return undefined;
    }
    if (buffer.toString('ascii', 0, 4) !== 'RIFF' || buffer.toString('ascii', 8, 12) !== 'WAVE') {
        throw new Error('not a RIFF/WAVE recording');
    }

    let audioFormat = 0;
    let sampleRate = 0;
    let channels = 0;
    let bitsPerSample = 0;
    let offset = 12;

    while (offset + 8 <= buffer.length) {
        const chunkId = buffer.toString('ascii', offset, offset + 4);
        const chunkSize = buffer.readUInt32LE(offset + 4);
        const body = offset + 8;

        if (chunkId === 'fmt ') {
            if (body + 16 > buffer.length) {
                return undefined;
            }
            audioFormat = buffer.readUInt16LE(body);
            channels = buffer.readUInt16LE(body + 2);
            sampleRate = buffer.readUInt32LE(body + 4);
            bitsPerSample = buffer.readUInt16LE(body + 14);
        } else if (chunkId === 'data') {
            if (sampleRate === 0) {
                throw new Error('malformed recording: audio data before its format chunk');
            }
            if (audioFormat !== 1 || bitsPerSample !== 16) {
                throw new Error(
                    `unsupported recording encoding (format ${audioFormat}, ${bitsPerSample}-bit); ` +
                        'calibration needs 16-bit PCM'
                );
            }
            return { sampleRate, channels, bitsPerSample, dataOffset: body, dataLength: chunkSize };
        }

        // Chunks are word-aligned: an odd size is followed by a pad byte.
        offset = body + chunkSize + (chunkSize % 2);
    }
    return undefined;
};

export interface EmbeddedSegment {
    channel: 'ch_0' | 'ch_1';
    durationSec: number;
    vector: number[];
}

/**
 * Turn embeddings of channel-labelled segments into an operating point.
 *
 * The threshold is placed between the two distributions rather than at either
 * edge: above what different speakers reach, below what the same speaker scores.
 * When they overlap there is no such point, and saying so is more useful than
 * returning a number that will fragment or merge speakers.
 */
export const deriveOperatingPoint = (segments: EmbeddedSegment[]): CalibrationResult => {
    const notes: string[] = [];
    const same: number[] = [];
    const different: number[] = [];
    const sameShort: number[] = [];
    const sameLong: number[] = [];

    for (let i = 0; i < segments.length; i += 1) {
        for (let j = i + 1; j < segments.length; j += 1) {
            const similarity = cosineSimilarity(segments[i].vector, segments[j].vector);
            if (segments[i].channel === segments[j].channel) {
                same.push(similarity);
                const shorter = Math.min(segments[i].durationSec, segments[j].durationSec);
                (shorter < 2.5 ? sameShort : sameLong).push(similarity);
            } else {
                different.push(similarity);
            }
        }
    }

    const distribution: CalibrationDistribution = {
        sameSpeakerPairs: same.length,
        differentSpeakerPairs: different.length,
        sameSpeakerP5: percentile(same, 0.05),
        sameSpeakerMedian: percentile(same, 0.5),
        differentSpeakerMedian: percentile(different, 0.5),
        differentSpeakerP95: percentile(different, 0.95),
        differentSpeakerMax: different.length ? Math.max(...different) : NaN,
    };

    if (same.length < 3 || different.length < 3) {
        return {
            ...distribution,
            separation: NaN,
            confidence: 'unusable',
            notes: [
                'Not enough isolated speech to compare: needs several utterances on each ' +
                    'channel from a meeting where both sides spoke.',
            ],
        };
    }

    const sameFloor = distribution.sameSpeakerP5;
    const differentCeiling = distribution.differentSpeakerP95;
    const separation = sameFloor - differentCeiling;

    if (separation <= 0) {
        notes.push(
            'The same-speaker and different-speaker scores overlap, so no threshold ' +
                'separates them on this audio. This is what a mismatched embedder looks ' +
                'like; it can also mean narrowband audio or heavy cross-talk.'
        );
        return { ...distribution, separation, confidence: 'unusable', notes };
    }

    // Place it inside the gap, nearer the different-speaker side. Both errors are
    // real — too low merges two people, too high fragments one — but fragmentation
    // is the failure that actually happened here (one speaker became eight, then
    // twenty-two), and it makes a transcript unreadable rather than merely
    // incomplete. Merging is also partly contained: channels are diarized
    // separately, so it can only ever merge people who share one microphone.
    let threshold = Number((differentCeiling + separation * 0.4).toFixed(3));

    // p95 leaves a tail. With a small sample that tail is one or two pairs, so
    // clear the highest OBSERVED different-speaker score too: then the guarantee is
    // concrete — no pair measured here would have been merged.
    if (Number.isFinite(distribution.differentSpeakerMax) && threshold <= distribution.differentSpeakerMax) {
        const clearOfMax = Number((distribution.differentSpeakerMax + 0.005).toFixed(3));
        if (clearOfMax >= sameFloor) {
            notes.push(
                'The highest different-speaker score reaches into the same-speaker range, ' +
                    'so no threshold clears every observed pair. More audio, or a different ' +
                    'speaker model, is needed.'
            );
            return { ...distribution, separation, confidence: 'unusable', notes };
        }
        notes.push(
            'Raised above the highest observed different-speaker score ' +
                `(${distribution.differentSpeakerMax.toFixed(3)}) so no measured pair would merge.`
        );
        threshold = clearOfMax;
    }

    const confidence = separation >= 0.1 ? 'good' : 'weak';
    if (confidence === 'weak') {
        notes.push(
            `The gap between the distributions is narrow (${separation.toFixed(3)}), so the ` +
                'threshold is sensitive to the audio it was measured on.'
        );
    }

    // Short utterances embed unreliably: if pairs involving a sub-2.5s segment score
    // materially worse than long-only pairs, recommend embedding only longer ones.
    let minSegmentMs: number | undefined;
    if (sameShort.length >= 3 && sameLong.length >= 3) {
        const shortMedian = percentile(sameShort, 0.5);
        const longMedian = percentile(sameLong, 0.5);
        if (longMedian - shortMedian >= 0.05) {
            minSegmentMs = 2500;
            notes.push(
                `Utterances under 2.5s scored ${(longMedian - shortMedian).toFixed(2)} lower ` +
                    'against the same speaker than longer ones, so shorter segments are best ' +
                    'left to inherit the current speaker.'
            );
        }
    } else {
        notes.push(
            'Not enough of a mix of short and long utterances to recommend a minimum ' +
                'utterance length; the existing value is kept.'
        );
    }

    notes.push(
        `Measured over ${distribution.sameSpeakerPairs} same-speaker and ` +
            `${distribution.differentSpeakerPairs} different-speaker pairs.`
    );

    return { ...distribution, speakerThreshold: threshold, minSegmentMs, separation, confidence, notes };
};
