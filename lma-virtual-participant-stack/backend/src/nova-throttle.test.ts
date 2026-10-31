/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Nova Sonic's concurrent-stream quota: how a refused stream is recognised,
 * and what each thing that opens a stream does when it is refused.
 *
 * The refusal stood in for here has the shape measured against Amazon Bedrock
 * (see nova-throttle.ts): `send()` itself throws a `ThrottlingException` with
 * HTTP status 429. The Bedrock client is a stand-in throughout, so these cover
 * the agent's handling of the refusal, not the service.
 */
import { strict as assert } from 'node:assert';
import { afterEach, beforeEach, mock, test } from 'node:test';
import { isNovaThrottle, throttleBackoffMs } from './nova-throttle.js';
import { MetricEmitter } from './metrics-emf.js';
import { NOVA_SONIC_STREAMS_THROTTLED } from './metrics-catalogue.js';
import { ASSISTANT_OPEN_ATTEMPTS, NovaAgent } from './nova-agent.js';
import { renderToolAckClip } from './tool-ack-clip.js';

function refusal(): Error {
    const error = new Error('Too many connections, please wait before trying again.') as any;
    error.name = 'ThrottlingException';
    error.$metadata = { httpStatusCode: 429 };
    return error;
}

let logs: string[] = [];
let warnings: string[] = [];
beforeEach(() => {
    logs = [];
    warnings = [];
    mock.method(console, 'log', (...args: unknown[]) => { logs.push(args.map(String).join(' ')); });
    mock.method(console, 'warn', (...args: unknown[]) => { warnings.push(args.map(String).join(' ')); });
    mock.method(console, 'error', () => {});
});
afterEach(() => mock.restoreAll());

// ---------------------------------------------------------------------------
// Recognising a refusal, and backing off
// ---------------------------------------------------------------------------

test('the measured refusal is recognised as the quota', () => {
    assert.equal(isNovaThrottle(refusal()), true);
    const byStatus = Object.assign(new Error('x'), { $metadata: { httpStatusCode: 429 } });
    assert.equal(isNovaThrottle(byStatus), true);
});

test('other failures are not taken for the quota', () => {
    const validation = Object.assign(new Error('First content must have SYSTEM role'), { name: 'ValidationException' });
    assert.equal(isNovaThrottle(validation), false);
    assert.equal(isNovaThrottle(new Error('socket hang up')), false);
    assert.equal(isNovaThrottle(null), false);
    assert.equal(isNovaThrottle('ThrottlingException'), false);
});

test('the backoff grows, is capped, and is jittered over the upper half', () => {
    const at = (attempt: number, r: number) => throttleBackoffMs(attempt, { baseMs: 1000, capMs: 8000, random: () => r });
    assert.equal(at(0, 0), 500);
    assert.equal(at(0, 1), 1000);
    assert.equal(at(2, 0), 2000);
    assert.equal(at(2, 1), 4000);
    assert.equal(at(10, 1), 8000, 'capped');
    assert.equal(at(10, 0), 4000);
});

test('a refusal is published as one NovaSonicStreamsThrottled datapoint', () => {
    const lines: string[] = [];
    const emitter = new MetricEmitter('lma-test', { sink: (l) => lines.push(l), clock: () => 1700000000000.4 });
    emitter.count(NOVA_SONIC_STREAMS_THROTTLED);
    assert.equal(lines.length, 1);
    const record = JSON.parse(lines[0]);
    assert.deepEqual(record._aws, {
        Timestamp: 1700000000000,
        CloudWatchMetrics: [{
            Namespace: 'lma-test',
            Dimensions: [[]],
            Metrics: [{ Name: NOVA_SONIC_STREAMS_THROTTLED, Unit: 'Count' }],
        }],
    });
    assert.equal(record[NOVA_SONIC_STREAMS_THROTTLED], 1);
});

test('an emitter with no namespace, or a name not in the catalogue, writes nothing', () => {
    const lines: string[] = [];
    new MetricEmitter('  ', { sink: (l) => lines.push(l) }).count(NOVA_SONIC_STREAMS_THROTTLED);
    new MetricEmitter('lma-test', { sink: (l) => lines.push(l) }).count('NotACatalogueMetric');
    assert.deepEqual(lines, []);
});

