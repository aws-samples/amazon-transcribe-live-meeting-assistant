/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * How a meeting's session moves between connections and ends
 * (session-lifecycle.ts), with fake sockets and recorded effects in place of
 * the END event, the recording upload and the engine. What every path must
 * leave behind: exactly one END event and one recording for the meeting, and
 * no connection still mapped to it.
 */
import assert from 'node:assert/strict';
import stream from 'node:stream';
import test from 'node:test';

import { CallMetaData, SocketCallData } from './eventtypes';
import {
    DEFAULT_AUDIO_RECONNECT_GRACE_MS,
    SessionLifecycle,
    SessionLifecycleDeps,
    parseReconnectGraceMs,
    recordedAudioSeconds,
} from './session-lifecycle';

class FakeSocket {
    readonly closes: Array<{ code?: number, reason?: string }> = [];
    constructor(readonly name: string) {}
    close(code?: number, reason?: string): void {
        this.closes.push({ code, reason });
    }
}

interface Effects {
    ends: string[];
    recordings: string[];
    engineStops: string[];
    transcriptionStarts: string[];
    errors: string[];
}

const lifecycle = (
    overrides: Partial<SessionLifecycleDeps> = {}
): { sessions: SessionLifecycle<FakeSocket>, effects: Effects, shutDown: () => void } => {
    const effects: Effects = { ends: [], recordings: [], engineStops: [], transcriptionStarts: [], errors: [] };
    let shuttingDown = false;
    const deps: SessionLifecycleDeps = {
        graceMs: 60_000,
        log: {
            info: () => undefined,
            warn: () => undefined,
            debug: () => undefined,
            error: (message) => effects.errors.push(message),
        },
        isShuttingDown: () => shuttingDown,
        writeCallEndEvent: async (meta) => {
            effects.ends.push(meta.callId);
        },
        finishRecording: async (session) => {
            effects.recordings.push(session.callMetadata.callId);
        },
        stopEngine: async (session) => {
            effects.engineStops.push(session.callMetadata.callId);
        },
        startTranscription: async (session) => {
            effects.transcriptionStarts.push(session.callMetadata.callId);
        },
        noteSessionEnded: () => undefined,
        describe: (meta) => meta.callId,
        describeError: (error) => String(error),
        ...overrides,
    };
    return { sessions: new SessionLifecycle<FakeSocket>(deps), effects, shutDown: () => (shuttingDown = true) };
};

const session = (callId = 'meeting-1', ownerSub = 'user-a'): SocketCallData => ({
    callMetadata: {
        callId,
        callEvent: 'START',
        samplingRate: 16000,
        accessToken: 'old-access',
        idToken: 'old-id',
        refreshToken: 'old-refresh',
    } as CallMetaData,
    audioInputStream: new stream.PassThrough(),
    recordingFileSize: 0,
    startStreamTime: new Date(),
    speakerEvents: [],
    ended: false,
    ownerSub,
    transcriptionStarted: true,
});

const newTokens = { accessToken: 'new-access', idToken: 'new-id', refreshToken: 'new-refresh' };

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

test('close, hold, START on a new connection, END: one meeting, one END, nothing left mapped', async () => {
    const { sessions, effects } = lifecycle();
    const first = new FakeSocket('first');
    const second = new FakeSocket('second');
    const meeting = session();
    sessions.register(first, meeting);

    await sessions.onConnectionClosed(first);
    assert.equal(sessions.heldCount, 1, 'the meeting is held for its client');
    assert.equal(sessions.sessionOf(first), undefined);
    assert.deepEqual(effects.ends, [], 'a close without END does not end the meeting');
    assert.equal(sessions.findLive('meeting-1'), meeting, 'a held meeting is still live for its video stream');

    const resumed = await sessions.resume(second, 'meeting-1', 'user-a', newTokens);
    assert.equal(resumed?.session, meeting, 'the same session continues');
    assert.ok((resumed?.detachedForMs ?? -1) >= 0);
    assert.equal(sessions.sessionOf(second), meeting);
    assert.equal(sessions.heldCount, 0);
    assert.equal(meeting.callMetadata.accessToken, 'new-access', 'later events carry the new connection\'s tokens');
    assert.deepEqual(effects.transcriptionStarts, [], 'the running engine is kept, not started again');

    await sessions.end(second, meeting);
    assert.deepEqual(effects.ends, ['meeting-1']);
    assert.deepEqual(effects.recordings, ['meeting-1']);
    assert.deepEqual(effects.engineStops, ['meeting-1']);
    assert.equal(meeting.audioInputStream?.destroyed, true);
    assert.deepEqual(sessions.connectedSessions(), []);

    // The second connection closing after its END changes nothing.
    await sessions.onConnectionClosed(second);
    assert.deepEqual(effects.ends, ['meeting-1']);
    assert.equal(sessions.heldCount, 0);
});

