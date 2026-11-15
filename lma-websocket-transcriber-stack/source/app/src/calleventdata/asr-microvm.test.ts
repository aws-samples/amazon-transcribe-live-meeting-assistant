/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

// Timings must be set before the module under test reads them at import time.
process.env['ASR_RETRY_BACKOFF_MS'] = '20';
process.env['ASR_MIN_BACKOFF_MS'] = '20';
process.env['ASR_READY_TIMEOUT_MS'] = '5000';
process.env['ASR_FINISH_TIMEOUT_MS'] = '500';
// Five seconds of 16 kHz mono PCM, so a test can overflow the backlog cheaply.
process.env['ASR_MAX_PENDING_BYTES'] = String(16000 * 2 * 5);
process.env['AWS_REGION'] = process.env['AWS_REGION'] || 'us-east-1';
// A non-empty ARN so invokeLauncher actually calls the (patched) Lambda client.
process.env['ASR_LAUNCHER_FUNCTION_ARN'] = 'arn:aws:lambda:us-east-1:000000000000:function:fake';

import assert from 'node:assert/strict';
import test from 'node:test';
import { AddressInfo } from 'node:net';
import { FastifyInstance } from 'fastify';
import WebSocket, { WebSocketServer } from 'ws';

import { KinesisClient } from '@aws-sdk/client-kinesis';
import { LambdaClient } from '@aws-sdk/client-lambda';
import BlockStream from 'block-stream2';

import {
    AsrChannelSession,
    coalesceBacklog,
    finishWaitMs,
    pushAsrAudio,
    resolveMaxSpeakers,
    startMicrovmAsr,
    stopMicrovmAsr,
    trimHeldFallbackAudio,
} from './asr-microvm';
import { sessionSettingsFrom } from './session-settings';
import { SpeakerNameRegistry } from './asr-audio';
import { pageSpeakersFor } from './speaker-fusion';
import { TranscriptSegmentRecord } from './transcribe';
import { CallMetaData, SocketCallData } from './eventtypes';
import { microvmRowLanguageCode } from '../config/configuration-store';

const fakeServer = {
    log: {
        info: () => undefined,
        warn: () => undefined,
        error: () => undefined,
        debug: () => undefined,
    },
} as unknown as FastifyInstance;

const makeSocketData = (overrides: Partial<CallMetaData> = {}): SocketCallData => ({
    callMetadata: {
        callId: 'test-call',
        callEvent: 'START',
        activeSpeaker: 'Meeting audio',
        agentId: 'Alex',
        samplingRate: 16000,
        channels: {},
        ...overrides,
    },
    startStreamTime: new Date(),
    speakerEvents: [],
    ended: false,
});

interface FakeAsr {
    url: string;
    audio: Buffer[];
    connections: number;
    close: () => Promise<void>;
}

const startFakeAsr = async (
    onConnection: (socket: WebSocket, connectionIndex: number) => void,
    answerEos = true
): Promise<FakeAsr> => {
    const wss = new WebSocketServer({ port: 0, host: '127.0.0.1' });
    const state: FakeAsr = {
        url: '',
        audio: [],
        connections: 0,
        close: () => new Promise<void>((resolve) => wss.close(() => resolve())),
    };
    wss.on('connection', (socket: WebSocket) => {
        const index = state.connections;
        state.connections += 1;
        socket.on('message', (data: WebSocket.RawData, isBinary: boolean) => {
            if (isBinary) {
                state.audio.push(Buffer.from(data as Buffer));
                return;
            }
            // Every session ends with eos; answering it here keeps the
            // per-test handlers focused on the transcript they emit.
            if (answerEos && (JSON.parse(data.toString()) as { type: string }).type === 'eos') {
                send(socket, { type: 'termination', audio_seconds: 1, segments: 1 });
            }
        });
        onConnection(socket, index);
    });
    await new Promise<void>((resolve) => wss.once('listening', () => resolve()));
    state.url = `ws://127.0.0.1:${(wss.address() as AddressInfo).port}`;
    return state;
};

const send = (socket: WebSocket, message: Record<string, unknown>): void => {
    socket.send(JSON.stringify(message));
};

