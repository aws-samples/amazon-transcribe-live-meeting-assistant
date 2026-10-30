/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * A fresh Nova Sonic session for every translated turn.
 *
 * The Nova tests drive processResponseStream with Nova's events in the order a
 * live stream delivers them: the participant's FINAL turn, then the start of
 * the assistant's answer. The Bedrock stream itself is stood in for, so these
 * cover the hand-over (what each session is sent, and when), not the model.
 *
 * What is NOT covered here: whether the model translates better for it. That
 * was measured by replaying synthesized meetings against Nova 2 Sonic (see
 * translator-turns.ts); it has not been verified in a live meeting.
 */
import { strict as assert } from 'node:assert';
import { afterEach, beforeEach, mock, test } from 'node:test';
import { MeetingMode } from './nova-sonic-config-loader.js';
import { NovaAgent } from './nova-agent.js';
import { pcmMs, RecentAudio } from './translator-turns.js';

// ---------------------------------------------------------------------------
// The recent-audio buffer
// ---------------------------------------------------------------------------

test('recent audio returns what was sent since a moment, oldest first', () => {
    const recent = new RecentAudio(3000);
    recent.push(Buffer.from('a'), 1000);
    recent.push(Buffer.from('b'), 2000);
    recent.push(Buffer.from('c'), 2500);
    assert.deepEqual(recent.since(2000).map(String), ['b', 'c']);
    assert.deepEqual(recent.since(9000), []);
});

test('recent audio keeps only the most recent stretch', () => {
    const recent = new RecentAudio(3000);
    recent.push(Buffer.from('old'), 1000);
    recent.push(Buffer.from('new'), 5000);
    assert.deepEqual(recent.since(0).map(String), ['new']);
});

test('pcmMs measures 16 kHz 16-bit mono audio', () => {
    assert.equal(pcmMs([Buffer.alloc(32000), Buffer.alloc(16000)]), 1500);
});

// ---------------------------------------------------------------------------
// Nova Sonic
// ---------------------------------------------------------------------------

let logs: string[] = [];
beforeEach(() => {
    logs = [];
    mock.method(console, 'log', (...args: unknown[]) => { logs.push(args.map(String).join(' ')); });
});
afterEach(() => mock.restoreAll());

function chunk(event: unknown): unknown {
    return { chunk: { bytes: new TextEncoder().encode(JSON.stringify({ event })) } };
}

const FINAL = JSON.stringify({ generationStage: 'FINAL' });
const SPECULATIVE = JSON.stringify({ generationStage: 'SPECULATIVE' });

const userTurn = (text: string) => [
    chunk({ contentStart: { type: 'TEXT', role: 'USER', additionalModelFields: FINAL } }),
    chunk({ textOutput: { content: text } }),
    chunk({ contentEnd: { type: 'TEXT', stopReason: 'END_TURN' } }),
];
const answerStarts = () => [
    chunk({ contentStart: { type: 'TEXT', role: 'ASSISTANT', additionalModelFields: SPECULATIVE } }),
    chunk({ textOutput: { content: 'Hola a todos.' } }),
    chunk({ contentEnd: { type: 'TEXT', stopReason: 'PARTIAL_TURN' } }),
];
const audioBlock = (bytes: number[], stopReason: string) => [
    chunk({ contentStart: { type: 'AUDIO', role: 'ASSISTANT' } }),
    chunk({ audioOutput: { content: Buffer.from(bytes).toString('base64') } }),
    chunk({ contentEnd: { type: 'AUDIO', stopReason } }),
];
const finalAnswer = (text: string) => [
    chunk({ contentStart: { type: 'TEXT', role: 'ASSISTANT', additionalModelFields: FINAL } }),
    chunk({ textOutput: { content: text } }),
    chunk({ contentEnd: { type: 'TEXT', stopReason: 'END_TURN' } }),
];

const flush = () => new Promise((resolve) => setImmediate(resolve));

function session(id: string): any {
    return {
        id, queue: [], isActive: true, promptName: `p-${id}`, audioContentId: `a-${id}`,
        isPromptStartSent: true, isAudioContentStartSent: true,
        sessionStartTime: Date.now(), lastRefreshTime: Date.now(), signal: null,
    };
}

