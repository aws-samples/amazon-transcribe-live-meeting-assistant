/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

// Timings must be set before the module under test reads them at import time.
process.env['ASR_RETRY_BACKOFF_MS'] = '20';
process.env['ASR_MIN_BACKOFF_MS'] = '20';
process.env['ASR_READY_TIMEOUT_MS'] = '5000';
process.env['ASR_FINISH_TIMEOUT_MS'] = '500';
process.env['AWS_REGION'] = process.env['AWS_REGION'] || 'us-east-1';

import assert from 'node:assert/strict';
import test from 'node:test';
import { AddressInfo } from 'node:net';
import { FastifyInstance } from 'fastify';
import WebSocket, { WebSocketServer } from 'ws';

import {
    AsrChannelSession,
    coalesceBacklog,
    resolveMaxSpeakers,
} from './asr-microvm';
import { SpeakerNameRegistry } from './asr-audio';
import { TranscriptSegmentRecord } from './transcribe';
import { CallMetaData, SocketCallData } from './eventtypes';

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
    onConnection: (socket: WebSocket, connectionIndex: number) => void
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
            if ((JSON.parse(data.toString()) as { type: string }).type === 'eos') {
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
        { diarize, maxSpeakers: 0, speakerThreshold: 0.5, endpointingMs: 1200 },
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
    assert.equal(rows[0].SegmentId, 'ch_1-g0-s0');
    assert.equal(rows[0].IsPartial, true);
    assert.equal(rows[0].Transcript, 'hello');
    assert.equal(rows[1].IsPartial, false);
    assert.equal(rows[1].Transcript, 'Hello there.');
    assert.equal(rows[1].Channel, 'AGENT');
    assert.equal(rows[1].StartTime, 0.5);
    assert.equal(rows[1].EndTime, 2.25);
    // The channel's name plus the engine's voice id.
    assert.equal(rows[1].Speaker, 'Alex (spk_0)');
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
        ['ch_0-g0-s0', 'ch_0-g0-s1', 'ch_0-g0-s2']
    );
    assert.equal(rows[0].Channel, 'CALLER');
});

test('a reconnect keeps segment ids unique and the timeline monotonic', async () => {
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
    await session.start();
    await waitFor(() => rows.length === 2);
    await session.finish();
    await asr.close();

    assert.equal(asr.connections, 2);
    assert.equal(rows[0].SegmentId, 'ch_1-g0-s0');
    assert.equal(rows[1].SegmentId, 'ch_1-g1-s0');
    assert.equal(rows[1].StartTime, 3.5);
    assert.equal(rows[1].EndTime, 4.5);
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