const ready = (diarize: boolean): Record<string, unknown> => ({
    type: 'ready',
    session_id: 'fake',
    effective_config: { type: 'config', sample_rate: 16000, diarize },
});

const waitFor = async (predicate: () => boolean, timeoutMs = 5000): Promise<void> => {
    const deadline = Date.now() + timeoutMs;
    while (Date.now() < deadline) {
        if (predicate()) {
            return;
        }
        await new Promise((resolve) => setTimeout(resolve, 10));
    }
    throw new Error('timed out waiting for condition');
};

const newSession = (
    asr: FakeAsr,
    rows: TranscriptSegmentRecord[],
    channelId: 'ch_0' | 'ch_1' = 'ch_1',
    socketData: SocketCallData = makeSocketData(),
    diarize = true
): AsrChannelSession =>
    new AsrChannelSession(
        fakeServer,
        socketData,
        channelId,
        { endpointUrl: asr.url },
        new SpeakerNameRegistry(),
        { diarize, maxSpeakers: 0, runTag: 'r1' },
        async (segment) => {
            rows.push(segment);
        }
    );

test('a partial and its final land on one transcript row', async () => {
    const asr = await startFakeAsr((socket) => {
        socket.on('message', (data: WebSocket.RawData, isBinary: boolean) => {
            if (isBinary) {
                return;
            }
            if ((JSON.parse(data.toString()) as { type: string }).type !== 'config') {
                return;
            }
            send(socket, ready(true));
            send(socket, { type: 'partial', segment: 0, text: 'hello', start: 0.5, speaker: 'spk_0' });
            send(socket, {
                type: 'final',
                segment: 0,
                text: 'Hello there.',
                start: 0.5,
                end: 2.25,
                speaker: 'spk_0',
            });
        });
    });

    const rows: TranscriptSegmentRecord[] = [];
    const session = newSession(asr, rows);
    assert.equal(await session.start(), true);
    await waitFor(() => rows.length === 2);
    await session.finish();
    await asr.close();

    assert.equal(rows[0].SegmentId, rows[1].SegmentId);
    assert.equal(rows[0].SegmentId, 'ch_1-r1-g0-s0');
    assert.equal(rows[0].IsPartial, true);
    assert.equal(rows[0].Transcript, 'hello');
    assert.equal(rows[1].IsPartial, false);
    assert.equal(rows[1].Transcript, 'Hello there.');
    assert.equal(rows[1].Channel, 'LOCAL');
    assert.equal(rows[1].StartTime, 0.5);
    assert.equal(rows[1].EndTime, 2.25);
    // The channel's name plus the engine's voice id.
    assert.equal(rows[1].Speaker, 'Alex (spk_0)');
});

test('rows sit after the audio recorded before the session was created', async () => {
    const asr = await startFakeAsr((socket) => {
        socket.on('message', (data: WebSocket.RawData, isBinary: boolean) => {
            if (isBinary || (JSON.parse(data.toString()) as { type: string }).type !== 'config') {
                return;
            }
            send(socket, ready(false));
            send(socket, { type: 'final', segment: 0, text: 'Hello.', start: 0.5, end: 1.5 });
        });
    });

    // Two seconds of 16 kHz stereo 16-bit audio already written to the recording.
    const socketData = { ...makeSocketData(), recordingFileSize: 2 * 16000 * 2 * 2 };
    const rows: TranscriptSegmentRecord[] = [];
    const session = newSession(asr, rows, 'ch_1', socketData, false);
    assert.equal(await session.start(), true);
    await waitFor(() => rows.length === 1);
    await session.finish();
    await asr.close();

    assert.equal(rows[0].StartTime, 2.5);
    assert.equal(rows[0].EndTime, 3.5);
});

