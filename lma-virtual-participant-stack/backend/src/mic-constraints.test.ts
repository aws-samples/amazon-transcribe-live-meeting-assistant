/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Tests for the mic constraint override (GitHub #543).
 *
 * The VP's microphone is a PulseAudio remap of Nova Sonic's synthesised speech —
 * no room, no echo path, no background noise. Chromium still ran its full WebRTC
 * audio processing over it because meeting clients ask for echo cancellation,
 * noise suppression and AGC by default, and noise suppression over steady
 * synthetic speech gates it audibly.
 *
 * Two earlier diagnoses were wrong (sink resampling, then avatar CPU). What ruled
 * them out: the container's own recording of agent_output is pristine on BOTH
 * platforms (0 discontinuities, 0 clipped samples), so the damage happens
 * downstream of the container — between agent_mic and the meeting's encoder, which
 * is where the audio processing module sits.
 */
import { strict as assert } from 'node:assert';
import { test } from 'node:test';
import { applyForcedAudioConstraints, FORCED_AUDIO_CONSTRAINTS } from './mic-constraints.js';

test('all three processing stages are disabled', () => {
    // Any one left on is enough to damage synthetic speech: the suppressor gates
    // it, the AGC pumps it, and the echo canceller subtracts against a reference
    // that does not exist for a virtual mic.
    assert.deepEqual(FORCED_AUDIO_CONSTRAINTS, {
        echoCancellation: false,
        noiseSuppression: false,
        autoGainControl: false,
    });
});

test('audio: true is widened so the flags can be attached', () => {
    const out = applyForcedAudioConstraints({ audio: true });
    assert.deepEqual(out, { audio: { ...FORCED_AUDIO_CONSTRAINTS } });
});

test('caller-requested processing is overridden, not merged permissively', () => {
    // This is the actual Teams case: it asks for the processing ON.
    const out = applyForcedAudioConstraints({
        audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
    });
    assert.deepEqual(out!.audio, { ...FORCED_AUDIO_CONSTRAINTS });
});

test('unrelated caller fields are preserved', () => {
    // The goal is to disable processing, NOT to override device selection — the
    // meeting client must still get the mic it asked for.
    const out = applyForcedAudioConstraints({
        audio: { deviceId: 'agent_mic', sampleRate: 16000, channelCount: 1, noiseSuppression: true },
    });
    assert.deepEqual(out!.audio, {
        deviceId: 'agent_mic',
        sampleRate: 16000,
        channelCount: 1,
        ...FORCED_AUDIO_CONSTRAINTS,
    });
});

test('video constraints are left completely untouched', () => {
    // The Simli avatar override owns video; this must not interfere with it.
    const video = { width: { exact: 1920 }, height: { exact: 1080 } };
    const out = applyForcedAudioConstraints({ audio: true, video });
    assert.deepEqual(out!.video, video);
});

test('a video-only request is passed through unchanged', () => {
    const input = { video: true };
    assert.equal(applyForcedAudioConstraints(input), input);
});

test('audio: false is respected — we do not force audio on', () => {
    const input = { audio: false, video: true };
    assert.equal(applyForcedAudioConstraints(input), input);
});

test('undefined constraints do not throw', () => {
    assert.equal(applyForcedAudioConstraints(undefined), undefined);
});
