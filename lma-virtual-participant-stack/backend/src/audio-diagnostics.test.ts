/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Tests for the outbound-audio classifier used to diagnose GitHub #543.
 *
 * These pin the thresholds that decide whether a live session's audio is leaving
 * cleanly. That matters twice over: three previous fixes were shipped on reasoning
 * rather than measurement (one of them broke transcription), and then the FIRST
 * version of this classifier produced false alarms that would have justified a
 * fourth wrong fix. The numbers in these tests are taken verbatim from the live
 * Teams capture so that specific misreading cannot come back.
 */
import { strict as assert } from 'node:assert';
import { test } from 'node:test';
import {
    classifyAudioWindow,
    formatDeviceSpecs,
    parsePactlShort,
    EXPECTED_PACKETS_PER_SECOND,
    MIN_CAPTURE_RATIO,
    MIN_MEASURE_SECONDS,
    SPEECH_ENERGY_THRESHOLD,
    STATS_INTERVAL_MS,
} from './audio-diagnostics.js';

/** Long enough that cumulative capture is judged rather than skipped. */
const measured = { elapsedSeconds: 60, cumulativeCaptureRatio: 1.0 };
/** A window with no audio energy: the sender is deliberately silent. */
const silent = { seconds: 1, packets: 5, captureRatio: 1.0, energyDelta: 0, ...measured };
/** A window mid-utterance on a healthy sender. */
const speaking = { seconds: 1, packets: 50, captureRatio: 1.0, energyDelta: 0.02, ...measured };

test('the expected cadence matches Opus at a 20ms frame', () => {
    // 1000ms / 20ms = 50 packets per second.
    assert.equal(EXPECTED_PACKETS_PER_SECOND, 50);
});

test('DTX comfort noise during silence is NOT a problem', () => {
    // Regression for the false alarm that filled the first live capture: 4.8
    // packets/s for ten minutes, every line reported "encoder or capture starving".
    // A DTX sender that has nothing to say is healthy, and drowning the log in
    // this hid whether anything real was happening.
    for (const packets of [4, 5, 6]) {
        const v = classifyAudioWindow({ ...silent, packets });
        assert.equal(v.state, 'idle', `${packets} pkt/s of comfort noise should read idle`);
        assert.match(v.reason, /silent/);
    }
});

test('a short utterance averaged over a long window is not misread as frame loss', () => {
    // The other artefact: sampling every 5s, a ~2.5s utterance at a perfect 50/s
    // reads as ~25/s. The old code called that "capture starving" — indistinguishable
    // from genuinely dropping half the frames, which was the question being asked.
    // At a 1s window the same sender is judged on capture continuity instead.
    const v = classifyAudioWindow({ seconds: 5, packets: 125, captureRatio: 1.0, energyDelta: 0.02 });
    assert.notEqual(v.state, 'problem');
});

test('capture starvation is flagged from the cumulative ratio', () => {
    // The signature actually being hunted: Chromium losing audio before the encoder
    // sees it. Independent of speech, DTX and window alignment.
    const v = classifyAudioWindow({ ...speaking, cumulativeCaptureRatio: 0.6 });
    assert.equal(v.state, 'problem');
    assert.match(v.reason, /capture starving/);
});

test('capture starvation is flagged even while silent', () => {
    // Capture runs whether or not the agent is speaking, so a gap during silence is
    // still a real fault — and it is the cheapest window in which to notice one.
    const v = classifyAudioWindow({ ...silent, cumulativeCaptureRatio: 0.5 });
    assert.equal(v.state, 'problem');
    assert.match(v.reason, /capture starving/);
});

test('the per-window ratio never produces a verdict on its own', () => {
    // Verbatim from the live capture: the per-window ratio alternated between these
    // two values on a sender whose cumulative ratio was 1.008. The first version
    // flagged every low-phase window as "capture starving". Four bogus PROBLEM
    // lines came from exactly these numbers.
    for (const ratio of [0.954, 0.959, 0.947, 0.949, 1.049, 1.061]) {
        assert.notEqual(
            classifyAudioWindow({ ...speaking, captureRatio: ratio }).state,
            'problem',
            `per-window ${ratio} must not be a verdict`,
        );
    }
});

test('cumulative capture is not judged before there is enough of it', () => {
    // Early on, the cumulative ratio carries the same quantisation as one window,
    // so judging it would just move the false alarms to the start of the meeting.
    const v = classifyAudioWindow({
        ...speaking,
        elapsedSeconds: MIN_MEASURE_SECONDS - 1,
        cumulativeCaptureRatio: 0.5,
    });
    assert.notEqual(v.state, 'problem');
});

test('a healthy cumulative ratio is accepted at the threshold', () => {
    for (const ratio of [MIN_CAPTURE_RATIO, 0.99, 1.0, 1.008, 1.01]) {
        assert.notEqual(
            classifyAudioWindow({ ...speaking, cumulativeCaptureRatio: ratio }).state,
            'problem',
            `${ratio} should be treated as healthy`,
        );
    }
});

test('the starvation message quantifies how much audio was lost', () => {
    // "0.90 vs 0.95" is not actionable; "6000ms of audio never captured" is.
    const v = classifyAudioWindow({
        ...speaking,
        elapsedSeconds: 60,
        cumulativeCaptureRatio: 0.95 - 0.05,
    });
    assert.equal(v.state, 'problem');
    assert.match(v.reason, /6000ms of audio never captured/);
});

test('far-end packet loss is reported as a wire fault, not a capture fault', () => {
    // Crackle from loss on the wire and crackle from a starved encoder need
    // opposite fixes, so the two must never collapse into one message.
    const v = classifyAudioWindow({ ...speaking, remotePacketsLost: 3 });
    assert.equal(v.state, 'problem');
    assert.match(v.reason, /wire/);
});

