/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
process.env['AWS_REGION'] = process.env['AWS_REGION'] || 'us-east-1';
process.env['ASR_CALIBRATION_TIMEOUT_MS'] = '5000';

import assert from 'node:assert/strict';
import test from 'node:test';
import { FastifyInstance } from 'fastify';
import WebSocket, { WebSocketServer } from 'ws';

import {
    CalibrationDeps,
    CalibrationError,
    embedSegments,
    readChannels,
    resolveRecordingKey,
    runCalibration,
} from './asr-calibrate';
import { CalibrationSegment, parseWavHeader, selectSpread } from './asr-calibration';
import { createWavHeader } from '../utils/wav';

const fakeServer = {
    log: {
        info: () => undefined,
        warn: () => undefined,
        error: () => undefined,
        debug: () => undefined,
    },
} as unknown as FastifyInstance;

interface Turn {
    channel: 0 | 1;
    from: number;
    to: number;
}

/**
 * Interleaved stereo PCM of a conversation: a 440 Hz tone on whichever channel is
 * speaking, room noise on the other. A tone rather than a square wave because the
 * downsampler low-passes at 7.2 kHz, and an alternating-sample fixture would be
 * filtered away to nothing before the segmenter ever saw it.
 */
const conversation = (sampleRate: number, totalSec: number, turns: Turn[]): Buffer => {
    const frames = Math.floor(sampleRate * totalSec);
    const pcm = Buffer.alloc(frames * 4);
    for (let frame = 0; frame < frames; frame += 1) {
        const at = frame / sampleRate;
        const tone = Math.sin(2 * Math.PI * 440 * at);
        for (const channel of [0, 1] as const) {
            const speaking = turns.some(
                (turn) => turn.channel === channel && at >= turn.from && at < turn.to
            );
            const amplitude = speaking ? 6000 : 30;
            pcm.writeInt16LE(Math.round(tone * amplitude), frame * 4 + channel * 2);
        }
    }
    return pcm;
};

const wav = (sampleRate: number, pcm: Buffer): Buffer =>
    Buffer.concat([createWavHeader(sampleRate, pcm.length), pcm]);

const streamOf = (buffer: Buffer, chunkBytes = 64 * 1024): AsyncIterable<Uint8Array> => ({
    async *[Symbol.asyncIterator]() {
        for (let offset = 0; offset < buffer.length; offset += chunkBytes) {
            yield buffer.subarray(offset, Math.min(offset + chunkBytes, buffer.length));
        }
    },
});

/** Four turns each, alternating, with mutual silence between them. */
const TWO_SIDED_TURNS: Turn[] = [
    { channel: 0, from: 0, to: 1.6 },
    { channel: 1, from: 2.1, to: 3.7 },
    { channel: 0, from: 4.2, to: 5.8 },
    { channel: 1, from: 6.3, to: 7.9 },
    { channel: 0, from: 8.4, to: 10.0 },
    { channel: 1, from: 10.5, to: 12.1 },
    { channel: 0, from: 12.6, to: 14.2 },
    { channel: 1, from: 14.7, to: 16.3 },
];

test('a canonical WAV header is read', () => {
    const format = parseWavHeader(createWavHeader(48000, 1024));

    assert.equal(format?.sampleRate, 48000);
    assert.equal(format?.channels, 2);
    assert.equal(format?.bitsPerSample, 16);
    assert.equal(format?.dataOffset, 44);
    assert.equal(format?.dataLength, 1024);
});

test('chunks between fmt and data are skipped rather than mistaken for audio', () => {
    // A writer may put LIST/INFO before the audio; assuming the 44-byte layout
    // would read that metadata as samples and swap the two channels.
    const canonical = createWavHeader(16000, 200);
    const list = Buffer.alloc(8 + 10);
    list.write('LIST', 0, 'ascii');
    list.writeUInt32LE(10, 4);
    const withList = Buffer.concat([
        canonical.subarray(0, 36),
        list,
        canonical.subarray(36),
        Buffer.alloc(200),
    ]);

    const format = parseWavHeader(withList);

    assert.equal(format?.dataOffset, 44 + list.length);
    assert.equal(format?.sampleRate, 16000);
});

