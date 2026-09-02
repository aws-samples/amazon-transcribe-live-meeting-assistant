/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import assert from 'node:assert/strict';
import test from 'node:test';

import {
    CALIBRATION_SAMPLE_RATE,
    EmbeddedSegment,
    cosineSimilarity,
    deriveOperatingPoint,
    findDominantSegments,
} from './asr-calibration';

const seconds = (count: number): number => CALIBRATION_SAMPLE_RATE * count * 2;

/** Mono PCM: `amplitude` while speaking, near-silence otherwise. */
const track = (spans: Array<{ from: number; to: number; amplitude: number }>, totalSec: number): Buffer => {
    const pcm = Buffer.alloc(seconds(totalSec));
    for (let i = 0; i < pcm.length / 2; i += 1) {
        const at = i / CALIBRATION_SAMPLE_RATE;
        const span = spans.find((s) => at >= s.from && at < s.to);
        const amplitude = span ? span.amplitude : 20;
        // Alternate sign so the RMS reflects the amplitude rather than a DC offset.
        pcm.writeInt16LE(i % 2 === 0 ? amplitude : -amplitude, i * 2);
    }
    return pcm;
};

const embedded = (
    channel: 'ch_0' | 'ch_1',
    vector: number[],
    durationSec = 4
): EmbeddedSegment => ({ channel, durationSec, vector });

/**
 * A vector `alpha` of the way toward its channel's centre, with the remainder in a
 * dimension unique to it. Cosine similarity is then exactly `alpha_i * alpha_j`
 * within a channel and `alpha_i * alpha_j * centreSim` across channels, so a
 * fixture can state the distribution it means to test instead of hoping for one.
 */
const clustered = (
    channel: 'ch_0' | 'ch_1',
    alpha: number,
    unique: number,
    centreSim: number,
    durationSec = 4
): EmbeddedSegment => {
    const vector = new Array(12).fill(0);
    if (channel === 'ch_1') {
        vector[0] = alpha;
    } else {
        vector[0] = alpha * centreSim;
        vector[1] = alpha * Math.sqrt(1 - centreSim * centreSim);
    }
    vector[2 + unique] = Math.sqrt(1 - alpha * alpha);
    return { channel, durationSec, vector };
};

test('cosine similarity is 1 for identical and 0 for orthogonal vectors', () => {
    assert.equal(cosineSimilarity([1, 0], [1, 0]), 1);
    assert.equal(cosineSimilarity([1, 0], [0, 1]), 0);
    assert.equal(cosineSimilarity([0, 0], [1, 0]), 0);
});

test('segments are found where a channel dominates the other', () => {
    const mic = track([{ from: 1, to: 4, amplitude: 4000 }], 6);
    const tab = track([], 6);

    const segments = findDominantSegments(mic, tab, 'ch_1');

    assert.equal(segments.length, 1);
    assert.ok(segments[0].startSec >= 0.9 && segments[0].startSec <= 1.1);
    assert.ok(segments[0].endSec >= 3.8 && segments[0].endSec <= 4.2);
    assert.equal(segments[0].channel, 'ch_1');
    assert.ok(segments[0].pcm.length > 0);
});

test('a gap between words does not split one utterance', () => {
    // 150 ms of quiet mid-utterance: shorter than the hangover, so one segment.
    const mic = track(
        [
            { from: 1, to: 2, amplitude: 4000 },
            { from: 2.15, to: 3.5, amplitude: 4000 },
        ],
        6
    );
    const segments = findDominantSegments(mic, track([], 6), 'ch_1');

    assert.equal(segments.length, 1, 'the 150 ms gap should not end the segment');
});

test('simultaneous speech on both channels is excluded', () => {
    // Both loud at once: attributing it to either channel would corrupt both
    // distributions, so neither should claim it.
    const both = track([{ from: 1, to: 4, amplitude: 4000 }], 6);

    assert.equal(findDominantSegments(both, both, 'ch_1').length, 0);
});

test('utterances shorter than a second are ignored', () => {
    const mic = track([{ from: 1, to: 1.4, amplitude: 4000 }], 4);

    assert.equal(findDominantSegments(mic, track([], 4), 'ch_1').length, 0);
});