function translator(meetingMode: MeetingMode = 'translator'): { a: any; started: any[]; played: Buffer[] } {
    const agent = new NovaAgent({
        modelId: 'amazon.nova-2-sonic-v1:0',
        systemPrompt: 'You are Alex, an AI meeting assistant.',
        activationMode: 'always_active',
        meetingMode,
        translatorLanguageA: 'English',
        translatorLanguageB: 'Spanish',
    });
    const a = agent as any;
    const started: any[] = [];
    const played: Buffer[] = [];
    a.bedrockClient = {};
    a.initiateBidirectionalStreaming = async (s: any) => { started.push(s); };
    a.startSessionRefreshTimer = () => {};
    a.playAudio = async (b: Buffer) => { played.push(b); };
    a.session = session('first');
    a._isActive = true;
    a.streamingActive = true;
    return { a, started, played };
}

/** Drive one session's response stream, running `between` before each event. */
async function stream(a: any, s: any, events: unknown[], between?: (i: number) => void | Promise<void>): Promise<void> {
    async function* body() {
        for (let i = 0; i < events.length; i++) {
            await between?.(i);
            yield events[i];
        }
    }
    await a.processResponseStream({ body: body() }, s);
    for (let i = 0; i < 3; i++) await flush();
}

const audioInputs = (s: any) => s.queue.filter((e: any) => e.event?.audioInput).map((e: any) => e.event.audioInput);

test('nova: a translator session that starts answering hands the meeting to a new session', async () => {
    const { a, started } = translator();
    const first = a.session;
    await stream(a, first, [...userTurn('hello everyone'), ...answerStarts()]);

    const next = a.session;
    assert.notEqual(next, first, 'the meeting moves on to a new session');
    assert.deepEqual(started, [next], 'and its stream is started');
    assert.equal(first.draining, true, 'the old session is left to finish its answer');
    assert.equal(first.isActive, true);

    // Set up from scratch: no conversation history is replayed into it.
    const kinds = next.queue.map((e: any) => Object.keys(e.event)[0]);
    assert.deepEqual(kinds.slice(0, 2), ['sessionStart', 'promptStart']);
    assert.equal(next.queue.at(-1).event.contentStart?.type, 'AUDIO');
    assert.equal(next.queue.filter((e: any) => e.event.contentStart?.role === 'USER' && e.event.contentStart.type === 'TEXT').length, 0);
    assert.ok(logs.some((l) => /^\[translator-turn\] n=1 from=first to=\S+ backlog_ms=\d+ draining=1$/.test(l)), logs.join('\n'));
});

test('nova: the audio heard since the turn ended is replayed into the new session first', async () => {
    const { a } = translator();
    const first = a.session;
    const before = Buffer.alloc(320, 1);
    const after = Buffer.alloc(320, 2);
    // `before` is sent while the participant is still speaking, `after` once
    // the turn has ended and before the answer starts.
    const pause = () => new Promise((resolve) => setTimeout(resolve, 5));
    await stream(a, first, [...userTurn('hello everyone'), ...answerStarts()], async (i) => {
        if (i === 0) a.sendAudioChunk(before);
        // Live chunks are 32 ms apart; keep them out of the turn end's millisecond.
        if (i === 1) await pause();
        if (i === 3) {
            await pause();
            a.sendAudioChunk(after);
        }
    });
    const next = a.session;
    const replayed = audioInputs(next).map((x: any) => Buffer.from(x.content, 'base64'));
    assert.deepEqual(replayed, [after], 'only what followed the turn is replayed');

    // From now on the new session hears the meeting and the old one silence.
    const live = Buffer.alloc(320, 3);
    a.sendAudioChunk(live);
    assert.deepEqual(Buffer.from(audioInputs(next).at(-1).content, 'base64'), live);
    const toOld = Buffer.from(audioInputs(first).at(-1).content, 'base64');
    assert.equal(toOld.length, live.length);
    assert.ok(toOld.every((b) => b === 0), 'the outgoing session hears silence');
});

test('nova: the outgoing session is closed once the final text of its answer is in', async () => {
    const { a } = translator();
    const first = a.session;
    await stream(a, first, [
        ...userTurn('hello everyone'), ...answerStarts(),
        ...audioBlock([1, 2], 'END_TURN'),
        ...finalAnswer('Hola a todos.'),
    ]);
    const kinds = first.queue.map((e: any) => Object.keys(e.event)[0]);
    assert.deepEqual(kinds.slice(-3), ['contentEnd', 'promptEnd', 'sessionEnd']);
    assert.equal(a.drainingSessions.size, 0);
    assert.ok(logs.some((l) => /^\[translator-turn\] closed first: answered after \d+ ms$/.test(l)));
});