// ---------------------------------------------------------------------------
// The lookup acknowledgement clip
// ---------------------------------------------------------------------------

const encoder = new TextEncoder();
const event = (body: unknown) => ({ chunk: { bytes: encoder.encode(JSON.stringify({ event: body })) } });
const clipEvents = [
    event({ contentStart: { type: 'AUDIO', role: 'ASSISTANT' } }),
    event({ audioOutput: { content: Buffer.alloc(32000, 3).toString('base64') } }),
    event({ contentEnd: { type: 'AUDIO' } }),
];

/** A client that refuses the first `refusals` sends and then answers with a clip. */
function clipClient(refusals: number): { client: any; sends: () => number } {
    let sends = 0;
    return {
        sends: () => sends,
        client: {
            send: async () => {
                sends++;
                if (sends <= refusals) throw refusal();
                return { body: (async function* () { for (const e of clipEvents) yield e; })() };
            },
        },
    };
}

test('a clip render refused for the quota is tried again later, and then succeeds', async () => {
    const { client, sends } = clipClient(2);
    const waits: number[] = [];
    const clip = await renderToolAckClip(client, 'm', 'tiffany', undefined, {
        sleep: async (ms) => { waits.push(ms); },
        random: () => 0,
    });
    assert.ok(clip, 'the clip is rendered once the quota allows it');
    assert.equal(sends(), 3);
    assert.deepEqual(waits, [2500, 5000], 'with a backoff that grows');
    assert.equal(warnings.filter((w) => w.startsWith('[nova-throttle] context=tool-ack-clip')).length, 2);
});

test('a clip render refused every time is skipped, without throwing', async () => {
    const { client, sends } = clipClient(Infinity);
    const clip = await renderToolAckClip(client, 'm', 'tiffany', undefined, { attempts: 3, sleep: async () => {} });
    assert.equal(clip, null);
    assert.equal(sends(), 3);
    assert.ok(warnings.includes('[tool-ack] action=render-skipped reason=throttled attempts=3'), warnings.join('\n'));
});

test('a clip render is abandoned once the agent has stopped', async () => {
    const { client, sends } = clipClient(Infinity);
    const clip = await renderToolAckClip(client, 'm', 'tiffany', undefined, {
        sleep: async () => {},
        shouldContinue: () => false,
    });
    assert.equal(clip, null);
    assert.equal(sends(), 1, 'no second stream is opened for a stopped agent');
});

// ---------------------------------------------------------------------------
// The assistant's session, opened on a wake phrase
// ---------------------------------------------------------------------------

function wakePhraseAgent(): any {
    const agent = new NovaAgent({
        modelId: 'amazon.nova-2-sonic-v1:0',
        systemPrompt: 'You are Alex, an AI meeting assistant.',
        activationMode: 'wake_phrase',
        meetingMode: 'normal',
    }) as any;
    agent.assistantOpenBackoff = { baseMs: 2, capMs: 4 };
    return agent;
}

/** A client refusing the first `refusals` sends, then holding a stream open. */
function sessionClient(refusals: number): { client: any; sends: () => number; release: () => void } {
    let sends = 0;
    let release: () => void = () => {};
    const held = new Promise<void>((resolve) => { release = resolve; });
    return {
        sends: () => sends,
        release: () => release(),
        client: {
            send: async () => {
                sends++;
                if (sends <= refusals) throw refusal();
                return { body: (async function* () { await held; })() };
            },
        },
    };
}

test('a wake phrase refused for the quota is retried, and the assistant activates once it opens', async () => {
    const agent = wakePhraseAgent();
    const { client, sends, release } = sessionClient(2);
    agent.bedrockClient = client;
    try {
        await agent.activate(30);
        assert.equal(sends(), 3);
        assert.equal(agent._isActivated, true);
        assert.equal(agent._isActive, true);
        assert.equal(warnings.filter((w) => w.startsWith('[nova-throttle] context=assistant-session')).length, 2);
        assert.equal(warnings.filter((w) => w.startsWith('[nova-throttle] action=retry-activation')).length, 2);
    } finally {
        agent.stopped = true;
        if (agent.activationTimeout) clearTimeout(agent.activationTimeout);
        release();
    }
});

