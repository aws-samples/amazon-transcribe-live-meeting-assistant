/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * A `strands_agent` lookup requested while the voice assistant is muted, and not
 * prompted by a turn that addressed it, is answered without invoking the
 * Strands Lambda.
 *
 * Recorded on a live meeting: an always-listening assistant, muted after being
 * asked to stop, asked for 31 lookups in four minutes of team conversation; every
 * answer was dropped by the mute. The Nova tests drive processResponseStream
 * with Nova's events in the order a live stream delivers them — the user's
 * FINAL turn, then the tool-use event, then the end of the tool block — so the
 * wake-phrase unmute and the lookup decision run in their real order.
 *
 * What is NOT covered here: whether the model stops asking once told, which
 * needs a live meeting.
 */
import { strict as assert } from 'node:assert';
import { afterEach, beforeEach, mock, test } from 'node:test';
import { ElevenLabsAgent } from './elevenlabs-agent.js';
import {
    addressesAssistant,
    mutedLookupSkipReason,
    SKIPPED_LOOKUP_RESULT,
} from './muted-lookup.js';
import { NovaAgent } from './nova-agent.js';
import { MeetingMode } from './nova-sonic-config-loader.js';

const STRANDS_ARN = 'arn:aws:lambda:us-east-1:123456789012:function:strands';
const WAKE = ['hey alex', 'ok alex'];

let captured: string[] = [];
const realLog = console.log;

beforeEach(() => {
    mock.timers.enable({ apis: ['setTimeout'] });
    captured = [];
    console.log = (...args: unknown[]) => {
        captured.push(args.map((arg) => String(arg)).join(' '));
    };
});

afterEach(() => {
    console.log = realLog;
    mock.timers.reset();
});

function lookupLines(): string[] {
    return captured.filter((l) => l.startsWith('[lookup]'));
}

function flush(): Promise<void> {
    return new Promise((resolve) => setImmediate(resolve));
}

// ---------------------------------------------------------------------------
// The decision
// ---------------------------------------------------------------------------

test('an audible assistant runs every lookup', () => {
    assert.equal(mutedLookupSkipReason(false, 'what did we decide last week', WAKE), null);
    assert.equal(mutedLookupSkipReason(false, '', WAKE), null);
});

test('a muted assistant does not run a lookup for talk not addressed to it', () => {
    assert.equal(mutedLookupSkipReason(true, 'are you still in the middle of that cost analysis', WAKE), 'not-addressed');
    assert.equal(mutedLookupSkipReason(true, '', WAKE), 'not-addressed');
});

test('a muted assistant runs a lookup for a turn that addresses it', () => {
    // The order the agents normally see is unmute first, then the tool call;
    // this covers the two arriving the other way round.
    assert.equal(mutedLookupSkipReason(true, 'Hey, Alex. What did we decide last week?', WAKE), null);
});

test('a stop request is addressed but does not count as asking for a lookup', () => {
    assert.equal(mutedLookupSkipReason(true, 'hey alex stop talking', WAKE), 'not-addressed');
});

test('addressing needs the whole wake phrase as words', () => {
    assert.equal(addressesAssistant('hey alexander', WAKE), false);
    assert.equal(addressesAssistant('okay alex', WAKE), false);
    assert.equal(addressesAssistant('so, OK Alex — the numbers?', WAKE), true);
});

// ---------------------------------------------------------------------------
// Nova Sonic
// ---------------------------------------------------------------------------

interface NovaHarness {
    agent: NovaAgent;
    a: any;
    invocations: () => number;
    toolResults: () => any[];
}

function nova(meetingMode: MeetingMode = 'normal'): NovaHarness {
    const agent = new NovaAgent({
        modelId: 'amazon.nova-2-sonic-v1:0',
        systemPrompt: 'You are Alex, an AI meeting assistant.',
        activationMode: 'always_active',
        meetingMode,
        strandsLambdaArn: STRANDS_ARN,
    });
    const a = agent as any;
    let invocations = 0;
    a.lambdaClient = {
        send: async () => {
            invocations++;
            return { Payload: new TextEncoder().encode(JSON.stringify({ message: 'the answer' })) };
        },
    };
    a.ensurePacatStream = () => null;
    a.audibleOutput = () => false;
    a.closeSession = async () => {};
    a.session = { isActive: true, queue: [], promptName: 'p', audioContentId: 'a' };
    a._isActive = true;
    a._isActivated = true;
    a.toolAckClip = null;
    const toolResults = () => a.session.queue
        .filter((e: any) => e.event?.toolResult)
        .map((e: any) => JSON.parse(e.event.toolResult.content));
    return { agent, a, invocations: () => invocations, toolResults };
}

function chunk(event: unknown): unknown {
    return { chunk: { bytes: new TextEncoder().encode(JSON.stringify({ event })) } };
}

/** A FINAL user turn followed by a `strands_agent` call, as Nova sends them. */
function userTurnThenLookup(text: string, toolUseId = 'tool-1'): unknown[] {
    return [
        chunk({
            contentStart: {
                type: 'TEXT',
                role: 'USER',
                additionalModelFields: JSON.stringify({ generationStage: 'FINAL' }),
            },
        }),
        chunk({ textOutput: { content: text } }),
        chunk({ contentEnd: { type: 'TEXT' } }),
        chunk({
            toolUse: {
                toolName: 'strands_agent',
                toolUseId,
                content: JSON.stringify({ query: 'cost analysis status' }),
            },
        }),
        chunk({ contentEnd: { type: 'TOOL' } }),
    ];
}

async function stream(h: NovaHarness, events: unknown[]): Promise<void> {
    async function* body() {
        for (const event of events) yield event;
    }
    await h.a.processResponseStream({ body: body() });
    for (let i = 0; i < 5; i++) await flush();
}

