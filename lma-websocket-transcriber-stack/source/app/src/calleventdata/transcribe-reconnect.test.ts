/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Where a meeting's transcript continues after an Amazon Transcribe request is
 * replaced (the four-hour limit, a server-side close, a transient error).
 * Transcribe's item times restart at 0 on every request, so the next request's
 * offset has to be the audio the earlier ones took: that is what the audio
 * recording holds, and the meeting page's player maps transcript time straight
 * onto the recording.
 */
process.env['AWS_REGION'] = process.env['AWS_REGION'] || 'us-east-1';

import assert from 'node:assert/strict';
import stream from 'node:stream';
import test from 'node:test';
import { FastifyInstance } from 'fastify';

import { KinesisClient } from '@aws-sdk/client-kinesis';
import { TranscribeStreamingClient } from '@aws-sdk/client-transcribe-streaming';

import { nextRequestTimeOffset, openAudioTap, startTranscribe, waitForAudio } from './transcribe';
import { SocketCallData } from './eventtypes';
import { sessionSettingsFrom } from './session-settings';

const fakeServer = {
    log: {
        info: () => undefined,
        warn: () => undefined,
        error: () => undefined,
        debug: () => undefined,
    },
} as unknown as FastifyInstance;

// 16 kHz, two channels, 16-bit: the stream every meeting sends Transcribe.
const BYTES_PER_SECOND = 16000 * 2 * 2;

const readBytes = async (iterator: AsyncIterator<unknown>, wanted: number): Promise<number> => {
    let taken = 0;
    while (taken < wanted) {
        const next = await iterator.next();
        if (next.done) {
            break;
        }
        const chunk = (next.value as { AudioEvent: { AudioChunk: Buffer } }).AudioEvent.AudioChunk;
        taken += chunk.length;
    }
    return taken;
};

test('the next request starts where the audio taken so far ends', () => {
    assert.equal(nextRequestTimeOffset(0, BYTES_PER_SECOND * 10, 16000), 10);
    assert.equal(nextRequestTimeOffset(10, BYTES_PER_SECOND / 2, 16000), 10.5);
    assert.equal(nextRequestTimeOffset(7, 0, 16000), 7);
});

test('a closed tap leaves later audio in the source for the next one', async () => {
    const source = new stream.PassThrough();
    const first = openAudioTap(source);
    source.write(Buffer.alloc(1000, 1));
    await new Promise((resolve) => setImmediate(resolve));
    assert.equal(first.close(), 1000);
    // Idempotent: the generator's finally and the reconnect both close it.
    assert.equal(first.close(), 1000);

    // Written between two requests: held, not lost and not counted twice.
    source.write(Buffer.alloc(500, 2));
    await new Promise((resolve) => setImmediate(resolve));

    const second = openAudioTap(source);
    const received: Buffer[] = [];
    second.sink.on('data', (chunk: Buffer) => received.push(chunk));
    await new Promise((resolve) => setImmediate(resolve));
    assert.equal(Buffer.concat(received).length, 500);
    assert.equal(second.close(), 500);
});