test('a wake phrase refused every time leaves the assistant silent and not activated', async () => {
    const agent = wakePhraseAgent();
    const { client, sends } = sessionClient(Infinity);
    agent.bedrockClient = client;
    await agent.activate(30, 'hey alex what is the weather today');
    assert.equal(sends(), ASSISTANT_OPEN_ATTEMPTS);
    assert.equal(agent._isActivated, false, 'not activated, so the next wake phrase tries again');
    assert.equal(agent.session, null, 'and no dead session is left behind');
    assert.equal(agent._isActive, false);
    assert.ok(warnings.some((w) => w.startsWith('[nova-throttle] action=gave-up-activation')), warnings.join('\n'));
});

test('a second wake phrase while the first is still opening does not open another session', async () => {
    const agent = wakePhraseAgent();
    agent.assistantOpenBackoff = { baseMs: 40, capMs: 40 };
    const { client, sends, release } = sessionClient(1);
    agent.bedrockClient = client;
    try {
        const first = agent.activate(30);
        await new Promise((resolve) => setTimeout(resolve, 5));
        await agent.activate(30);
        await first;
        assert.equal(sends(), 2, 'one refused open and one retry, not a second activation of its own');
        assert.equal(agent._isActivated, true);
    } finally {
        agent.stopped = true;
        if (agent.activationTimeout) clearTimeout(agent.activationTimeout);
        release();
    }
});

test('a second wake phrase while a pre-connect is still in flight does not open another session, and its question is asked', async () => {
    const agent = wakePhraseAgent();
    const { client, sends, release } = sessionClient(1);
    agent.bedrockClient = client;
    const asked: string[] = [];
    agent.sendUserMessage = (text: string) => { asked.push(text); };
    try {
        const preConnect = agent.preConnect();
        assert.equal(agent._preConnecting, true);
        const first = agent.activate(30);
        const second = agent.activate(30, 'hey alex what time is the launch');
        await Promise.all([preConnect, first, second]);
        assert.equal(sends(), 2, 'the refused pre-connect and one retry, and no session of the second activation');
        assert.equal(agent._isActivated, true);
        assert.deepEqual(asked, ['what time is the launch'], 'the question of the later wake phrase is asked');
    } finally {
        agent.stopped = true;
        if (agent.activationTimeout) clearTimeout(agent.activationTimeout);
        release();
    }
});

// ---------------------------------------------------------------------------
// An always-listening session
// ---------------------------------------------------------------------------

test('an always-listening session refused for the quota is rebuilt after a backoff, not a second later', async () => {
    const agent = new NovaAgent({
        modelId: 'amazon.nova-2-sonic-v1:0',
        systemPrompt: 'You are Alex.',
        activationMode: 'always_active',
        meetingMode: 'normal',
    }) as any;
    const scheduled: number[] = [];
    agent.scheduleSessionCreateRetry = (_attempt: () => void, delay: number) => { scheduled.push(delay); };
    let refreshes = 0;
    agent.refreshSession = async () => { refreshes++; };
    agent.bedrockClient = { send: async () => { throw refusal(); } };
    agent.session = agent.newSessionData();
    agent._isActive = true;
    agent.streamingActive = true;
    await agent.initiateBidirectionalStreaming(agent.session);
    await agent.initiateBidirectionalStreaming(agent.session);
    assert.equal(refreshes, 0, 'not rebuilt straight away');
    assert.equal(scheduled.length, 2);
    assert.ok(scheduled[0] >= 1000 && scheduled[0] <= 2000, `first backoff ${scheduled[0]}`);
    assert.ok(scheduled[1] >= 2000 && scheduled[1] <= 4000, `second backoff ${scheduled[1]}`);
    assert.equal(agent._isActive, false);
});

// ---------------------------------------------------------------------------
// The translator's next session
// ---------------------------------------------------------------------------