test('an incomplete header asks for more bytes instead of guessing', () => {
    assert.equal(parseWavHeader(createWavHeader(16000, 10).subarray(0, 20)), undefined);
    assert.equal(parseWavHeader(Buffer.alloc(4)), undefined);
});

test('a non-PCM recording is refused with a reason', () => {
    const header = createWavHeader(16000, 10);
    header.writeUInt16LE(7, 20);

    assert.throws(() => parseWavHeader(header), /unsupported recording encoding/);
    assert.throws(() => parseWavHeader(Buffer.alloc(64)), /not a RIFF/);
});

test('spread selection keeps the ends of a meeting, not just its start', () => {
    const items = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10];

    assert.deepEqual(selectSpread(items, 3), [1, 6, 10]);
    assert.deepEqual(selectSpread([1, 2], 5), [1, 2]);
    assert.deepEqual(selectSpread(items, 0), []);
});

test('a 48 kHz stereo recording is decoded into two 16 kHz channels', async () => {
    const pcm = conversation(48000, 4, [{ channel: 0, from: 0, to: 2 }]);

    const audio = await readChannels(streamOf(wav(48000, pcm)), 60);

    assert.equal(audio.format.sampleRate, 48000);
    assert.equal(audio.truncated, false);
    assert.ok(Math.abs(audio.secondsRead - 4) < 0.1, `read ${audio.secondsRead}s`);
    assert.ok(Math.abs(audio.channel0.length - audio.channel1.length) <= 4);
    // The tone is on ch_0 for the first half, so that channel must carry more
    // energy — proof the de-interleave survived the chunk boundaries.
    const energy = (buffer: Buffer): number => {
        let total = 0;
        for (let offset = 0; offset + 1 < buffer.length; offset += 2) {
            total += Math.abs(buffer.readInt16LE(offset));
        }
        return total;
    };
    assert.ok(energy(audio.channel0) > energy(audio.channel1) * 10);
});

test('a mono recording is refused with the reason it cannot be used', async () => {
    const header = createWavHeader(16000, 32);
    header.writeUInt16LE(1, 22);

    await assert.rejects(
        readChannels(streamOf(Buffer.concat([header, Buffer.alloc(32)])), 60),
        (error: CalibrationError) =>
            error.status === 400 && /two-channel recording/.test(error.message)
    );
});

test('a long recording is truncated at the time cap and says so', async () => {
    const audio = await readChannels(
        streamOf(wav(16000, conversation(16000, 6, [{ channel: 0, from: 0, to: 6 }]))),
        2
    );

    assert.equal(audio.truncated, true);
    assert.ok(audio.secondsRead < 3, `read ${audio.secondsRead}s`);
});

test('a recording key is derived from the callId and confined to the prefix', () => {
    // Same posixify() the recording writer used, so the key matches the object it
    // actually wrote — any other spelling of it just 404s.
    assert.equal(
        resolveRecordingKey({ callId: 'Stream Audio - 2026-08-12' }),
        'lma-audio-recordings/Stream_Audio___2026_08_12.wav'
    );
    assert.equal(
        resolveRecordingKey({ recordingKey: 'lma-audio-recordings/meeting.wav' }),
        'lma-audio-recordings/meeting.wav'
    );
    assert.throws(() => resolveRecordingKey({}), /callId or a recordingKey/);
    // The task role can read the whole bucket, so an arbitrary key would make this
    // route a file reader for anyone in the Admin group.
    assert.throws(
        () => resolveRecordingKey({ recordingKey: '../../secrets.wav' }),
        /must be an object under/
    );
    assert.throws(
        () => resolveRecordingKey({ recordingKey: 'lma-audio-recordings/../x.wav' }),
        /must be an object under/
    );
    assert.throws(
        () => resolveRecordingKey({ recordingKey: 'lma-audio-recordings/meeting.raw' }),
        /reads the .wav recording/
    );
});