test('capture starvation outranks far-end loss', () => {
    // If capture is already losing frames, the far end will also report loss; the
    // upstream cause is the actionable one.
    const v = classifyAudioWindow({ ...speaking, cumulativeCaptureRatio: 0.4, remotePacketsLost: 3 });
    assert.match(v.reason, /capture starving/);
});

test('queueing is flagged even when the cadence looks right', () => {
    const v = classifyAudioWindow({ ...speaking, sendDelayPerPacketMs: 45 });
    assert.equal(v.state, 'problem');
    assert.match(v.reason, /send delay/);
});

test('a starved cadence during speech is flagged', () => {
    const v = classifyAudioWindow({ ...speaking, packets: 20 });
    assert.equal(v.state, 'problem');
    assert.match(v.reason, /only 20\.0 pkt\/s/);
});

test('audio energy with no packets at all is flagged', () => {
    // Distinct from silence: the agent is producing sound and none of it is leaving.
    const v = classifyAudioWindow({ ...speaking, packets: 0 });
    assert.equal(v.state, 'problem');
    assert.match(v.reason, /no packets sent/);
});

test('a high cadence at the start of an utterance is not flagged', () => {
    // A DTX sender resuming mid-window legitimately overshoots 50/s. The first
    // version called this "bursting after a stall" and it was just speech onset.
    assert.equal(classifyAudioWindow({ ...speaking, packets: 62 }).state, 'ok');
});

test('missing optional stats do not produce a false alarm', () => {
    // Not every stat is present on every browser or before the first RTCP report.
    // Absence must read as "unknown", never as a fault.
    const v = classifyAudioWindow({ seconds: 1, packets: 50, energyDelta: 0.02 });
    assert.equal(v.state, 'ok');
});

test('the speech threshold sits above a silent null sink and below real speech', () => {
    // Energy is summed squared amplitude; the observed values differ by orders of
    // magnitude, so this only has to land between them.
    assert.ok(SPEECH_ENERGY_THRESHOLD > 0);
    assert.equal(classifyAudioWindow({ ...silent, energyDelta: SPEECH_ENERGY_THRESHOLD }).state, 'idle');
    assert.equal(classifyAudioWindow({ ...speaking, energyDelta: 0.01 }).state, 'ok');
});

test('the sampling interval can resolve a single utterance', () => {
    // A typical assistant reply is a few seconds. At 5s the utterance and the
    // silence around it landed in one window and the rate became meaningless.
    assert.ok(STATS_INTERVAL_MS <= 1000, 'must be <= 1s to separate speech from silence');
    assert.ok(STATS_INTERVAL_MS >= 1000, 'but not so often that polling adds load');
});

/**
 * The PulseAudio device-format probe. Its job is to show whether the virtual mic's
 * format flaps while Teams holds two captures on it with conflicting channel
 * counts — the one mechanism that fits every measurement taken so far (clean
 * container-side recording, clean packet counters, garbled listener audio).
 */
test('sink and source listings are parsed into name/format pairs', () => {
    // Verbatim shape of `pactl list short sinks` after #538 pinned the sinks.
    const out = parsePactlShort(
        [
            '0\tmeeting_audio\tmodule-null-sink.c\ts16le 1ch 16000Hz\tSUSPENDED',
            '1\tagent_output\tmodule-null-sink.c\ts16le 1ch 16000Hz\tRUNNING',
            '2\tcombined_audio\tmodule-null-sink.c\ts16le 1ch 16000Hz\tRUNNING',
        ].join('\n'),
    );
    assert.deepEqual(
        out.map((e) => e.name),
        ['meeting_audio', 'agent_output', 'combined_audio'],
    );
    assert.ok(out.every((e) => e.spec === 's16le 1ch 16000Hz'));
});

test('a device running at a DIFFERENT format is reported as-is', () => {
    // The failure being hunted: something on the capture leg pulling the pipeline
    // off 16 kHz mono. If the parser normalised this away the probe would be blind.
    const out = parsePactlShort('1\tagent_output\tmodule-null-sink.c\tfloat32le 2ch 48000Hz\tRUNNING');
    assert.equal(out[0].spec, 'float32le 2ch 48000Hz');
});

test('source-outputs are labelled even though their second column is numeric', () => {
    // This listing is the direct evidence of Teams holding two captures at once:
    // a numeric source id sits where sinks put a name, so a naive index would
    // label every client the same and the two would be indistinguishable.
    const out = parsePactlShort(
        ['0\t3\t12\tprotocol-native.c\ts16le 1ch 16000Hz', '1\t3\t12\tprotocol-native.c\ts16le 2ch 48000Hz'].join(
            '\n',
        ),
    );
    assert.equal(out.length, 2);
    assert.notEqual(out[0].name, out[1].name, 'two concurrent captures must be distinguishable');
    assert.equal(out[1].spec, 's16le 2ch 48000Hz');
});

test('blank lines and headers are ignored', () => {
    assert.deepEqual(parsePactlShort('\n\n'), []);
    assert.deepEqual(parsePactlShort('no sample spec here\n'), []);
});

test('formatting is stable so unchanged devices produce no log line', () => {
    // The probe logs only on change; an unstable rendering would emit a line every
    // tick and bury the flap it exists to catch.
    const entries = [{ name: 'agent_output', spec: 's16le 1ch 16000Hz' }];
    assert.equal(formatDeviceSpecs(entries), formatDeviceSpecs([...entries]));
    assert.match(formatDeviceSpecs(entries), /agent_output=\[s16le 1ch 16000Hz\]/);
});

test('an empty listing formats to something falsy, not the string "undefined"', () => {
    assert.equal(formatDeviceSpecs([]), '');
});