test('nova: the next answer waits until the outgoing one has finished playing out', async () => {
    const { a, played } = translator();
    const first = a.session;
    await stream(a, first, [...userTurn('hello everyone'), ...answerStarts(),
        chunk({ contentStart: { type: 'AUDIO', role: 'ASSISTANT' } }),
        chunk({ audioOutput: { content: Buffer.from([1]).toString('base64') } })]);
    const next = a.session;
    // The new session answers the next turn while the old one is still producing audio.
    await stream(a, next, [chunk({ contentStart: { type: 'AUDIO', role: 'ASSISTANT' } }),
        chunk({ audioOutput: { content: Buffer.from([9]).toString('base64') } })]);
    assert.deepEqual(played.map((b) => [...b]), [[1]], 'held back');

    await stream(a, first, [chunk({ contentEnd: { type: 'AUDIO', stopReason: 'END_TURN' } })]);
    assert.deepEqual(played.map((b) => [...b]), [[1], [9]], 'played after the outgoing answer');
});

test('nova: a session is not replaced outside unpaused translator mode', async () => {
    for (const setup of [
        (a: any) => { a.meetingMode = 'normal'; },
        (a: any) => { a._translatorState = 'MUTED'; },
    ]) {
        const { a, started } = translator();
        setup(a);
        const first = a.session;
        await stream(a, first, [...userTurn('hello everyone'), ...answerStarts()]);
        assert.equal(a.session, first);
        assert.deepEqual(started, []);
    }
});

test('nova: an answer with no participant turn before it does not replace the session', async () => {
    const { a, started } = translator();
    const first = a.session;
    await stream(a, first, answerStarts());
    assert.equal(a.session, first);
    assert.deepEqual(started, []);
});

test('nova: one session hands over once, however many blocks its answer has', async () => {
    const { a, started } = translator();
    const first = a.session;
    await stream(a, first, [...userTurn('hello everyone'), ...answerStarts(), ...answerStarts(),
        ...audioBlock([1], 'PARTIAL_TURN'), ...audioBlock([2], 'END_TURN')]);
    assert.equal(started.length, 1);
});

test('nova: a refresh closes an outgoing session with the one it replaces', async () => {
    const { a } = translator();
    const first = a.session;
    await stream(a, first, [...userTurn('hello everyone'), ...answerStarts()]);
    a.createSession = async () => {};
    await a.refreshSession();
    assert.equal(a.drainingSessions.size, 0);
    assert.equal(first.queue.at(-1).event.sessionEnd !== undefined, true);
    assert.ok(logs.some((l) => /^\[translator-turn\] closed first: refresh/.test(l)));
});

test('nova: a mute called by an outgoing session is not answered on the new one', async () => {
    const { a } = translator();
    a.playConfirmationTone = () => {};
    a.refreshSession = async () => {};
    a.MUTE_TOOL_AUDIO_FLUSH_MS = 0;
    const first = a.session;
    await stream(a, first, [...userTurn('translator mute'), ...answerStarts(),
        chunk({ toolUse: { toolName: 'mute', toolUseId: 'm-1', content: '{}' } }),
        chunk({ contentEnd: { type: 'TOOL' } })]);
    await new Promise((resolve) => setTimeout(resolve, 20));
    const next = a.session;
    assert.notEqual(next, first);
    assert.equal(next.queue.filter((e: any) => e.event?.toolResult).length, 0,
        'the new session never issued that tool use');
    assert.equal(a._translatorState, 'MUTED');
});

test('nova: a stream that ends while the agent is stopping does not open another session', async () => {
    const { a } = translator();
    a.initiateBidirectionalStreaming = Object.getPrototypeOf(a).initiateBidirectionalStreaming;
    a.bedrockClient = { send: async () => ({ body: (async function* () {})() }) };
    let refreshes = 0;
    a.refreshSession = async () => { refreshes++; };
    a.stopped = true;
    await a.initiateBidirectionalStreaming(a.session);
    assert.equal(refreshes, 0);

    // The same stream ending unexpectedly in a running meeting still recovers.
    a.stopped = false;
    await a.initiateBidirectionalStreaming(a.session);
    assert.equal(refreshes, 1);
});