test('a second voice on a channel gets its own suffix', async () => {
    const asr = await startFakeAsr((socket) => {
        socket.on('message', (data: WebSocket.RawData, isBinary: boolean) => {
            if (isBinary) {
                return;
            }
            if ((JSON.parse(data.toString()) as { type: string }).type !== 'config') {
                return;
            }
            send(socket, ready(true));
            send(socket, { type: 'final', segment: 0, text: 'one', start: 0, end: 1, speaker: 'spk_0' });
            send(socket, { type: 'final', segment: 1, text: 'two', start: 1, end: 2, speaker: 'spk_1' });
            send(socket, { type: 'final', segment: 2, text: 'three', start: 2, end: 3, speaker: 'spk_0' });
        });
    });

    const rows: TranscriptSegmentRecord[] = [];
    const session = newSession(asr, rows, 'ch_0');
    await session.start();
    await waitFor(() => rows.length === 3);
    await session.finish();
    await asr.close();

    // The suffix is what separates the two voices. Without it the first would take
    // the channel's placeholder name and read as a leftover bucket next to the second.
    assert.deepEqual(
        rows.map((row) => row.Speaker),
        ['Meeting audio (spk_0)', 'Meeting audio (spk_1)', 'Meeting audio (spk_0)']
    );
    assert.deepEqual(
        rows.map((row) => row.SegmentId),
        ['ch_0-r1-g0-s0', 'ch_0-r1-g0-s1', 'ch_0-r1-g0-s2']
    );
    assert.equal(rows[0].Channel, 'REMOTE');
});

test('a reconnect keeps segment ids unique and continues from the audio sent', async () => {
    const asr = await startFakeAsr((socket, index) => {
        socket.on('message', (data: WebSocket.RawData, isBinary: boolean) => {
            if (isBinary) {
                return;
            }
            if ((JSON.parse(data.toString()) as { type: string }).type !== 'config') {
                return;
            }
            send(socket, ready(true));
            if (index === 0) {
                // The engine's own clock; it restarts from 0 on the next connect.
                // Its last word ends at 3 s, a second before the audio does.
                send(socket, { type: 'final', segment: 0, text: 'before', start: 1, end: 3, speaker: 'spk_0' });
                // terminate(), not close(): an abrupt drop is what a MicroVM
                // session loss actually looks like to the transcriber.
                setTimeout(() => socket.terminate(), 50);
            } else {
                send(socket, { type: 'final', segment: 0, text: 'after', start: 0.5, end: 1.5, speaker: 'spk_0' });
            }
        });
    });

    const rows: TranscriptSegmentRecord[] = [];
    const session = newSession(asr, rows);
    // Four seconds of audio before the first connection opens: its backlog.
    for (let i = 0; i < 40; i += 1) {
        session.pushPcm(Buffer.alloc(3200, 1));
    }
    await session.start();
    await waitFor(() => rows.length === 2);
    await session.finish();
    await asr.close();

    assert.equal(asr.connections, 2);
    assert.equal(rows[0].SegmentId, 'ch_1-r1-g0-s0');
    assert.equal(rows[1].SegmentId, 'ch_1-r1-g1-s0');
    // After the four seconds the first connection was sent, where the
    // recording has it; the last word's end (3 s) would put it a second early.
    assert.equal(rows[1].StartTime, 4.5);
    assert.equal(rows[1].EndTime, 5.5);
});

test('audio dropped from a full backlog moves the clock on, so later rows stay on the recording', async () => {
    const asr = await startFakeAsr((socket) => {
        socket.on('message', (data: WebSocket.RawData, isBinary: boolean) => {
            if (isBinary) {
                return;
            }
            if ((JSON.parse(data.toString()) as { type: string }).type === 'config') {
                send(socket, ready(false));
                send(socket, { type: 'final', segment: 0, text: 'kept', start: 0.5, end: 1 });
            }
        });
    });

    const rows: TranscriptSegmentRecord[] = [];
    const session = newSession(asr, rows);
    // Six seconds before the connection opens, against a five-second backlog:
    // the oldest second goes, and the engine hears the last five.
    for (let i = 0; i < 60; i += 1) {
        session.pushPcm(Buffer.alloc(3200, i));
    }
    await session.start();
    await waitFor(() => rows.length === 1);
    await session.finish();
    await asr.close();

    const heard = Buffer.concat(asr.audio);
    assert.equal(heard.length, 16000 * 2 * 5);
    assert.equal(heard[0], 10, 'the backlog keeps the newest audio');
    assert.equal(rows[0].StartTime, 1.5);
    assert.equal(rows[0].EndTime, 2);
});