test('an END that arrives on the old connection after a resume still clears the new one', async () => {
    const { sessions, effects } = lifecycle();
    const first = new FakeSocket('first');
    const second = new FakeSocket('second');
    const meeting = session();
    sessions.register(first, meeting);
    await sessions.onConnectionClosed(first);
    await sessions.resume(second, 'meeting-1', 'user-a', newTokens);

    await sessions.end(undefined, meeting);

    assert.deepEqual(effects.ends, ['meeting-1']);
    assert.equal(sessions.sessionOf(second), undefined, 'the session\'s current connection is cleared');
});

test('close and no reconnect: the meeting ends once when the window closes', async () => {
    const { sessions, effects } = lifecycle({ graceMs: 20 });
    const first = new FakeSocket('first');
    const meeting = session();
    sessions.register(first, meeting);

    await sessions.onConnectionClosed(first);
    assert.deepEqual(effects.ends, []);
    await sleep(80);

    assert.deepEqual(effects.ends, ['meeting-1']);
    assert.deepEqual(effects.recordings, ['meeting-1']);
    assert.equal(meeting.ended, true);
    assert.equal(sessions.heldCount, 0);
    assert.deepEqual(sessions.connectedSessions(), []);
    assert.equal(
        await sessions.resume(new FakeSocket('late'), 'meeting-1', 'user-a', newTokens),
        undefined,
        'an ended meeting is not resumed'
    );
});

test('a START while the old connection still looks open takes the meeting over', async () => {
    const { sessions, effects } = lifecycle();
    const first = new FakeSocket('first');
    const second = new FakeSocket('second');
    const meeting = session();
    sessions.register(first, meeting);

    const resumed = await sessions.resume(second, 'meeting-1', 'user-a', newTokens);

    assert.equal(resumed?.session, meeting);
    assert.equal(resumed?.detachedForMs, 0);
    assert.deepEqual(first.closes, [{ code: 1000, reason: 'the meeting continues on a newer connection' }]);
    assert.equal(sessions.sessionOf(first), undefined);
    assert.equal(sessions.sessionOf(second), meeting);

    // The old connection's close event, when it comes, neither ends nor holds the meeting.
    await sessions.onConnectionClosed(first);
    assert.deepEqual(effects.ends, []);
    assert.equal(sessions.heldCount, 0);

    await sessions.end(second, meeting);
    assert.deepEqual(effects.ends, ['meeting-1']);
    assert.deepEqual(sessions.connectedSessions(), []);
});

test('another user\'s START neither resumes nor takes over the meeting', async () => {
    const { sessions } = lifecycle();
    const first = new FakeSocket('first');
    const meeting = session();
    sessions.register(first, meeting);

    assert.equal(await sessions.resume(new FakeSocket('other'), 'meeting-1', 'user-b', newTokens), undefined);
    assert.equal(await sessions.resume(new FakeSocket('anonymous'), 'meeting-1', undefined, newTokens), undefined);
    assert.equal(sessions.sessionOf(first), meeting);
    assert.deepEqual(first.closes, []);

    await sessions.onConnectionClosed(first);
    assert.equal(await sessions.resume(new FakeSocket('other'), 'meeting-1', 'user-b', newTokens), undefined);
    assert.equal(sessions.heldCount, 1, 'still held for its owner');
    await sessions.endHeldSessions();
});

test('a resumed meeting that had not started transcribing starts once, after the held frames', async () => {
    const order: string[] = [];
    const { sessions, effects } = lifecycle({
        startTranscription: async (s) => {
            order.push('start');
            effects.transcriptionStarts.push(s.callMetadata.callId);
        },
    });
    const first = new FakeSocket('first');
    const meeting = session();
    meeting.transcriptionStarted = false;
    sessions.register(first, meeting);
    await sessions.onConnectionClosed(first);

    await sessions.resume(new FakeSocket('second'), 'meeting-1', 'user-a', newTokens, async () => {
        order.push('frames');
    });

    assert.deepEqual(order, ['frames', 'start']);
    assert.equal(meeting.transcriptionStarted, true);
    await sessions.endHeldSessions();
});

