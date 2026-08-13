/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Tests for per-channel Transcribe diarization label handling.
 *
 * These helpers exist because Amazon Transcribe streaming does not hand us a
 * usable per-channel speaker name. Every rule below was derived from a measured
 * run against a two-voice stereo fixture (see test/diarization-spike.ts), so the
 * tests pin observed API behaviour, not guesses:
 *
 *   - the label is a BARE INTEGER ("0"), not the `spk_0` the batch API returns;
 *   - only FINAL results carry labels — 0 of 85 partials did;
 *   - punctuation items never carry a label;
 *   - per-item labels are NOISY, so a segment's label is a majority vote.
 */
import { strict as assert } from 'node:assert';
import { test } from 'node:test';
import {
    SpeakerLabelTally,
    anyChannelDiarized,
    diarizationEnabledFor,
    dominantSpeakerLabel,
    formatSpeakerLabel,
    resolveSpeakerLabel,
    tallySpeakerLabel,
} from './diarization';
import { CHANNEL_MIC, CHANNEL_SYSTEM } from './eventtypes';

/** Build a tally from a list of raw Item.Speaker values, in arrival order. */
const tallyOf = (...raw: Array<string | undefined>): SpeakerLabelTally => {
    const tally: SpeakerLabelTally = new Map();
    for (const value of raw) {
        tallySpeakerLabel(tally, value);
    }
    return tally;
};

// ---------------------------------------------------------------------------
// Channel mapping. Everything else keys off this: the desktop clients
// interleave system audio first on purpose (StereoMixer.swift / .cs), and the
// browser worklets land on the same order via an off-by-one in their interleave
// loop. If this mapping is ever flipped, every user's per-channel toggle
// silently inverts — so pin it here rather than discovering it in production.
// ---------------------------------------------------------------------------

test('ch_0 is the system/meeting channel and ch_1 is the microphone', () => {
    assert.equal(CHANNEL_SYSTEM, 'ch_0');
    assert.equal(CHANNEL_MIC, 'ch_1');
});

test('the system-channel flag routes to ch_0 only', () => {
    const settings = { diarizeSystemChannel: true, diarizeMicChannel: false };
    assert.equal(diarizationEnabledFor(CHANNEL_SYSTEM, settings), true);
    assert.equal(diarizationEnabledFor(CHANNEL_MIC, settings), false);
});

test('the microphone flag routes to ch_1 only', () => {
    const settings = { diarizeSystemChannel: false, diarizeMicChannel: true };
    assert.equal(diarizationEnabledFor(CHANNEL_SYSTEM, settings), false);
    assert.equal(diarizationEnabledFor(CHANNEL_MIC, settings), true);
});

// ---------------------------------------------------------------------------
// The four cases the feature must support: system-only, mic-only, both, neither.
// ---------------------------------------------------------------------------

test('all four per-channel combinations are honoured', () => {
    const cases: Array<[boolean, boolean]> = [
        [false, false],
        [true, false],
        [false, true],
        [true, true],
    ];
    for (const [system, mic] of cases) {
        const settings = { diarizeSystemChannel: system, diarizeMicChannel: mic };
        assert.equal(diarizationEnabledFor(CHANNEL_SYSTEM, settings), system);
        assert.equal(diarizationEnabledFor(CHANNEL_MIC, settings), mic);
        assert.equal(anyChannelDiarized(settings), system || mic);
    }
});

test('anything other than an explicit true is off', () => {
    // The flags arrive as untrusted JSON from a client, so a missing field, a
    // string "true", or a null must not switch diarization on by accident.
    assert.equal(diarizationEnabledFor(CHANNEL_SYSTEM, undefined), false);
    assert.equal(diarizationEnabledFor(CHANNEL_SYSTEM, {}), false);
    assert.equal(anyChannelDiarized(undefined), false);
    assert.equal(anyChannelDiarized({}), false);
    for (const bogus of ['true', 1, null, {}, []]) {
        const settings = { diarizeSystemChannel: bogus } as never;
        assert.equal(
            diarizationEnabledFor(CHANNEL_SYSTEM, settings),
            false,
            `${JSON.stringify(bogus)} should not enable diarization`
        );
    }
});

test('an unknown channel id is treated as the microphone channel', () => {
    // Transcribe only ever sends ch_0/ch_1 for a 2-channel stream, but the code
    // must not fall through to "enabled" if that ever changes.
    assert.equal(diarizationEnabledFor('ch_7', { diarizeMicChannel: true }), true);
    assert.equal(diarizationEnabledFor('ch_7', { diarizeSystemChannel: true }), false);
});

// ---------------------------------------------------------------------------
// Label formatting. Streaming returns "0"; LMA displays and documents spk_0.
// ---------------------------------------------------------------------------

test('a bare integer label is formatted as spk_N', () => {
    assert.equal(formatSpeakerLabel('0'), 'spk_0');
    assert.equal(formatSpeakerLabel('1'), 'spk_1');
    assert.equal(formatSpeakerLabel('29'), 'spk_29');
});

test('an already-prefixed label is passed through unchanged', () => {
    // The batch API returns this form; accept it so the helper keeps working if
    // the streaming API ever aligns with it.
    assert.equal(formatSpeakerLabel('spk_3'), 'spk_3');
});

