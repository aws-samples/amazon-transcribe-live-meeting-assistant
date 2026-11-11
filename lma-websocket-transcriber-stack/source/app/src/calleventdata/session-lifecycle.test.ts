/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
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
    warnings: string[];
    /** Audio written to a session, as `callId:frame`, interleaved with `start:callId` when an engine starts. */
    audio: string[];
}

const lifecycle = (
    overrides: Partial<SessionLifecycleDeps> = {}
): { sessions: SessionLifecycle<FakeSocket>, effects: Effects, shutDown: () => void } => {
    const effects: Effects = {
        ends: [],
        recordings: [],
        engineStops: [],
        transcriptionStarts: [],
        errors: [],
        warnings: [],
        audio: [],
    };
    let shuttingDown = false;
    const deps: SessionLifecycleDeps = {
        graceMs: 60_000,
        log: {
            info: () => undefined,
            warn: (message) => effects.warnings.push(message),
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
            effects.audio.push(`start:${session.callMetadata.callId}`);
        },
        writeAudio: (session, frame) => {
            effects.audio.push(`${session.callMetadata.callId}:${Buffer.from(frame).toString()}`);
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

test('ending a resumed meeting without naming a connection clears the connection it resumed on', async () => {
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

// Audio sent right behind a START, while the START is still being handled.

const frame = (text: string): Uint8Array => Buffer.from(text);

test('audio sent at once behind a START is written to the new session in order once it exists', () => {
    const { sessions, effects } = lifecycle();
    const ws = new FakeSocket('ws');
    sessions.holdAudio(ws, 16000);
    assert.equal(sessions.onAudio(ws, frame('a')), true, 'held, not dropped');
    assert.equal(sessions.onAudio(ws, frame('b')), true);
    // The START registers its session, then finishes its consent frames; audio
    // arriving meanwhile is still held, behind the earlier frames.
    sessions.register(ws, session());
    sessions.onAudio(ws, frame('c'));
    assert.deepEqual(effects.audio, [], 'nothing is written while the START is in progress');

    sessions.releaseAudio(ws);
    assert.deepEqual(effects.audio, ['meeting-1:a', 'meeting-1:b', 'meeting-1:c']);
    assert.equal(sessions.pendingAudioCount, 0);
});

test('audio after the session exists is written after the held audio, not before it', () => {
    const { sessions, effects } = lifecycle();
    const ws = new FakeSocket('ws');
    sessions.holdAudio(ws, 16000);
    sessions.onAudio(ws, frame('held-1'));
    sessions.onAudio(ws, frame('held-2'));
    sessions.register(ws, session());
    sessions.releaseAudio(ws);
    sessions.onAudio(ws, frame('live-1'));
    sessions.onAudio(ws, frame('live-2'));
    assert.deepEqual(effects.audio, ['meeting-1:held-1', 'meeting-1:held-2', 'meeting-1:live-1', 'meeting-1:live-2']);
});

test('a resumed session gets the audio held behind its START before its engine starts', async () => {
    const { sessions, effects } = lifecycle();
    const first = new FakeSocket('first');
    const second = new FakeSocket('second');
    const meeting = session();
    meeting.transcriptionStarted = false;
    sessions.register(first, meeting);
    await sessions.onConnectionClosed(first);

    sessions.holdAudio(second, 16000);
    sessions.onAudio(second, frame('replayed'));
    const resumed = await sessions.resume(second, 'meeting-1', 'user-a', newTokens, async () => {
        // The session is on the new connection now, and audio is still held.
        sessions.onAudio(second, frame('live-during-start'));
        assert.deepEqual(effects.audio, []);
    });
    assert.equal(resumed?.session, meeting);
    sessions.onAudio(second, frame('live-after'));
    assert.deepEqual(effects.audio, [
        'meeting-1:replayed',
        'meeting-1:live-during-start',
        'start:meeting-1',
        'meeting-1:live-after',
    ]);
});

test('audio beyond the hold\'s bound drops the oldest frames and reports how many', () => {
    const { sessions, effects } = lifecycle();
    const ws = new FakeSocket('ws');
    // 8 kHz stereo 16-bit for 8 s: 256,000 bytes.
    sessions.holdAudio(ws, 8000);
    const chunk = Buffer.alloc(1000);
    for (let i = 0; i < 300; i += 1) {
        sessions.onAudio(ws, Buffer.concat([Buffer.from(String(i).padStart(4, '0')), chunk.subarray(4)]));
    }
    sessions.register(ws, session());
    sessions.releaseAudio(ws);
    assert.equal(effects.audio.length, 256);
    assert.ok(effects.audio[0].startsWith('meeting-1:0044'), 'the 44 oldest frames are dropped');
    assert.ok(effects.audio[255].startsWith('meeting-1:0299'), 'the newest frame is kept');
    assert.equal(effects.warnings.length, 1);
    assert.match(effects.warnings[0], /44 audio frame\(s\) \(44000 bytes\)/);
});

test('a refused START discards the audio held for it', () => {
    const { sessions, effects } = lifecycle();
    const ws = new FakeSocket('ws');
    sessions.holdAudio(ws, 16000);
    sessions.onAudio(ws, frame('a'));
    sessions.dropAudio(ws);
    assert.equal(sessions.pendingAudioCount, 0);
    assert.equal(sessions.onAudio(ws, frame('b')), false, 'with no START and no session the frame is dropped');
    // A release that follows a refusal has nothing to write.
    sessions.releaseAudio(ws);
    assert.deepEqual(effects.audio, []);
});

test('a START whose session ended while it was handled discards its held audio', () => {
    const { sessions, effects } = lifecycle();
    const ws = new FakeSocket('ws');
    const meeting = session();
    sessions.holdAudio(ws, 16000);
    sessions.onAudio(ws, frame('a'));
    sessions.register(ws, meeting);
    // The consent check found the meeting is somebody else's.
    meeting.ended = true;
    sessions.discard(ws, meeting);
    sessions.releaseAudio(ws);
    assert.deepEqual(effects.audio, []);
    assert.equal(sessions.pendingAudioCount, 0);
});

test('a connection that closes while its START is in progress releases its held audio', async () => {
    const { sessions, effects } = lifecycle();
    const ws = new FakeSocket('ws');
    sessions.holdAudio(ws, 16000);
    sessions.onAudio(ws, frame('a'));
    await sessions.onConnectionClosed(ws);
    assert.equal(sessions.pendingAudioCount, 0, 'nothing is held for a closed connection');
    // The START finishing afterwards finds nothing to write.
    sessions.register(ws, session());
    sessions.releaseAudio(ws);
    assert.deepEqual(effects.audio, []);
});

test('a second START on a connection holds its audio away from the session it already carries', () => {
    const { sessions, effects } = lifecycle();
    const ws = new FakeSocket('ws');
    sessions.register(ws, session('meeting-1'));
    sessions.onAudio(ws, frame('first'));
    sessions.holdAudio(ws, 16000);
    sessions.onAudio(ws, frame('second'));
    sessions.register(ws, session('meeting-2'));
    sessions.releaseAudio(ws);
    assert.deepEqual(effects.audio, ['meeting-1:first', 'meeting-2:second']);
});

test('a connection that closed before its START registered a session has that session held once the START finishes', async () => {
    const { sessions, effects } = lifecycle();
    const ws = new FakeSocket('closed-during-start');
    // The close arrives while the START is still awaiting: nothing is mapped yet.
    await sessions.onConnectionClosed(ws);
    const meeting = session();
    sessions.register(ws, meeting);

    await sessions.afterStart(ws);

    assert.equal(sessions.sessionOf(ws), undefined, 'the closed connection carries no session');
    assert.equal(sessions.heldCount, 1, 'the meeting is held for its client to reconnect');
    assert.deepEqual(effects.ends, []);
    const resumed = await sessions.resume(new FakeSocket('next'), 'meeting-1', 'user-a', newTokens);
    assert.equal(resumed?.session, meeting);
});

test('with no reconnect window, a session whose connection closed during its START ends when the START finishes', async () => {
    const { sessions, effects } = lifecycle({ graceMs: 0 });
    const ws = new FakeSocket('closed-during-start');
    await sessions.onConnectionClosed(ws);
    sessions.register(ws, session());

    await sessions.afterStart(ws);

    assert.deepEqual(effects.ends, ['meeting-1']);
    assert.deepEqual(effects.engineStops, ['meeting-1']);
    assert.deepEqual(sessions.connectedSessions(), []);
});

test('the end of a START changes nothing for an open connection or one whose close was already handled', async () => {
    const { sessions, effects } = lifecycle();
    const open = new FakeSocket('open');
    const meeting = session();
    sessions.register(open, meeting);
    await sessions.afterStart(open);
    assert.equal(sessions.sessionOf(open), meeting);

    const closedLater = new FakeSocket('closed-later');
    sessions.register(closedLater, session('meeting-2'));
    await sessions.onConnectionClosed(closedLater);
    await sessions.afterStart(closedLater);
    assert.equal(sessions.heldCount, 1, 'held once, by its own close');
    assert.deepEqual(effects.ends, []);
});

test('ending a meeting whose engine is still starting waits for it before the END event', async () => {
    const order: string[] = [];
    const { sessions } = lifecycle({
        writeCallEndEvent: async () => {
            order.push('end-event');
        },
        stopEngine: async () => {
            order.push('engine-stopped');
        },
    });
    const ws = new FakeSocket('a');
    const meeting = session();
    let finishStart: () => void = () => undefined;
    meeting.engineStarting = new Promise<void>((resolve) => {
        finishStart = () => {
            order.push('engine-started');
            resolve();
        };
    });
    sessions.register(ws, meeting);

    const ending = sessions.end(ws, meeting);
    await new Promise((resolve) => setTimeout(resolve, 20));
    assert.equal(meeting.ended, true, 'marked ended at once, so the starting engine finishes rather than streams');
    assert.deepEqual(order, [], 'nothing is written while the engine is starting');
    assert.equal(meeting.audioInputStream?.destroyed, false, 'the audio held for the engine is kept');

    finishStart();
    await ending;
    assert.deepEqual(order, ['engine-started', 'end-event', 'engine-stopped']);
    assert.equal(meeting.audioInputStream?.destroyed, true);
});

test('the wait for a starting engine is bounded, and skipped while the task shuts down', async () => {
    const { sessions, effects } = lifecycle({ engineStartWaitMs: 30 });
    const ws = new FakeSocket('a');
    const meeting = session();
    meeting.engineStarting = new Promise<void>(() => undefined);
    sessions.register(ws, meeting);
    await sessions.end(ws, meeting);
    assert.deepEqual(effects.ends, ['meeting-1']);
    assert.ok(effects.warnings.some((line) => line.includes('had not started')));

    const shutting = lifecycle({ engineStartWaitMs: 60_000 });
    shutting.shutDown();
    const other = session('meeting-2');
    other.engineStarting = new Promise<void>(() => undefined);
    shutting.sessions.register(new FakeSocket('b'), other);
    const started = Date.now();
    await shutting.sessions.end(undefined, other);
    assert.ok(Date.now() - started < 1000, 'shutdown does not wait for an engine to start');
    assert.deepEqual(shutting.effects.ends, ['meeting-2']);
});