test('audio pushed before the session connects is buffered, not dropped', async () => {
    const asr = await startFakeAsr((socket) => {
        socket.on('message', (data: WebSocket.RawData, isBinary: boolean) => {
            if (isBinary) {
                return;
            }
            if ((JSON.parse(data.toString()) as { type: string }).type === 'config') {
                send(socket, ready(false));
            }
        });
    });

    const rows: TranscriptSegmentRecord[] = [];
    const session = newSession(asr, rows);
    // 100 ms of 16 kHz mono 16-bit PCM per frame, pushed in worklet-sized pieces
    // to exercise the coalescing path.
    const piece = Buffer.alloc(320, 7);
    for (let i = 0; i < 10; i += 1) {
        session.pushPcm(piece);
    }
    await session.start();
    await waitFor(() => asr.audio.length === 1);
    assert.equal(asr.audio[0].length, 3200, 'a full frame is sent as one WebSocket frame');

    // A partial frame is held back until there is 100 ms of it...
    session.pushPcm(piece);
    session.pushPcm(piece);
    assert.equal(asr.audio.length, 1);

    // ...and flushed on finish, so the tail of the meeting is not lost.
    await session.finish();
    await asr.close();

    assert.equal(Buffer.concat(asr.audio).length, piece.length * 12);
});

test('a diarization request the image cannot serve degrades to channel labels', async () => {
    const asr = await startFakeAsr((socket) => {
        socket.on('message', (data: WebSocket.RawData, isBinary: boolean) => {
            if (isBinary) {
                return;
            }
            if ((JSON.parse(data.toString()) as { type: string }).type !== 'config') {
                return;
            }
            // The engine downgrades diarize when no speaker model was baked in.
            send(socket, ready(false));
            send(socket, { type: 'final', segment: 0, text: 'unlabelled', start: 0, end: 1, speaker: null });
        });
    });

    const rows: TranscriptSegmentRecord[] = [];
    const session = newSession(asr, rows);
    await session.start();
    await waitFor(() => rows.length === 1);
    await session.finish();
    await asr.close();

    assert.equal(session.speakerLabelsActive, false);
    assert.equal(rows[0].Speaker, 'Alex');
});

test('an empty transcript never becomes a transcript row', async () => {
    const asr = await startFakeAsr((socket) => {
        socket.on('message', (data: WebSocket.RawData, isBinary: boolean) => {
            if (isBinary) {
                return;
            }
            if ((JSON.parse(data.toString()) as { type: string }).type !== 'config') {
                return;
            }
            send(socket, ready(true));
            send(socket, { type: 'final', segment: 0, text: '   ', start: 0, end: 1, speaker: 'spk_0' });
            send(socket, { type: 'final', segment: 1, text: 'real', start: 1, end: 2, speaker: 'spk_0' });
        });
    });

    const rows: TranscriptSegmentRecord[] = [];
    const session = newSession(asr, rows);
    await session.start();
    await waitFor(() => rows.length === 1);
    await session.finish();
    await asr.close();

    assert.equal(rows.length, 1);
    assert.equal(rows[0].Transcript, 'real');
});

test('start reports failure when the engine rejects the session', async () => {
    const asr = await startFakeAsr((socket) => {
        socket.on('message', (data: WebSocket.RawData, isBinary: boolean) => {
            if (isBinary) {
                return;
            }
            if ((JSON.parse(data.toString()) as { type: string }).type !== 'config') {
                return;
            }
            send(socket, {
                type: 'error',
                code: 'BAD_CONFIG',
                message: 'sample_rate 16000 is not supported by this image',
                fatal: true,
            });
        });
    });

    const rows: TranscriptSegmentRecord[] = [];
    const session = newSession(asr, rows);
    assert.equal(await session.start(), false);
    await session.finish();
    await asr.close();
    assert.equal(rows.length, 0);
});

test('a buffered backlog is flushed as a few large frames, losing nothing', () => {
    // 60 seconds of 100 ms frames — what a slow MicroVM launch would accumulate.
    const pending = Array.from({ length: 600 }, (_, i) => Buffer.alloc(3200, i % 251));
    const frames = coalesceBacklog(pending);

    // The engine's ingest queue holds 64 entries and drops beyond that, so the
    // flush must be far fewer entries than the backlog.
    assert.ok(frames.length <= 64, `expected <= 64 frames, got ${frames.length}`);
    assert.equal(
        Buffer.concat(frames).length,
        pending.reduce((total, chunk) => total + chunk.length, 0)
    );
    assert.ok(Buffer.concat(frames).equals(Buffer.concat(pending)));
});