test('nova: a muted assistant answers an unaddressed lookup without invoking Strands', async () => {
    const h = nova('normal');
    h.a._isMuted = true;
    await stream(h, userTurnThenLookup('are you still in the middle of that cost analysis'));
    assert.equal(h.invocations(), 0);
    assert.deepEqual(h.toolResults(), [SKIPPED_LOOKUP_RESULT]);
    assert.deepEqual(lookupLines(), [
        '[lookup] action=skipped reason=not-addressed muted=true mode=normal activation=always_active',
    ]);
    assert.equal(h.a._isProcessingTool, false, 'a skipped lookup does not hold the session busy');
    mock.timers.tick(10_000);
    assert.equal(captured.filter((l) => l.startsWith('[tool-ack]')).length, 0, 'nothing to acknowledge');
});

test('nova: in group mode an unaddressed lookup is not run either', async () => {
    const h = nova('group');
    assert.equal(h.a._isMuted, true, 'group mode starts muted');
    await stream(h, userTurnThenLookup('the lambda runtime costs are the same or more than transcribe'));
    assert.equal(h.invocations(), 0);
    assert.deepEqual(lookupLines(), [
        '[lookup] action=skipped reason=not-addressed muted=true mode=group activation=always_active',
    ]);
});

test('nova: in group mode a wake phrase unmutes and its lookup runs', async () => {
    const h = nova('group');
    await stream(h, userTurnThenLookup('hey alex what did we decide about the asr default last week'));
    assert.equal(h.a._isMuted, false);
    assert.equal(h.invocations(), 1);
    assert.deepEqual(h.toolResults(), [{ result: 'the answer' }]);
    assert.deepEqual(lookupLines(), []);
});

test('nova: a normal-mode assistant muted by a stop request runs the lookup it is asked for again', async () => {
    const h = nova('normal');
    h.a._isMuted = true;
    await stream(h, userTurnThenLookup('hey alex can you look up the transcribe price'));
    assert.equal(h.a._isMuted, false);
    assert.equal(h.invocations(), 1);
    assert.deepEqual(lookupLines(), []);
});

test('nova: an audible assistant runs a follow-up lookup that does not name it', async () => {
    // Group mode's conversation window, and normal mode generally.
    const h = nova('group');
    h.a._isMuted = false;
    await stream(h, userTurnThenLookup('and what about the one before that'));
    assert.equal(h.invocations(), 1);
    assert.deepEqual(lookupLines(), []);
});

test('nova: a turn heard by an earlier session does not let a lookup through on a new one', () => {
    const h = nova('group');
    h.a.lastUserTurnText = 'hey alex what did we decide last week';
    assert.equal(h.a.strandsLookupSkipReason(), null);
    h.a.streamingActive = false;
    h.a.initiateBidirectionalStreaming = async () => {};
    // Cleared synchronously, before createSession's first await.
    void h.a.createSession(true);
    assert.equal(h.a.strandsLookupSkipReason(), 'not-addressed');
});

test('nova: the group prompt asks the model to look things up only when asked', () => {
    const h = nova('group');
    const prompt: string = h.a.buildSystemPromptContent();
    assert.match(prompt, /Only use strands_agent when someone asks YOU for information/);
    assert.match(prompt, /Do NOT look anything up in response to participants talking to each other/);
    assert.doesNotMatch(nova('normal').a.buildSystemPromptContent(), /Only use strands_agent when someone asks YOU/);
});

// ---------------------------------------------------------------------------
// ElevenLabs
// ---------------------------------------------------------------------------

function elevenLabs(meetingMode: MeetingMode): { a: any; invocations: () => number; sent: any[] } {
    const agent = new ElevenLabsAgent({
        apiKey: 'test-key',
        meetingMode,
        activationMode: 'always_active',
        strandsLambdaArn: STRANDS_ARN,
    });
    const a = agent as any;
    let invocations = 0;
    a.lambdaClient = {
        send: async () => {
            invocations++;
            return { Payload: new TextEncoder().encode(JSON.stringify({ message: 'the answer' })) };
        },
    };
    const sent: any[] = [];
    a.ws = { send: (m: string) => sent.push(JSON.parse(m)) };
    a.isConnected = true;
    return { a, invocations: () => invocations, sent };
}

function clientToolCall(): unknown {
    return {
        type: 'client_tool_call',
        client_tool_call: { tool_name: 'strands_agent', tool_call_id: 'call-1', parameters: { query: 'q' } },
    };
}

function heard(text: string): unknown {
    return { type: 'user_transcript', user_transcription_event: { user_transcript: text } };
}

test('elevenlabs: in group mode an unaddressed lookup is not run', async () => {
    const h = elevenLabs('group');
    await h.a.handleWebSocketMessage(heard('the lambda runtime costs are the same or more'));
    await h.a.handleWebSocketMessage(clientToolCall());
    assert.equal(h.invocations(), 0);
    assert.equal(h.sent.length, 1);
    assert.equal(h.sent[0].type, 'client_tool_result');
    assert.deepEqual(JSON.parse(h.sent[0].result), SKIPPED_LOOKUP_RESULT);
    assert.deepEqual(lookupLines(), [
        '[lookup] action=skipped reason=not-addressed muted=true mode=group activation=always_active',
    ]);
});

test('elevenlabs: in group mode a wake phrase unmutes and its lookup runs', async () => {
    const h = elevenLabs('group');
    await h.a.handleWebSocketMessage(heard('hey alex what did we decide last week'));
    await h.a.handleWebSocketMessage(clientToolCall());
    assert.equal(h.invocations(), 1);
    assert.equal(h.sent[0].result, 'the answer');
    assert.deepEqual(lookupLines(), []);
});