test('a silent channel yields no segments', () => {
    assert.equal(findDominantSegments(track([], 5), track([], 5), 'ch_0').length, 0);
});

test('a clean two-speaker sample yields a threshold between the distributions', () => {
    // Within a channel the vectors are near-identical; across channels orthogonal —
    // the shape real embeddings have when the model suits the audio.
    const segments = [
        embedded('ch_1', [1, 0.02]),
        embedded('ch_1', [1, 0.05]),
        embedded('ch_1', [1, 0.0]),
        embedded('ch_0', [0.02, 1]),
        embedded('ch_0', [0.0, 1]),
        embedded('ch_0', [0.05, 1]),
    ];

    const result = deriveOperatingPoint(segments);

    assert.equal(result.confidence, 'good');
    assert.ok(result.speakerThreshold !== undefined);
    assert.ok(
        result.speakerThreshold! > result.differentSpeakerP95,
        'must sit above what different speakers reach'
    );
    assert.ok(
        result.speakerThreshold! < result.sameSpeakerP5,
        'must sit below what the same speaker scores'
    );
    assert.ok(result.separation > 0.1);
});

test('overlapping distributions are reported as unusable, not papered over', () => {
    // Every pair scores about the same, whoever is speaking: the signature of an
    // embedder that does not suit this audio. A number here would merge or split
    // speakers with equal confidence.
    const segments = [
        embedded('ch_1', [1, 0.9]),
        embedded('ch_1', [0.9, 1]),
        embedded('ch_1', [1, 0.95]),
        embedded('ch_0', [0.95, 1]),
        embedded('ch_0', [1, 0.92]),
        embedded('ch_0', [0.93, 1]),
    ];

    const result = deriveOperatingPoint(segments);

    assert.equal(result.confidence, 'unusable');
    assert.equal(result.speakerThreshold, undefined);
    assert.ok(result.notes.some((note) => note.includes('overlap')));
});

test('too little audio is reported rather than guessed from', () => {
    const result = deriveOperatingPoint([embedded('ch_1', [1, 0]), embedded('ch_0', [0, 1])]);

    assert.equal(result.confidence, 'unusable');
    assert.equal(result.speakerThreshold, undefined);
    assert.ok(result.notes[0].includes('Not enough isolated speech'));
});

test('a narrow gap is flagged as weak rather than reported as good', () => {
    // Two clusters 40 degrees apart with enough in-cluster spread that the
    // distributions nearly touch: same-speaker p5 0.835 against different-speaker
    // p95 0.785, a separation of 0.05. Solved for rather than eyeballed — the first
    // attempt at this fixture actually had a wide gap.
    const segments = [
        embedded('ch_1', [1, 0, 0]),
        embedded('ch_1', [1, 0, 0.3]),
        embedded('ch_1', [1, 0, -0.3]),
        embedded('ch_0', [0.766, 0.6428, 0]),
        embedded('ch_0', [0.766, 0.6428, 0.3]),
        embedded('ch_0', [0.766, 0.6428, -0.3]),
    ];

    const result = deriveOperatingPoint(segments);

    assert.equal(result.confidence, 'weak');
    assert.ok(result.separation > 0 && result.separation < 0.1);
    assert.ok(result.notes.some((note) => note.includes('narrow')));
    // A usable threshold is still returned; it is just marked as fragile.
    assert.ok(result.speakerThreshold !== undefined);
});

test('short utterances scoring worse produce a minimum-length recommendation', () => {
    // Mirrors the real finding: every phantom speaker came from a 1.2-2.4s utterance.
    const long = [
        embedded('ch_1', [1, 0.01], 5),
        embedded('ch_1', [1, 0.02], 6),
        embedded('ch_1', [1, 0.0], 5),
        embedded('ch_1', [1, 0.03], 7),
    ];
    const short = [
        embedded('ch_1', [1, 0.6], 1.5),
        embedded('ch_1', [1, 0.7], 1.4),
        embedded('ch_1', [1, 0.65], 2.0),
    ];
    const other = [
        embedded('ch_0', [0.02, 1], 5),
        embedded('ch_0', [0.0, 1], 6),
        embedded('ch_0', [0.03, 1], 5),
    ];

    const result = deriveOperatingPoint([...long, ...short, ...other]);

    assert.equal(result.minSegmentMs, 2500);
    assert.ok(result.notes.some((note) => note.includes('under 2.5s')));
});