test('shutdown ends held meetings at once and does not hold closing ones', async () => {
    const { sessions, effects, shutDown } = lifecycle();
    const held = session('held-meeting');
    const open = session('open-meeting');
    const heldSocket = new FakeSocket('held');
    const openSocket = new FakeSocket('open');
    sessions.register(heldSocket, held);
    sessions.register(openSocket, open);
    await sessions.onConnectionClosed(heldSocket);

    shutDown();
    await sessions.endHeldSessions();
    await sessions.onConnectionClosed(openSocket);

    assert.deepEqual(effects.ends.sort(), ['held-meeting', 'open-meeting']);
    assert.equal(sessions.heldCount, 0);
    assert.deepEqual(sessions.connectedSessions(), []);
});

test('a second END for a meeting writes nothing more', async () => {
    const { sessions, effects } = lifecycle();
    const ws = new FakeSocket('ws');
    const meeting = session();
    sessions.register(ws, meeting);

    await sessions.end(ws, meeting);
    await sessions.end(ws, meeting);

    assert.deepEqual(effects.ends, ['meeting-1']);
    assert.equal(effects.errors.length, 1, 'the duplicate is logged');
});

test('a failed END event still releases the engine and the connection', async () => {
    const { sessions, effects } = lifecycle({
        writeCallEndEvent: async () => {
            throw new Error('stream unavailable');
        },
    });
    const ws = new FakeSocket('ws');
    const meeting = session();
    sessions.register(ws, meeting);

    await assert.rejects(sessions.end(ws, meeting), /stream unavailable/);

    assert.deepEqual(effects.engineStops, ['meeting-1']);
    assert.equal(sessions.sessionOf(ws), undefined);
    assert.equal(meeting.ended, true);
});

test('a discarded session is neither held nor mapped', async () => {
    const { sessions, effects } = lifecycle({ graceMs: 20 });
    const ws = new FakeSocket('ws');
    const meeting = session();
    sessions.register(ws, meeting);
    await sessions.onConnectionClosed(ws);

    sessions.discard(ws, meeting);
    await sleep(60);

    assert.equal(sessions.heldCount, 0);
    assert.deepEqual(effects.ends, [], 'a discarded meeting gets no END');
});

test('the reconnect window falls back to the default unless it is a finite number', () => {
    assert.equal(parseReconnectGraceMs(undefined), DEFAULT_AUDIO_RECONNECT_GRACE_MS);
    assert.equal(parseReconnectGraceMs(''), DEFAULT_AUDIO_RECONNECT_GRACE_MS);
    assert.equal(parseReconnectGraceMs('sixty seconds'), DEFAULT_AUDIO_RECONNECT_GRACE_MS);
    assert.equal(parseReconnectGraceMs('60s'), DEFAULT_AUDIO_RECONNECT_GRACE_MS);
    assert.equal(parseReconnectGraceMs('Infinity'), DEFAULT_AUDIO_RECONNECT_GRACE_MS);
    assert.equal(parseReconnectGraceMs('30000'), 30_000);
    assert.equal(parseReconnectGraceMs(' 15000 '), 15_000);
    assert.equal(parseReconnectGraceMs('0'), 0);
});

test('with a non-numeric window a closed connection is held, not ended at once', async () => {
    const { sessions, effects } = lifecycle({ graceMs: parseReconnectGraceMs('not-a-number') });
    const ws = new FakeSocket('ws');
    sessions.register(ws, session());

    await sessions.onConnectionClosed(ws);
    await sleep(30);

    assert.deepEqual(effects.ends, []);
    assert.equal(sessions.heldCount, 1);
    await sessions.endHeldSessions();
});

test('the timeline position is the duration of the audio received', () => {
    const meeting = session();
    // 16 kHz, two channels, 16-bit: 64,000 bytes a second.
    meeting.recordingFileSize = 64_000 * 90;
    assert.equal(recordedAudioSeconds(meeting), 90);
    meeting.recordingFileSize = undefined;
    assert.equal(recordedAudioSeconds(meeting), 0);
});