test('an empty backlog flushes nothing', () => {
    assert.deepEqual(coalesceBacklog([]), []);
});

test('a backlog smaller than one frame is sent uncopied', () => {
    const only = Buffer.alloc(3200, 4);
    assert.deepEqual(coalesceBacklog([only]), [only]);
});

test('a blank speaker count leaves the deployment cap in force', () => {
    // 0 is what a blank field in the UI sends, i.e. "no opinion". Treating it as an
    // explicit "unbounded" meant every Stream Audio meeting silently overrode a cap
    // an admin had set, so the cap never applied to the client that most needed it.
    assert.equal(resolveMaxSpeakers(0, 2), 2);
    assert.equal(resolveMaxSpeakers(undefined, 2), 2);
});

test('a real speaker count from the client wins over the deployment cap', () => {
    // Only the person in the meeting knows how many people share their microphone.
    assert.equal(resolveMaxSpeakers(4, 2), 4);
    assert.equal(resolveMaxSpeakers(1, 0), 1);
});

test('no cap anywhere still means discover as many speakers as appear', () => {
    assert.equal(resolveMaxSpeakers(undefined, 0), 0);
    assert.equal(resolveMaxSpeakers(0, 0), 0);
});

test('a call that ends during MicroVM acquisition releases the acquired MicroVM', async () => {
    // The acquire takes seconds (a MicroVM boot); the client can hang up in that
    // window. stopMicrovmAsr then has nothing to release (the pending lease has
    // no microvmId yet) - the bug was that the resolving acquire wrote through
    // the cleared socketData.asr (TypeError) and the fresh MicroVM ran unreleased
    // until its max-duration backstop.
    const invocations: Array<Record<string, unknown>> = [];
    let resolveAcquire: ((value: unknown) => void) | undefined;
    const originalSend = LambdaClient.prototype.send;
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    (LambdaClient.prototype as any).send = (command: any) => {
        const payload = JSON.parse(Buffer.from(command.input.Payload).toString('utf8'));
        invocations.push(payload);
        if (payload.action === 'acquire') {
            return new Promise((resolve) => {
                resolveAcquire = resolve;
            });
        }
        return Promise.resolve({
            Payload: Buffer.from(JSON.stringify({ ok: 'true' })),
        });
    };
    try {
        const socketData = makeSocketData();
        const startPromise = startMicrovmAsr(socketData, fakeServer);
        while (!resolveAcquire) {
            await new Promise((resolve) => setTimeout(resolve, 5));
        }

        await stopMicrovmAsr(socketData, fakeServer);
        assert.equal(socketData.asr, undefined);

        resolveAcquire({
            Payload: Buffer.from(
                JSON.stringify({
                    ok: 'true',
                    microvmId: 'm-race',
                    endpoint: 'example.com/session',
                    authToken: 'token',
                })
            ),
        });

        const started = await startPromise;
        assert.equal(started, false);
        const release = invocations.find((p) => p['action'] === 'release');
        assert.ok(release, 'the freshly acquired MicroVM must be released');
        assert.equal(release['microvmId'], 'm-race');
        assert.equal(socketData.asr, undefined);
    } finally {
        LambdaClient.prototype.send = originalSend;
    }
});