test('after a reconnect the transcript continues from the audio sent, not from the last word', async (t) => {
    const source = new stream.PassThrough();
    const socketData: SocketCallData = {
        callMetadata: {
            callId: 'reconnect-call',
            callEvent: 'START',
            activeSpeaker: 'Meeting audio',
            agentId: 'Alex',
            samplingRate: 16000,
            channels: {},
            // The client-reported speaker path: no diarization.
            diarizeSystemChannel: false,
            diarizeMicChannel: false,
        },
        audioInputStream: source,
        startStreamTime: new Date(),
        speakerEvents: [],
        ended: false,
        sessionSettings: sessionSettingsFrom({}, {}),
    };

    // Ten seconds of audio before the first request ends, five before the
    // second does. The first request's only word ends at 4 s: six seconds of
    // silence follow it, which the recording holds.
    source.write(Buffer.alloc(BYTES_PER_SECOND * 10, 1));

    const requests: number[] = [];
    t.mock.method(TranscribeStreamingClient.prototype, 'send', async (command: { input: Record<string, unknown> }) => {
        const index = requests.length;
        const iterator = (command.input['AudioStream'] as AsyncIterable<unknown>)[Symbol.asyncIterator]();
        const taken = await readBytes(iterator, index === 0 ? BYTES_PER_SECOND * 10 : BYTES_PER_SECOND * 5);
        requests.push(taken);
        if (index === 1) {
            socketData.ended = true;
        }
        const word = index === 0 ? { start: 3, end: 4, text: 'before' } : { start: 1, end: 2, text: 'after' };
        async function* results() {
            yield {
                TranscriptEvent: {
                    Transcript: {
                        Results: [
                            {
                                ResultId: `result-${index}`,
                                ChannelId: 'ch_0',
                                IsPartial: false,
                                StartTime: word.start,
                                EndTime: word.end,
                                Alternatives: [
                                    {
                                        Transcript: word.text,
                                        Items: [
                                            {
                                                Type: 'pronunciation',
                                                Content: word.text,
                                                StartTime: word.start,
                                                EndTime: word.end,
                                            },
                                        ],
                                    },
                                ],
                            },
                        ],
                    },
                },
            };
        }
        return { SessionId: 'session-1', TranscriptResultStream: results() };
    });

    const segments: Array<{ Transcript: string; StartTime: number; EndTime: number }> = [];
    t.mock.method(KinesisClient.prototype, 'send', async (command: { input: { Data: Buffer } }) => {
        const record = JSON.parse(command.input.Data.toString()) as { EventType: string };
        if (record.EventType === 'ADD_TRANSCRIPT_SEGMENT') {
            segments.push(record as unknown as { Transcript: string; StartTime: number; EndTime: number });
        }
        return {};
    });

    const done = startTranscribe(socketData, fakeServer);
    // The second request's audio, written while the first one is still open.
    setTimeout(() => source.write(Buffer.alloc(BYTES_PER_SECOND * 5, 2)), 50);
    await done;

    assert.deepEqual(requests, [BYTES_PER_SECOND * 10, BYTES_PER_SECOND * 5]);
    assert.equal(segments.length, 2);
    assert.equal(segments[0].Transcript, 'before');
    assert.equal(segments[0].StartTime, 3);
    assert.equal(segments[1].Transcript, 'after');
    // 10 s of audio before it plus its own 1 s, where it sits in the recording.
    // The last segment's end would have put it at 5 s.
    assert.equal(segments[1].StartTime, 11);
    assert.equal(segments[1].EndTime, 12);
});

test('waiting for audio resolves once audio arrives, and not at all for a meeting that ends', async () => {
    const sink = new stream.PassThrough();
    let ended = false;
    const waiting = waitForAudio(sink, () => ended, 5);
    setTimeout(() => sink.write(Buffer.alloc(4, 1)), 20);
    assert.equal(await waiting, true);

    const idle = new stream.PassThrough();
    const endedWaiting = waitForAudio(idle, () => ended, 5);
    setTimeout(() => (ended = true), 20);
    assert.equal(await endedWaiting, false);
});

test('no Transcribe request is opened while no audio arrives, and none once the meeting ends', async (t) => {
    const source = new stream.PassThrough();
    const socketData: SocketCallData = {
        callMetadata: {
            callId: 'held-call',
            callEvent: 'START',
            activeSpeaker: 'Meeting audio',
            agentId: 'Alex',
            samplingRate: 16000,
            channels: {},
            diarizeSystemChannel: false,
            diarizeMicChannel: false,
        },
        audioInputStream: source,
        startStreamTime: new Date(),
        speakerEvents: [],
        ended: false,
        sessionSettings: sessionSettingsFrom({}, {}),
    };
    source.write(Buffer.alloc(BYTES_PER_SECOND, 1));

    let requests = 0;
    t.mock.method(TranscribeStreamingClient.prototype, 'send', async (command: { input: Record<string, unknown> }) => {
        requests += 1;
        const iterator = (command.input['AudioStream'] as AsyncIterable<unknown>)[Symbol.asyncIterator]();
        await readBytes(iterator, BYTES_PER_SECOND);
        // Transcribe closes the request: the client's connection has gone quiet.
        async function* results() {}
        return { SessionId: 'session-1', TranscriptResultStream: results() };
    });

    const done = startTranscribe(socketData, fakeServer);
    // Past the reconnect backoff, with no audio sent since the first request.
    await new Promise((resolve) => setTimeout(resolve, 1200));
    assert.equal(requests, 1, 'the next request waits for audio');
    socketData.ended = true;
    await done;
    assert.equal(requests, 1);
});
