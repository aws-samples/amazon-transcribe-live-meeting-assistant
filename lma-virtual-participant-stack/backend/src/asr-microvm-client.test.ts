/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

// Timings are read at import time.
process.env.ASR_RETRY_BACKOFF_MS = '20';
process.env.ASR_MIN_BACKOFF_MS = '20';
process.env.ASR_MAX_RETRIES = '2';
process.env.ASR_READY_TIMEOUT_MS = '5000';
process.env.ASR_FINISH_TIMEOUT_MS = '500';
process.env.AWS_REGION = process.env.AWS_REGION || 'us-east-1';
delete process.env.ASR_ENGINE;
delete process.env.ASR_LAUNCHER_FUNCTION_ARN;
process.env.ASR_DIRECT_ENDPOINT = 'ws://127.0.0.1:1'; // marks the engine as configured for resolve tests

import assert from 'node:assert/strict';
import test from 'node:test';
import { AddressInfo } from 'node:net';
import WebSocket, { WebSocketServer } from 'ws';
import type { AsrSegment } from './asr-microvm-client.js';

// A static import would be hoisted above the process.env assignments (this
// package is ESM), so the module would read its production defaults - 5 retries
// at 2 s backoff, no endpoint - and the timing and fallback tests below would run
// against those. A dynamic import after the assignments is what makes the env
// actually apply.
const { MicrovmAsrSession, coalesceBacklog, fetchAsrRuntimeSwitches, resolveVpAsrEngine, speakerNameFor } =
    await import('./asr-microvm-client.js');

interface FakeAsr {
    url: string;
    connections: number;
    audioBytes: number;
    close: () => Promise<void>;
}

const startFakeAsr = async (script: (socket: WebSocket, connection: number) => void): Promise<FakeAsr> => {
    const wss = new WebSocketServer({ port: 0 });
    const fake: FakeAsr = {
        url: '',
        connections: 0,
        audioBytes: 0,
        close: () =>
            new Promise<void>((resolve) => {
                for (const client of wss.clients) client.terminate();
                wss.close(() => resolve());
            }),
    };
    wss.on('connection', (socket) => {
        fake.connections += 1;
        socket.on('message', (data: WebSocket.RawData, isBinary: boolean) => {
            if (isBinary) fake.audioBytes += (data as Buffer).length;
        });
        script(socket, fake.connections);
    });
    await new Promise<void>((resolve) => wss.on('listening', () => resolve()));
    fake.url = `ws://127.0.0.1:${(wss.address() as AddressInfo).port}`;
    return fake;
};

const ready = (socket: WebSocket, diarize = true) =>
    socket.on('message', (data: WebSocket.RawData, isBinary: boolean) => {
        if (isBinary) return;
        const message = JSON.parse(data.toString());
        if (message.type === 'config') {
            socket.send(JSON.stringify({ type: 'ready', effective_config: { diarize: diarize && message.diarize } }));
        }
        if (message.type === 'eos') {
            socket.send(JSON.stringify({ type: 'termination', audio_seconds: 1, segments: 1 }));
        }
    });

const newSession = (asr: FakeAsr, rows: AsrSegment[], live = { value: true }, diarize = true) =>
    new MicrovmAsrSession({
        callId: 'vp-test-call',
        lease: { endpointUrl: asr.url },
        diarize,
        onSegment: (segment) => rows.push(segment),
        isMeetingLive: () => live.value,
    });

test('a partial and its final land on one row, and speaker labels follow the engine', async () => {
    const asr = await startFakeAsr((socket) => {
        ready(socket);
        socket.on('message', (data: WebSocket.RawData, isBinary: boolean) => {
            if (!isBinary) return;
            socket.send(JSON.stringify({ type: 'partial', segment: 3, text: 'hello there', start: 1.0, end: 1.8 }));
            socket.send(
                JSON.stringify({ type: 'final', segment: 3, text: 'Hello there.', start: 1.0, end: 2.1, speaker: 'spk_1' }),
            );
        });
    });
    const rows: AsrSegment[] = [];
    const session = newSession(asr, rows);
    try {
        assert.equal(await session.start(), true);
        assert.equal(session.speakerLabelsActive, true);
        session.pushPcm(Buffer.alloc(3200));
        await new Promise((resolve) => setTimeout(resolve, 150));
        await session.finish();

        assert.equal(rows.length, 2);
        assert.equal(rows[0].segmentId, rows[1].segmentId, 'partial and final must share a row');
        assert.equal(rows[0].isPartial, true);
        assert.equal(rows[0].speaker, undefined, 'partials name nobody');
        assert.equal(rows[1].isPartial, false);
        assert.equal(rows[1].speaker, 'spk_1');
        assert.equal(rows[1].text, 'Hello there.');
        assert.ok(asr.audioBytes >= 3200, 'audio reached the engine');
    } finally {
        await asr.close();
    }
});

test('a reconnect keeps segment ids unique and the timeline monotonic', async () => {
    const asr = await startFakeAsr((socket, connection) => {
        ready(socket);
        socket.on('message', (data: WebSocket.RawData, isBinary: boolean) => {
            if (!isBinary) return;
            // Both connections report the engine's own clock restarting at 0.
            socket.send(JSON.stringify({ type: 'final', segment: 1, text: `turn ${connection}`, start: 0.5, end: 4.0 }));
            if (connection === 1) socket.close(1011, 'engine restart');
        });
    });
    const rows: AsrSegment[] = [];
    const session = newSession(asr, rows);
    try {
        assert.equal(await session.start(), true);
        session.pushPcm(Buffer.alloc(3200));
        await new Promise((resolve) => setTimeout(resolve, 400)); // close + backoff + reconnect
        session.pushPcm(Buffer.alloc(3200));
        await new Promise((resolve) => setTimeout(resolve, 200));
        await session.finish();

        assert.equal(asr.connections, 2);
        assert.equal(rows.length, 2);
        assert.notEqual(rows[0].segmentId, rows[1].segmentId);
        assert.ok(rows[1].startSec >= rows[0].endSec, 'second connection continues the meeting timeline');
    } finally {
        await asr.close();
    }
});