interface FakeEmbedder {
    url: string;
    configs: Array<Record<string, unknown>>;
    frames: number[];
    close: () => Promise<void>;
}

/** A stand-in for the engine's embed mode: one vector per binary frame. */
const startFakeEmbedder = async (
    vectorFor: (index: number) => number[] | 'error' | 'drop'
): Promise<FakeEmbedder> => {
    const wss = new WebSocketServer({ port: 0, host: '127.0.0.1' });
    const state: FakeEmbedder = {
        url: '',
        configs: [],
        frames: [],
        close: () => new Promise<void>((resolve) => wss.close(() => resolve())),
    };
    wss.on('connection', (socket: WebSocket) => {
        let index = 0;
        socket.on('message', (data: WebSocket.RawData, isBinary: boolean) => {
            if (isBinary) {
                state.frames.push((data as Buffer).length);
                const vector = vectorFor(index);
                index += 1;
                if (vector === 'error') {
                    socket.send(
                        JSON.stringify({ type: 'error', code: 'BAD_CONFIG', message: 'no speaker model' })
                    );
                    return;
                }
                if (vector === 'drop') {
                    socket.close();
                    return;
                }
                socket.send(
                    JSON.stringify({ type: 'embedding', index: index - 1, dim: vector.length, vector })
                );
                return;
            }
            const message = JSON.parse(data.toString()) as { type: string };
            if (message.type === 'config') {
                state.configs.push(message as Record<string, unknown>);
                socket.send(JSON.stringify({ type: 'ready', effective_config: { diarize: false } }));
            } else if (message.type === 'eos') {
                socket.send(JSON.stringify({ type: 'termination', audio_seconds: 1, segments: index }));
            }
        });
    });
    await new Promise<void>((resolve) => wss.once('listening', () => resolve()));
    const address = wss.address() as { port: number };
    state.url = `ws://127.0.0.1:${address.port}`;
    return state;
};

const segment = (channel: 'ch_0' | 'ch_1', index: number): CalibrationSegment => ({
    channel,
    startSec: index,
    endSec: index + 3,
    pcm: Buffer.alloc(3 * 16000 * 2),
});

test('embed mode sends one utterance at a time and collects the vectors', async () => {
    const engine = await startFakeEmbedder((index) => [index, 1, 0]);
    const segments = [segment('ch_0', 0), segment('ch_1', 1), segment('ch_0', 2)];

    const vectors = await embedSegments({ endpointUrl: engine.url }, segments, fakeServer);
    await engine.close();

    assert.equal(vectors.length, 3);
    assert.deepEqual(vectors[2], [2, 1, 0]);
    assert.equal(engine.configs[0]['mode'], 'embed');
    assert.equal(engine.configs[0]['sample_rate'], 16000);
    // One frame per utterance: the engine's reply order is what maps a vector back
    // to the channel it came from, so batching frames would lose the labels.
    assert.deepEqual(engine.frames, [96000, 96000, 96000]);
});

test('an engine without a speaker model reports why instead of hanging', async () => {
    const engine = await startFakeEmbedder(() => 'error');

    await assert.rejects(
        embedSegments({ endpointUrl: engine.url }, [segment('ch_0', 0)], fakeServer),
        (error: CalibrationError) => error.status === 503 && /no speaker model/.test(error.message)
    );
    await engine.close();
});

test('vectors already collected survive a dropped connection', async () => {
    const engine = await startFakeEmbedder((index) => (index < 2 ? [index, 1] : 'drop'));
    const segments = [segment('ch_0', 0), segment('ch_1', 1), segment('ch_0', 2)];

    const vectors = await embedSegments({ endpointUrl: engine.url }, segments, fakeServer);
    await engine.close();

    assert.equal(vectors.length, 2);
});