test('the threshold sits at the middle of the measured gap', () => {
    const segments = [
        embedded('ch_1', [1, 0.0]),
        embedded('ch_1', [1, 0.0]),
        embedded('ch_1', [1, 0.0]),
        embedded('ch_0', [0.0, 1]),
        embedded('ch_0', [0.0, 1]),
        embedded('ch_0', [0.0, 1]),
    ];

    const result = deriveOperatingPoint(segments);
    const midpoint = (result.sameSpeakerP5 + result.differentSpeakerP95) / 2;

    // A threshold hugging the different-speaker edge merged two similar voices that
    // a real sample did not contain; the midpoint keeps margin on both sides.
    assert.ok(Math.abs(result.speakerThreshold! - midpoint) <= 0.001);
});

test('a wide gap is not wasted by hugging the different-speaker edge', () => {
    // The shape measured on a real meeting: different speakers near zero, the same
    // speaker high. A pair of similar voices scoring 0.31 must not merge.
    const segments = [
        embedded('ch_1', [1, 0.02]),
        embedded('ch_1', [1, 0.05]),
        embedded('ch_0', [0.02, 1]),
        embedded('ch_0', [0.0, 1]),
        embedded('ch_0', [0.05, 1]),
    ];

    const result = deriveOperatingPoint(segments);

    assert.ok(result.separation > 0.7, `separation ${result.separation}`);
    assert.ok(
        result.speakerThreshold! > 0.35,
        `threshold ${result.speakerThreshold} must clear similar-voice pairs around 0.31`
    );
});

test('the threshold clears the highest observed different-speaker score', () => {
    // p95 leaves a tail, and with a small sample the tail is a single pair. A
    // threshold inside that tail would merge a pair the calibration itself saw.
    // Channel centres 0.83 apart: the closest cross pair scores 0.830 while p95
    // sits at 0.772, so the midpoint of the gap lands at 0.818 — inside the tail.
    const segments = [
        clustered('ch_1', 1, 0, 0.83),
        clustered('ch_1', 0.93, 1, 0.83),
        clustered('ch_1', 0.93, 2, 0.83),
        clustered('ch_0', 1, 3, 0.83),
        clustered('ch_0', 0.93, 4, 0.83),
        clustered('ch_0', 0.93, 5, 0.83),
        clustered('ch_0', 0.93, 6, 0.83),
    ];

    const result = deriveOperatingPoint(segments);

    assert.ok(result.differentSpeakerMax > result.differentSpeakerP95, 'fixture must have a tail');
    assert.ok(
        result.speakerThreshold! > result.differentSpeakerMax,
        `threshold ${result.speakerThreshold} must clear the observed max ` +
            `${result.differentSpeakerMax}`
    );
    assert.ok(result.speakerThreshold! < result.sameSpeakerP5, 'and stay under the same speaker');
    assert.ok(result.notes.some((note) => note.includes('Raised above the highest observed')));
});

test('an unclearable outlier is refused rather than half-honoured', () => {
    // Same shape, centres 0.87 apart: now the top cross pair reaches 0.870 while the
    // same speaker's worst pair is 0.865, so clearing the tail would put the
    // threshold above the same-speaker floor. No number is honest here.
    const segments = [
        clustered('ch_1', 1, 0, 0.87),
        clustered('ch_1', 0.93, 1, 0.87),
        clustered('ch_1', 0.93, 2, 0.87),
        clustered('ch_0', 1, 3, 0.87),
        clustered('ch_0', 0.93, 4, 0.87),
        clustered('ch_0', 0.93, 5, 0.87),
        clustered('ch_0', 0.93, 6, 0.87),
    ];

    const result = deriveOperatingPoint(segments);

    assert.ok(result.separation > 0, 'the distributions must not merely overlap');
    assert.equal(result.confidence, 'unusable');
    assert.equal(result.speakerThreshold, undefined);
    assert.ok(result.notes.some((note) => note.includes('reaches into the same-speaker range')));
});