function chunk(body: unknown): unknown {
    return event(body);
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
const flush = () => new Promise((resolve) => setImmediate(resolve));

function translator(send: () => Promise<unknown>): any {
    const a = new NovaAgent({
        modelId: 'amazon.nova-2-sonic-v1:0',
        systemPrompt: 'You are Alex, an AI meeting assistant.',
        activationMode: 'always_active',
        meetingMode: 'translator',
        translatorLanguageA: 'English',
        translatorLanguageB: 'Spanish',
    }) as any;
    a.bedrockClient = { send };
    a.startSessionRefreshTimer = () => {};
    a.playAudio = async () => {};
    a.refreshSession = async () => { throw new Error('a refused hand-over must not rebuild the session'); };
    const first = a.newSessionData();
    first.id = 'first';
    first.isPromptStartSent = true;
    first.isAudioContentStartSent = true;
    a.session = first;
    a._isActive = true;
    a.streamingActive = true;
    return a;
}

async function stream(a: any, s: any, events: unknown[], between?: (i: number) => void): Promise<void> {
    async function* body() {
        for (let i = 0; i < events.length; i++) {
            between?.(i);
            yield events[i];
        }
    }
    await a.processResponseStream({ body: body() }, s);
    for (let i = 0; i < 5; i++) await flush();
}

const audioInputs = (s: any) => s.queue.filter((e: any) => e.event?.audioInput).map((e: any) => e.event.audioInput);

test('a translator hand-over refused for the quota keeps the current session', async () => {
    const a = translator(async () => { throw refusal(); });
    const first = a.session;
    const live = Buffer.alloc(320, 7);
    await stream(a, first, [...userTurn('hello everyone'), ...answerStarts()]);
    // The refusal arrives while the meeting is being sent to the new session.
    a.sendAudioChunk(live);
    for (let i = 0; i < 5; i++) await flush();

    assert.equal(a.session, first, 'the meeting stays on the session it was leaving');
    assert.equal(first.draining, false);
    assert.equal(first.isActive, true);
    assert.equal(a.drainingSessions.size, 0);
    assert.equal(a._isActive, true, 'still one live stream');
    assert.ok(a.translatorHandoverBlockedUntil > Date.now(), 'no hand-over is tried again straight away');
    assert.ok(
        warnings.some((w) => /^\[translator-turn\] action=kept-session reason=throttled session=first refused=\S+ next_handover_after_ms=\d+ streak=1$/.test(w)),
        warnings.join('\n'),
    );
    assert.ok(warnings.some((w) => w.startsWith('[nova-throttle] context=translator-turn')));

    // What the refused session was sent reaches the kept one.
    const sent = audioInputs(first).map((x: any) => Buffer.from(x.content, 'base64'));
    assert.ok(sent.some((b: Buffer) => b.equals(live)), 'the kept session hears the meeting again');
});

test('after a refused hand-over the translator hands over again on a later turn once the backoff has passed', async () => {
    let refuse = true;
    const a = translator(async () => {
        if (refuse) throw refusal();
        return { body: (async function* () {})() };
    });
    const first = a.session;
    await stream(a, first, [...userTurn('hello everyone'), ...answerStarts()]);
    assert.equal(a.session, first);

    // The rest of the same answer does not hand over: there is no new
    // participant turn for a new session to translate.
    a.translatorHandoverBlockedUntil = 0;
    await stream(a, first, answerStarts());
    assert.equal(a.session, first, 'not mid-answer');

    // While the backoff runs, the next turn is translated by the same session.
    a.translatorHandoverBlockedUntil = Date.now() + 60000;
    await stream(a, first, [...userTurn('how are you'), ...answerStarts()]);
    assert.equal(a.session, first, 'not while the backoff runs');

    refuse = false;
    a.translatorHandoverBlockedUntil = 0;
    first.handedOver = false;
    await stream(a, first, [...userTurn('see you tomorrow'), ...answerStarts()]);
    assert.notEqual(a.session, first, 'hands over again once the quota allows it');
    assert.equal(first.draining, true);
    assert.equal(a.translatorHandoverThrottles, 0, 'an accepted hand-over ends the streak');
    a.closeDrainingSessions('closed');
});