const depsFor = (
    recording: Buffer,
    overrides: Partial<CalibrationDeps> = {}
): CalibrationDeps & { released: string[]; acquired: number } => {
    const state = {
        released: [] as string[],
        acquired: 0,
        fetchRecording: async () => streamOf(recording),
        acquire: async () => {
            state.acquired += 1;
            return { endpointUrl: 'ws://unused', microvmId: 'mv-1' };
        },
        release: async (microvmId: string) => {
            state.released.push(microvmId);
        },
        // Distinct directions per channel, the shape real embeddings have when the
        // speaker model suits the audio.
        embed: async (_lease: unknown, segments: CalibrationSegment[]) =>
            segments.map((item, index) =>
                item.channel === 'ch_0' ? [1, 0.02 * index] : [0.02 * index, 1]
            ),
        ...overrides,
    };
    return state as unknown as CalibrationDeps & { released: string[]; acquired: number };
};

test('a two-sided recording yields an operating point and frees the MicroVM', async () => {
    const deps = depsFor(wav(48000, conversation(48000, 17, TWO_SIDED_TURNS)));

    const run = await runCalibration({ callId: 'meeting-1' }, fakeServer, deps);

    assert.equal(run.recordingKey, 'lma-audio-recordings/meeting_1.wav');
    assert.equal(run.sourceSampleRate, 48000);
    assert.ok(run.segmentsFound >= 6, `found ${run.segmentsFound} segments`);
    assert.equal(run.segmentsEmbedded, run.segmentsFound);
    assert.ok(run.result.speakerThreshold !== undefined);
    assert.equal(run.result.confidence, 'good');
    assert.deepEqual(deps.released, ['mv-1']);
});

test('a one-sided recording is refused before a MicroVM is launched', async () => {
    // Only the microphone side spoke: there is no different-speaker comparison to
    // make, so launching an engine to embed it would cost money for nothing.
    const deps = depsFor(
        wav(48000, conversation(48000, 12, [
            { channel: 1, from: 0, to: 2 },
            { channel: 1, from: 3, to: 5 },
            { channel: 1, from: 6, to: 8 },
            { channel: 1, from: 9, to: 11 },
        ]))
    );

    const run = await runCalibration({ callId: 'one-sided' }, fakeServer, deps);

    assert.equal(deps.acquired, 0);
    assert.equal(run.segmentsEmbedded, 0);
    assert.equal(run.result.confidence, 'unusable');
    assert.ok(run.result.notes.some((note) => note.includes('at least')));
});

test('the MicroVM is released even when embedding fails', async () => {
    const deps = depsFor(wav(48000, conversation(48000, 17, TWO_SIDED_TURNS)), {
        embed: async () => {
            throw new CalibrationError('engine exploded', 502);
        },
    });

    await assert.rejects(runCalibration({ callId: 'meeting-2' }, fakeServer, deps), /engine exploded/);
    assert.deepEqual(deps.released, ['mv-1']);
});

test('a second concurrent run is refused rather than launching another MicroVM', async () => {
    let releaseEmbed: (() => void) | undefined;
    const deps = depsFor(wav(48000, conversation(48000, 17, TWO_SIDED_TURNS)), {
        embed: async (_lease: unknown, segments: CalibrationSegment[]) => {
            await new Promise<void>((resolve) => {
                releaseEmbed = resolve;
            });
            return segments.map(() => [1, 0]);
        },
    });

    const first = runCalibration({ callId: 'meeting-3' }, fakeServer, deps);
    // Let the first run reach the embed step.
    while (!releaseEmbed) {
        await new Promise((resolve) => setTimeout(resolve, 10));
    }

    await assert.rejects(
        runCalibration({ callId: 'meeting-4' }, fakeServer, deps),
        (error: CalibrationError) => error.status === 409
    );

    releaseEmbed();
    await first;
    assert.equal(deps.acquired, 1);
});