test('a call that ends while its MicroVM is acquired has the audio buffered meanwhile transcribed', async (t) => {
    // Two seconds of stereo audio per channel's engine session.
    const perChannelBytes = 16000 * 2 * 2;
    const asr = await startFakeAsr((socket) => {
        let heard = 0;
        socket.on('message', (data: WebSocket.RawData, isBinary: boolean) => {
            if (!isBinary) {
                if ((JSON.parse(data.toString()) as { type: string }).type === 'config') {
                    send(socket, ready(false));
                }
                return;
            }
            heard += (data as Buffer).length;
            if (heard === perChannelBytes) {
                send(socket, { type: 'final', segment: 0, text: 'said before the engine was up', start: 0.5, end: 1.5 });
            }
        });
    });
    const rows: Array<{ Transcript: string; StartTime: number; Channel: string }> = [];
    t.mock.method(KinesisClient.prototype, 'send', async (command: { input: { Data: Buffer } }) => {
        const record = JSON.parse(command.input.Data.toString()) as { EventType: string };
        if (record.EventType === 'ADD_TRANSCRIPT_SEGMENT') {
            rows.push(record as unknown as { Transcript: string; StartTime: number; Channel: string });
        }
        return {};
    });

    const socketData = makeSocketData();
    socketData.sessionSettings = sessionSettingsFrom({}, {});
    let acquired: (lease: { endpointUrl: string }) => void = () => undefined;
    const starting = startMicrovmAsr(
        socketData,
        fakeServer,
        () =>
            new Promise((resolve) => {
                acquired = resolve;
            })
    );
    // The client streams for two seconds and hangs up, all before the MicroVM is up.
    for (let i = 0; i < 20; i += 1) {
        pushAsrAudio(socketData, Buffer.alloc(6400, 1), fakeServer);
    }
    socketData.ended = true;
    acquired({ endpointUrl: asr.url });

    assert.equal(await starting, true, 'no fallback: the MicroVM transcribed it');
    await asr.close();
    assert.equal(Buffer.concat(asr.audio).length, perChannelBytes * 2, 'every buffered byte reached the engine');
    assert.equal(rows.length, 2, 'one row per channel, written before startMicrovmAsr returned');
    assert.equal(rows[0].StartTime, 0.5, 'times run from the start of the call');
    assert.equal(socketData.asr, undefined, 'the sessions were finished and the MicroVM released at once');
});

test('finishing a session that opened with a backlog waits for the engine to work through it', async () => {
    // The engine answers eos only after a second, twice the usual grace in this
    // file (ASR_FINISH_TIMEOUT_MS=500), as it would while decoding a backlog.
    const asr = await startFakeAsr((socket) => {
        socket.on('message', (data: WebSocket.RawData, isBinary: boolean) => {
            if (isBinary) {
                return;
            }
            const type = (JSON.parse(data.toString()) as { type: string }).type;
            if (type === 'config') {
                send(socket, ready(false));
            } else if (type === 'eos') {
                setTimeout(() => {
                    send(socket, { type: 'final', segment: 0, text: 'the tail', start: 2, end: 3 });
                    send(socket, { type: 'termination', audio_seconds: 3, segments: 1 });
                }, 1000);
            }
        });
    }, false);
    const rows: TranscriptSegmentRecord[] = [];
    const session = newSession(asr, rows);
    // Three seconds buffered before the connection opens.
    for (let i = 0; i < 30; i += 1) {
        session.pushPcm(Buffer.alloc(3200, 1));
    }
    await session.start();
    await session.finish();
    await asr.close();
    assert.deepEqual(rows.map((row) => row.Transcript), ['the tail']);
});

test('a stream that ends mid-utterance has that utterance\'s final written before finish returns', async () => {
    // The engine has sent only partials for the utterance in progress when the
    // stream ends. On eos it closes the utterance, and its final takes longer
    // than the usual grace (500 ms in this file) to arrive, as it did on the
    // integration stack for a 20-second stream.
    const asr = await startFakeAsr((socket) => {
        socket.on('message', (data: WebSocket.RawData, isBinary: boolean) => {
            if (isBinary) {
                return;
            }
            const type = (JSON.parse(data.toString()) as { type: string }).type;
            if (type === 'config') {
                send(socket, ready(false));
                send(socket, { type: 'partial', segment: 0, text: 'I bumped into', start: 0.5, end: 2 });
            } else if (type === 'eos') {
                setTimeout(() => {
                    send(socket, { type: 'final', segment: 0, text: 'I bumped into the wall.', start: 0.5, end: 3 });
                    send(socket, { type: 'termination', audio_seconds: 3, segments: 1 });
                }, 1500);
            }
        });
    }, false);
    const rows: TranscriptSegmentRecord[] = [];
    const session = newSession(asr, rows);
    assert.equal(await session.start(), true);
    // Three seconds of live audio, none of it finalised.
    for (let i = 0; i < 30; i += 1) {
        session.pushPcm(Buffer.alloc(3200, 1));
    }
    await waitFor(() => rows.length === 1);
    await session.finish();
    await asr.close();
    assert.deepEqual(
        rows.map((row) => [row.Transcript, row.IsPartial]),
        [
            ['I bumped into', true],
            ['I bumped into the wall.', false],
        ],
        'the final lands on the partial\'s row before finish() resolves'
    );
    assert.equal(rows[0].SegmentId, rows[1].SegmentId);
});