test('audio pushed before the session connects is buffered, not dropped', async () => {
    const asr = await startFakeAsr((socket) => ready(socket));
    const session = newSession(asr, []);
    try {
        session.pushPcm(Buffer.alloc(6400));
        assert.equal(await session.start(), true);
        await new Promise((resolve) => setTimeout(resolve, 100));
        assert.ok(asr.audioBytes >= 6400);
        await session.finish();
    } finally {
        await asr.close();
    }
});

test('a diarization request the image cannot serve degrades to roster names only', async () => {
    const asr = await startFakeAsr((socket) => ready(socket, false));
    const session = newSession(asr, []);
    try {
        assert.equal(await session.start(), true);
        assert.equal(session.speakerLabelsActive, false);
        await session.finish();
    } finally {
        await asr.close();
    }
});

test('an engine that keeps dying is given up on after the retry budget', async () => {
    const asr = await startFakeAsr((socket) => {
        ready(socket);
        setTimeout(() => socket.close(1011, 'boom'), 30);
    });
    const session = newSession(asr, []);
    try {
        assert.equal(await session.start(), true);
        await new Promise((resolve) => setTimeout(resolve, 600));
        assert.equal(session.hasGivenUp, true);
        assert.ok(asr.connections >= 3);
        await session.finish();
    } finally {
        await asr.close();
    }
});

test('a closed socket is not reconnected once the meeting is over', async () => {
    const live = { value: true };
    const asr = await startFakeAsr((socket) => {
        ready(socket);
        setTimeout(() => {
            live.value = false;
            socket.close(1011, 'meeting over');
        }, 30);
    });
    const session = newSession(asr, [], live);
    try {
        assert.equal(await session.start(), true);
        await new Promise((resolve) => setTimeout(resolve, 200));
        assert.equal(asr.connections, 1);
        assert.equal(session.hasGivenUp, false);
        await session.finish();
    } finally {
        await asr.close();
    }
});

test('the per-VP override beats the deployment default, and both fall back when unconfigured', () => {
    assert.equal(resolveVpAsrEngine({ engineDefaultMicrovm: false, diarizeVirtualParticipant: false }), 'transcribe');
    assert.equal(resolveVpAsrEngine({ engineDefaultMicrovm: true, diarizeVirtualParticipant: false }), 'microvm');
    assert.equal(
        resolveVpAsrEngine({ engineDefaultMicrovm: true, diarizeVirtualParticipant: false }, 'transcribe'),
        'transcribe',
    );
    assert.equal(
        resolveVpAsrEngine({ engineDefaultMicrovm: false, diarizeVirtualParticipant: false }, 'MicroVM'),
        'microvm',
    );
});

test('the runtime switches are read from getAsrConfig and default off on any failure', async () => {
    const okFetch = (async () =>
        new Response(
            JSON.stringify({ data: { getAsrConfig: { engineDefaultMicrovm: true, diarizeVirtualParticipant: true } } }),
        )) as unknown as typeof fetch;
    assert.deepEqual(await fetchAsrRuntimeSwitches('https://example/graphql', okFetch), {
        engineDefaultMicrovm: true,
        diarizeVirtualParticipant: true,
    });

    const noRecord = (async () => new Response(JSON.stringify({ data: { getAsrConfig: null } }))) as unknown as typeof fetch;
    assert.deepEqual(await fetchAsrRuntimeSwitches('https://example/graphql', noRecord), {
        engineDefaultMicrovm: false,
        diarizeVirtualParticipant: false,
    });

    const failing = (async () => {
        throw new Error('network');
    }) as unknown as typeof fetch;
    assert.deepEqual(await fetchAsrRuntimeSwitches('https://example/graphql', failing), {
        engineDefaultMicrovm: false,
        diarizeVirtualParticipant: false,
    });

    assert.deepEqual(await fetchAsrRuntimeSwitches(''), {
        engineDefaultMicrovm: false,
        diarizeVirtualParticipant: false,
    });
});

test('speaker names are the roster name plus the voice id in the shared label format', () => {
    assert.equal(speakerNameFor('Feldman, Jeremy', 'spk_1'), 'Feldman, Jeremy (spk_1)');
    assert.equal(speakerNameFor('Feldman, Jeremy', '2'), 'Feldman, Jeremy (spk_2)');
    assert.equal(speakerNameFor('Feldman, Jeremy', undefined), 'Feldman, Jeremy');
    assert.equal(speakerNameFor('Feldman, Jeremy', null), 'Feldman, Jeremy');
});

test('a buffered backlog is flushed as a few large frames, losing nothing', () => {
    const frames = coalesceBacklog([Buffer.alloc(1000), Buffer.alloc(1000), Buffer.alloc(500)], 1500);
    assert.deepEqual(
        frames.map((f) => f.length),
        [2000, 500],
    );
    assert.deepEqual(coalesceBacklog([]), []);
});
