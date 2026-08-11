/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import assert from 'node:assert/strict';
import test from 'node:test';

import {
    ASR_SAMPLE_RATE,
    ChannelResampler,
    SpeakerNameRegistry,
    StereoDeinterleaver,
    channelToLabelSuffix,
    channelToTranscriptChannel,
} from './asr-audio';

const interleave = (left: number[], right: number[]): Buffer => {
    const out = Buffer.alloc(left.length * 4);
    for (let i = 0; i < left.length; i += 1) {
        out.writeInt16LE(left[i], i * 4);
        out.writeInt16LE(right[i], i * 4 + 2);
    }
    return out;
};

const readInt16 = (buffer: Buffer): number[] => {
    const out: number[] = [];
    for (let i = 0; i + 1 < buffer.length; i += 2) {
        out.push(buffer.readInt16LE(i));
    }
    return out;
};

const sinePcm = (freqHz: number, sampleRate: number, samples: number): Buffer => {
    const out = Buffer.alloc(samples * 2);
    for (let i = 0; i < samples; i += 1) {
        out.writeInt16LE(Math.round(8000 * Math.sin((2 * Math.PI * freqHz * i) / sampleRate)), i * 2);
    }
    return out;
};

const rms = (samples: number[]): number =>
    Math.sqrt(samples.reduce((sum, value) => sum + value * value, 0) / Math.max(samples.length, 1));

test('deinterleaver splits ch_0 from the even samples and ch_1 from the odd', () => {
    const split = new StereoDeinterleaver().split(interleave([1, 2, 3], [-1, -2, -3]));
    assert.deepEqual(readInt16(split.ch_0), [1, 2, 3]);
    assert.deepEqual(readInt16(split.ch_1), [-1, -2, -3]);
});

test('deinterleaver carries a partial frame across chunks instead of swapping channels', () => {
    const deinterleaver = new StereoDeinterleaver();
    const full = interleave([1, 2, 3, 4], [-1, -2, -3, -4]);

    // Split mid-frame: 6 bytes is one whole frame plus half of the next.
    const first = deinterleaver.split(full.subarray(0, 6));
    const second = deinterleaver.split(full.subarray(6));

    assert.deepEqual(readInt16(first.ch_0), [1]);
    assert.deepEqual(readInt16(first.ch_1), [-1]);
    assert.deepEqual(readInt16(second.ch_0), [2, 3, 4]);
    assert.deepEqual(readInt16(second.ch_1), [-2, -3, -4]);
});

test('resampler passes 16 kHz through untouched', () => {
    const resampler = new ChannelResampler(ASR_SAMPLE_RATE);
    const input = sinePcm(440, ASR_SAMPLE_RATE, 320);
    assert.equal(resampler.needsResampling, false);
    assert.deepEqual(resampler.process(input), input);
});

test('resampler converts 48 kHz to 16 kHz at a third of the sample count', () => {
    const resampler = new ChannelResampler(48000);
    const output = resampler.process(sinePcm(300, 48000, 4800));
    const samples = readInt16(output);
    assert.ok(
        Math.abs(samples.length - 1600) <= 2,
        `expected about 1600 samples, got ${samples.length}`
    );
    // A 300 Hz tone is well inside the passband, so amplitude must survive.
    assert.ok(rms(samples) > 4000, `expected the tone to survive, rms=${rms(samples)}`);
});

test('resampler output is identical whether the stream arrives whole or in chunks', () => {
    const whole = new ChannelResampler(48000).process(sinePcm(700, 48000, 9600));

    const chunked = new ChannelResampler(48000);
    const source = sinePcm(700, 48000, 9600);
    const pieces: Buffer[] = [];
    // Deliberately uneven chunk sizes, including an odd sample count.
    for (const [start, end] of [
        [0, 1000],
        [1000, 3402],
        [3402, 9600],
    ]) {
        pieces.push(chunked.process(source.subarray(start * 2, end * 2)));
    }

    assert.deepEqual(Buffer.concat(pieces), whole);
});

test('resampler low-passes so content above 8 kHz cannot alias into the band', () => {
    const passband = readInt16(new ChannelResampler(48000).process(sinePcm(1000, 48000, 9600)));
    const aliasing = readInt16(new ChannelResampler(48000).process(sinePcm(15000, 48000, 9600)));
    assert.ok(
        rms(aliasing) < rms(passband) / 10,
        `15 kHz should be attenuated: passband rms=${rms(passband)}, alias rms=${rms(aliasing)}`
    );
});

test('resampler handles a non-integer ratio and upsampling', () => {
    const down = new ChannelResampler(44100);
    const downSamples = readInt16(down.process(sinePcm(400, 44100, 44100)));
    assert.ok(
        Math.abs(downSamples.length - 16000) <= 3,
        `expected about 16000 samples, got ${downSamples.length}`
    );

    const up = new ChannelResampler(8000);
    const upSamples = readInt16(up.process(sinePcm(400, 8000, 8000)));
    assert.ok(
        Math.abs(upSamples.length - 16000) <= 3,
        `expected about 16000 samples, got ${upSamples.length}`
    );
});

test('channel identity maps to the existing transcript channels and label suffixes', () => {
    assert.equal(channelToTranscriptChannel('ch_0'), 'CALLER');
    assert.equal(channelToTranscriptChannel('ch_1'), 'AGENT');
    assert.equal(channelToLabelSuffix('ch_0'), 'tab');
    assert.equal(channelToLabelSuffix('ch_1'), 'mic');
});

test('speaker registry keeps the known name for each channel first voice', () => {
    const registry = new SpeakerNameRegistry();
    assert.equal(registry.nameFor('ch_1', 'spk_0', 'Alex'), 'Alex');
    assert.equal(registry.nameFor('ch_1', 'spk_0', 'Alex'), 'Alex');
    assert.equal(registry.nameFor('ch_0', 'spk_0', 'Meeting audio'), 'Meeting audio');
});

test('speaker registry numbers extra voices uniquely across both channels', () => {
    const registry = new SpeakerNameRegistry();
    registry.nameFor('ch_1', 'spk_0', 'Alex');
    registry.nameFor('ch_0', 'spk_0', 'Meeting audio');

    const secondOnMic = registry.nameFor('ch_1', 'spk_1', 'Alex');
    const secondOnTab = registry.nameFor('ch_0', 'spk_1', 'Meeting audio');
    const thirdOnTab = registry.nameFor('ch_0', 'spk_2', 'Meeting audio');

    assert.equal(secondOnMic, 'Speaker 1 (mic)');
    assert.equal(secondOnTab, 'Speaker 2 (tab)');
    assert.equal(thirdOnTab, 'Speaker 3 (tab)');
    // Stable once assigned.
    assert.equal(registry.nameFor('ch_0', 'spk_1', 'Meeting audio'), 'Speaker 2 (tab)');
});

test('speaker registry falls back to the channel name when diarization is off', () => {
    const registry = new SpeakerNameRegistry();
    assert.equal(registry.nameFor('ch_0', null, 'Meeting audio'), 'Meeting audio');
    assert.equal(registry.nameFor('ch_1', undefined, 'Alex'), 'Alex');
});

test('speaker registry tracks the current channel name, not the one first seen', () => {
    const registry = new SpeakerNameRegistry();
    assert.equal(registry.nameFor('ch_0', 'spk_0', 'Unknown'), 'Unknown');
    assert.equal(registry.nameFor('ch_0', 'spk_0', 'Jordan'), 'Jordan');
});