test('finishing stops waiting when the engine closes the connection', async () => {
    // An engine that goes away after eos sends nothing more; waiting out the
    // whole bound would only delay the meeting's END event.
    const asr = await startFakeAsr((socket) => {
        socket.on('message', (data: WebSocket.RawData, isBinary: boolean) => {
            if (isBinary) {
                return;
            }
            const type = (JSON.parse(data.toString()) as { type: string }).type;
            if (type === 'config') {
                send(socket, ready(false));
            } else if (type === 'eos') {
                socket.close();
            }
        });
    }, false);
    const rows: TranscriptSegmentRecord[] = [];
    const session = newSession(asr, rows);
    assert.equal(await session.start(), true);
    // Ten seconds unfinalised: a wait of several seconds were the close ignored.
    for (let i = 0; i < 100; i += 1) {
        session.pushPcm(Buffer.alloc(3200, 1));
    }
    const started = Date.now();
    await session.finish();
    await asr.close();
    assert.ok(Date.now() - started < 1000, `finish took ${Date.now() - started} ms`);
});

test('the finish wait grows with the audio not yet finalised, within its bounds', () => {
    assert.equal(finishWaitMs(0, 5000, 2000, 60000), 5000, 'nothing open: the usual grace');
    assert.equal(finishWaitMs(17, 5000, 2000, 60000), 39000, 'a 17-second utterance still open');
    assert.equal(finishWaitMs(3600, 5000, 2000, 60000), 60000, 'never past the ceiling');
    assert.equal(finishWaitMs(-1, 5000, 2000, 60000), 5000);
    assert.equal(finishWaitMs(10, 5000, 2000, 1000), 5000, 'a ceiling below the grace leaves the grace');
});

test('the stream held for a fallback keeps only the newest audio once past its bound', async () => {
    const warnings: string[] = [];
    const server = {
        log: { ...fakeServer.log, warn: (message: string) => warnings.push(message) },
    } as unknown as FastifyInstance;
    const socketData = makeSocketData();
    // The meeting stream as the transcriber builds it: 100 ms blocks of 16 kHz stereo.
    const source = new BlockStream({ size: 6400 });
    socketData.audioInputStream = source as unknown as SocketCallData['audioInputStream'];
    const bytesPerSecond = 16000 * 2 * 2;
    let written = 0;
    // Ten seconds against a two-second bound.
    for (let i = 0; i < 100; i += 1) {
        source.write(Buffer.alloc(6400, i));
        written += 6400;
        await new Promise((resolve) => setImmediate(resolve));
        trimHeldFallbackAudio(socketData, server, 2);
    }
    const held = source.readableLength + source.writableLength;
    const dropped = socketData.heldAudioDroppedBytes ?? 0;
    assert.equal(held + dropped, written, 'every byte is either held or counted as dropped');
    assert.ok(held <= bytesPerSecond * 2, `held ${held} bytes against a two-second bound`);
    assert.equal(dropped % 4, 0, 'whole stereo frames are dropped');
    assert.equal(warnings.length, 1, 'said once, not per frame');
    const first = source.read(6400) as Buffer;
    assert.equal(first[0], 100 - held / 6400, 'what is kept is the newest audio');
});

