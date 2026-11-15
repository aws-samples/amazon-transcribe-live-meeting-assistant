/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Which engine ends up with the audio a meeting sent while its ASR MicroVM was
 * being acquired (engine-start.ts), with the engines replaced by recorders.
 */
import assert from 'node:assert/strict';
import stream from 'node:stream';
import test from 'node:test';

import { EngineStartDeps, startTranscriptionEngine } from './engine-start';
import { CallMetaData, SocketCallData } from './eventtypes';
import { TranscribeStartOptions } from './transcribe';

const meeting = (): SocketCallData => ({
    callMetadata: { callId: 'meeting-1', callEvent: 'START', samplingRate: 16000 } as CallMetaData,
    audioInputStream: new stream.PassThrough(),
    startStreamTime: new Date(),
    speakerEvents: [],
    ended: false,
});

interface Recorded {
    transcribeCalls: Array<TranscribeStartOptions | undefined>;
    engines: string[];
    logs: string[];
}

const deps = (
    startMicrovm: (socketData: SocketCallData) => Promise<boolean>,
    overrides: Partial<EngineStartDeps> = {}
): { deps: EngineStartDeps; recorded: Recorded } => {
    const recorded: Recorded = { transcribeCalls: [], engines: [], logs: [] };
    return {
        recorded,
        deps: {
            chooseEngine: async () => 'microvm',
            startMicrovm,
            startTranscribe: async (_socketData, options) => {
                recorded.transcribeCalls.push(options);
            },
            fallbackToTranscribe: () => true,
            noteEngine: (_socketData, engine) => recorded.engines.push(engine),
            log: {
                warn: (message) => recorded.logs.push(message),
                error: (message) => recorded.logs.push(message),
            },
            ...overrides,
        },
    };
};

test('a MicroVM that cannot be acquired hands Amazon Transcribe the audio held meanwhile', async () => {
    const data = meeting();
    data.audioInputStream?.write(Buffer.alloc(6400, 1));
    const { deps: d, recorded } = deps(async () => false);
    await startTranscriptionEngine(data, d);
    assert.deepEqual(recorded.engines, ['transcribe']);
    assert.deepEqual(recorded.transcribeCalls, [undefined], 'a live meeting gets the ordinary Transcribe loop');
    assert.equal(data.audioInputStream?.readableLength, 6400, 'the held audio is still in the stream for it');
});

test('a meeting that ends while its MicroVM is acquired, which then fails, has its held audio sent to Amazon Transcribe', async () => {
    const data = meeting();
    let failAcquire: ((started: boolean) => void) | undefined;
    let transcribed: () => void = () => undefined;
    const transcribing = new Promise<void>((resolve) => {
        transcribed = resolve;
    });
    const { deps: d, recorded } = deps(
        () =>
            new Promise<boolean>((resolve) => {
                failAcquire = resolve;
            }),
        {
            startTranscribe: async (_socketData, options) => {
                recorded.transcribeCalls.push(options);
                await transcribing;
            },
        }
    );
    void startTranscriptionEngine(data, d);
    let settled = false;
    void data.engineStarting?.then(() => (settled = true));

    while (!failAcquire) {
        await new Promise((resolve) => setImmediate(resolve));
    }
    data.ended = true;
    failAcquire(false);
    await new Promise((resolve) => setTimeout(resolve, 10));
    assert.deepEqual(recorded.transcribeCalls, [{ heldAudioOnly: true }]);
    assert.equal(settled, false, 'the meeting end waits for that request too');
    transcribed();
    await data.engineStarting;
    assert.equal(settled, true);
});

test('a MicroVM that starts drains the stream, which no other engine reads', async () => {
    const data = meeting();
    data.audioInputStream?.write(Buffer.alloc(6400, 1));
    const { deps: d, recorded } = deps(async () => true);
    await startTranscriptionEngine(data, d);
    await new Promise((resolve) => setImmediate(resolve));
    assert.deepEqual(recorded.engines, ['microvm']);
    assert.deepEqual(recorded.transcribeCalls, []);
    assert.equal(data.audioInputStream?.readableLength, 0);
});

test('with the fallback switched off nothing transcribes a meeting whose MicroVM failed', async () => {
    const data = meeting();
    const { deps: d, recorded } = deps(async () => false, { fallbackToTranscribe: () => false });
    await startTranscriptionEngine(data, d);
    assert.deepEqual(recorded.transcribeCalls, []);
    assert.ok(recorded.logs.some((line) => line.includes('fallback is disabled')));
});