test('an unusable label yields undefined rather than spk_undefined', () => {
    assert.equal(formatSpeakerLabel(undefined), undefined);
    assert.equal(formatSpeakerLabel(''), undefined);
    assert.equal(formatSpeakerLabel('   '), undefined);
});

// ---------------------------------------------------------------------------
// Tallying and majority vote.
// ---------------------------------------------------------------------------

test('unlabelled items are not tallied', () => {
    // Covers both punctuation (never labelled) and every item on a partial
    // result (also never labelled), which is why no caller needs an IsPartial
    // check.
    assert.equal(dominantSpeakerLabel(tallyOf(undefined, undefined, '')), undefined);
});

test('the most-seen label wins', () => {
    assert.equal(dominantSpeakerLabel(tallyOf('0', '0', '1')), 'spk_0');
    assert.equal(dominantSpeakerLabel(tallyOf('1', '0', '1')), 'spk_1');
});

test('a tie resolves to the first label seen', () => {
    // Insertion order matters here and it is why the tally is a Map: a plain
    // object iterates integer-like keys in NUMERIC order, so {"1":1,"0":1} would
    // hand the tie to spk_0 and quietly break this rule.
    assert.equal(dominantSpeakerLabel(tallyOf('1', '0')), 'spk_1');
    assert.equal(dominantSpeakerLabel(tallyOf('0', '1')), 'spk_0');
    assert.equal(dominantSpeakerLabel(tallyOf('2', '0', '2', '0')), 'spk_2');
});

// ---------------------------------------------------------------------------
// The emitted Speaker string.
// ---------------------------------------------------------------------------

test('the winning label is appended to the base speaker name', () => {
    assert.equal(
        resolveSpeakerLabel('Other Participant', tallyOf('0')),
        'Other Participant (spk_0)'
    );
    // The browser extension supplies a real participant name for ch_0; the label
    // augments it rather than replacing it.
    assert.equal(resolveSpeakerLabel('Alice', tallyOf('1', '1')), 'Alice (spk_1)');
});

test('with no labels the base name is returned untouched', () => {
    // This is what makes "diarization off" and "partial result" byte-identical to
    // the pre-feature output — there is no separate code path for either.
    assert.equal(resolveSpeakerLabel('Other Participant', new Map()), 'Other Participant');
    assert.equal(resolveSpeakerLabel('Other Participant', undefined), 'Other Participant');
    assert.equal(resolveSpeakerLabel('', new Map()), '');
});

// ---------------------------------------------------------------------------
// Regression against the measured spike run. Each case is a real per-segment
// tally observed from the two-voice fixture, including every noisy one where a
// stray word flipped to another speaker. Naive per-item attribution mislabelled
// 6 of these 14; the majority vote gets all 14 right.
// ---------------------------------------------------------------------------

test('majority vote reproduces the measured spike attribution', () => {
    const observed: Array<{ channel: string; tally: Record<string, number>; expected: string }> = [
        { channel: 'ch_1', tally: { '0': 5 }, expected: 'spk_0' },
        { channel: 'ch_1', tally: { '0': 8, '1': 1 }, expected: 'spk_0' },
        { channel: 'ch_0', tally: { '0': 18 }, expected: 'spk_0' },
        { channel: 'ch_0', tally: { '1': 8 }, expected: 'spk_1' },
        { channel: 'ch_0', tally: { '1': 10 }, expected: 'spk_1' },
        { channel: 'ch_1', tally: { '0': 3 }, expected: 'spk_0' },
        { channel: 'ch_0', tally: { '0': 17, '2': 1 }, expected: 'spk_0' },
        { channel: 'ch_1', tally: { '0': 11, '1': 1 }, expected: 'spk_0' },
        { channel: 'ch_0', tally: { '1': 16 }, expected: 'spk_1' },
        { channel: 'ch_1', tally: { '0': 4, '1': 1 }, expected: 'spk_0' },
        { channel: 'ch_1', tally: { '0': 8 }, expected: 'spk_0' },
        { channel: 'ch_0', tally: { '0': 17, '2': 1 }, expected: 'spk_0' },
        { channel: 'ch_0', tally: { '1': 8 }, expected: 'spk_1' },
        { channel: 'ch_0', tally: { '1': 9, '2': 1 }, expected: 'spk_1' },
    ];
    for (const { channel, tally, expected } of observed) {
        const raw: string[] = [];
        for (const [label, count] of Object.entries(tally)) {
            raw.push(...new Array<string>(count).fill(label));
        }
        assert.equal(
            dominantSpeakerLabel(tallyOf(...raw)),
            expected,
            `${channel} ${JSON.stringify(tally)} should resolve to ${expected}`
        );
    }
});

test('the spike fixture yields per-channel numbering, not a shared namespace', () => {
    // Measured: the same voice was label "1" on ch_0 but label "0" on ch_1, so
    // each channel numbers its speakers from zero independently. That is what
    // makes the "both channels" case readable without server-side renumbering —
    // if this ever changes, the mic channel would start reporting e.g. spk_2.
    const ch0Labels = [dominantSpeakerLabel(tallyOf('0', '0')), dominantSpeakerLabel(tallyOf('1'))];
    const ch1Labels = [dominantSpeakerLabel(tallyOf('0'))];
    assert.deepEqual(ch0Labels, ['spk_0', 'spk_1']);
    assert.deepEqual(ch1Labels, ['spk_0']);
});