test('meeting-audio rows are named from the page speakers the client reported, and by a proven voice', async () => {
    // Ana speaks for 30 s as spk_0, Sam for 25 s as spk_1, then Ana replies
    // for 1.5 s while the page still shows Sam, as Zoom's speaker view does.
    const finals = [
        ...[0, 6, 12, 18, 24].map((t) => ({ start: t, end: t + 5, speaker: 'spk_0' })),
        { start: 30, end: 55, speaker: 'spk_1' },
        { start: 55.3, end: 56.8, speaker: 'spk_0' },
    ];
    const asr = await startFakeAsr((socket) => {
        socket.on('message', (data: WebSocket.RawData, isBinary: boolean) => {
            if (isBinary || (JSON.parse(data.toString()) as { type: string }).type !== 'config') {
                return;
            }
            send(socket, ready(true));
            finals.forEach((row, segment) => send(socket, { type: 'final', segment, text: 'words', ...row }));
        });
    });

    // The client's active speaker is whoever the page shows NOW; the rows are
    // written after it has moved on.
    const socketData = makeSocketData({ activeSpeaker: 'Sam Lee' });
    const page = pageSpeakersFor(socketData.callMetadata);
    page.record(0, 'Ana Ruiz');
    page.record(30, 'Sam Lee');
    const rows: TranscriptSegmentRecord[] = [];
    const session = newSession(asr, rows, 'ch_0', socketData);
    await session.start();
    await waitFor(() => rows.length === finals.length);
    await session.finish();
    await asr.close();

    assert.deepEqual(
        rows.map((row) => row.Speaker),
        [
            ...Array(5).fill('Ana Ruiz (spk_0)'),
            'Sam Lee (spk_1)',
            'Ana Ruiz (spk_0)',
        ]
    );
});

test('a final carries the language the engine named, as a locale, and its partial carries none', async () => {
    const asr = await startFakeAsr((socket) => {
        socket.on('message', (data: WebSocket.RawData, isBinary: boolean) => {
            if (isBinary || (JSON.parse(data.toString()) as { type: string }).type !== 'config') {
                return;
            }
            send(socket, ready(false));
            send(socket, { type: 'partial', segment: 0, text: 'hola a', start: 0.5 });
            send(socket, { type: 'final', segment: 0, text: 'Hola a todos.', start: 0.5, end: 1.5, language: 'es' });
            send(socket, {
                type: 'final',
                segment: 1,
                text: 'Thanks everyone for joining.',
                start: 2,
                end: 3,
                language: 'en',
            });
            send(socket, { type: 'final', segment: 2, text: 'Okay.', start: 3.5, end: 4, language: null });
        });
    });
    const rows: TranscriptSegmentRecord[] = [];
    const session = new AsrChannelSession(
        fakeServer,
        makeSocketData(),
        'ch_1',
        { endpointUrl: asr.url },
        new SpeakerNameRegistry(),
        { diarize: false, maxSpeakers: 0, runTag: 'r1', meetingLanguageCode: 'en-GB' },
        async (segment) => {
            rows.push(segment);
        }
    );
    assert.equal(await session.start(), true);
    await waitFor(() => rows.length === 4);
    await session.finish();
    await asr.close();

    assert.equal('LanguageCode' in rows[0], false, 'a partial names no language');
    assert.equal(rows[1].LanguageCode, 'es-US', 'another language gets its default region');
    assert.equal(rows[2].LanguageCode, 'en-GB', 'a row in the meeting language keeps the meeting region');
    assert.equal('LanguageCode' in rows[3], false, 'a row the engine named no language for has none');
});

test('microvmRowLanguageCode turns a base language into a locale', () => {
    assert.equal(microvmRowLanguageCode('es', 'es-ES'), 'es-ES');
    assert.equal(microvmRowLanguageCode('es', 'en-US'), 'es-US');
    assert.equal(microvmRowLanguageCode('pt', 'en-US'), 'pt-BR');
    assert.equal(microvmRowLanguageCode('de', ''), 'de-DE');
    assert.equal(microvmRowLanguageCode('en', ''), 'en-US');
    assert.equal(microvmRowLanguageCode('en', 'en-AU'), 'en-AU');
    // A language with no entry in the region table is kept as its base code.
    assert.equal(microvmRowLanguageCode('ga', 'en-US'), 'ga');
    // A meeting set to identify its language has no region to lend.
    assert.equal(microvmRowLanguageCode('fr', 'identify-language'), 'fr-FR');
    for (const none of [undefined, null, '', ' ', 'not a code']) {
        assert.equal(microvmRowLanguageCode(none, 'en-US'), undefined);
    }
});
