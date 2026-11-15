/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Wake-phrase assistant mode answers the request it was woken for and nothing
 * else: the reply gate (wake-reply-gate.ts), the prompt written for that state,
 * the stop request read from the model's own transcript, and the activation
 * ending once a stop has silenced it. Also the order in which a new session's
 * setup and the meeting's audio reach its stream.
 *
 * What is NOT covered here: whether Nova Sonic follows the prompt, and how its
 * turn detection splits a live meeting into turns. Both need a live meeting.
 */
import { strict as assert } from 'node:assert';
import { afterEach, beforeEach, mock, test } from 'node:test';
import { ADDRESSED_FOLLOW_ON_MS, WakeReplyGate } from './wake-reply-gate.js';
import { NovaAgent, STOP_DEACTIVATE_MAX_MS, STOP_REQUEST_TOOL_GRACE_MS, WAKE_PHRASE_MODE_PROMPT } from './nova-agent.js';
import { MeetingMode } from './nova-sonic-config-loader.js';
import { routeLogsToConsole } from './logger.js';

routeLogsToConsole();

let logs: string[] = [];
beforeEach(() => {
    logs = [];
    mock.method(console, 'log', (...args: unknown[]) => { logs.push(args.map(String).join(' ')); });
});
afterEach(() => mock.restoreAll());

// ---------------------------------------------------------------------------
// The gate on its own
// ---------------------------------------------------------------------------

test('a turn that does not address the assistant has its reply withheld', () => {
    const gate = new WakeReplyGate();
    gate.noteActivated(0);
    // Well past the follow-on window, as on the live meeting.
    assert.deepEqual(gate.noteUserTurn(false, ADDRESSED_FOLLOW_ON_MS + 1), { audible: false, reason: 'not-addressed' });
    assert.equal(gate.answerOwed(), false);
});

test('a turn with the wake phrase is answered, at any point in the activation', () => {
    const gate = new WakeReplyGate();
    gate.noteActivated(0);
    gate.noteUserTurn(false, 60_000);
    assert.deepEqual(gate.noteUserTurn(true, 70_000), { audible: true, reason: 'addressed' });
    assert.equal(gate.answerOwed(), true);
});

test('a request split by a pause is answered as one request', () => {
    // "Hey Alex" ... "what is on the agenda": two turns to the model.
    const gate = new WakeReplyGate();
    assert.equal(gate.noteUserTurn(true, 1_000).audible, true);
    assert.deepEqual(gate.noteUserTurn(false, 1_000 + ADDRESSED_FOLLOW_ON_MS), { audible: true, reason: 'follow-on' });
    // And the follow-on extends the request, so a third piece still counts.
    assert.equal(gate.noteUserTurn(false, 1_000 + 2 * ADDRESSED_FOLLOW_ON_MS).audible, true);
});

test('the answer to a lookup is heard however much the room talks while it runs', () => {
    const gate = new WakeReplyGate();
    gate.noteActivated(0);
    gate.noteLookupStarted();
    assert.deepEqual(gate.noteUserTurn(false, 30_000), { audible: true, reason: 'lookup-pending' });
    // The preamble ends while the lookup is still running: still owed.
    gate.noteAnswerHeard(true);
    assert.equal(gate.noteUserTurn(false, 40_000).audible, true);
    // The answer has been heard: the next unaddressed turn is not answered.
    gate.noteAnswerHeard(false);
    assert.equal(gate.noteUserTurn(false, 50_000).audible, false);
});

test('once an answer has been heard, nothing more is owed until the assistant is addressed again', () => {
    const gate = new WakeReplyGate();
    gate.noteActivated(0);
    gate.noteAnswerHeard();
    assert.equal(gate.answerOwed(), false);
    assert.equal(gate.noteUserTurn(false, ADDRESSED_FOLLOW_ON_MS + 1).audible, false);
});

// ---------------------------------------------------------------------------
// The gate in the Nova agent, on the event stream
// ---------------------------------------------------------------------------

function chunk(event: unknown): unknown {
    return { chunk: { bytes: new TextEncoder().encode(JSON.stringify({ event })) } };
}
const FINAL = JSON.stringify({ generationStage: 'FINAL' });
const userTurn = (text: string) => [
    chunk({ contentStart: { type: 'TEXT', role: 'USER', additionalModelFields: FINAL } }),
    chunk({ textOutput: { content: text } }),
    chunk({ contentEnd: { type: 'TEXT', stopReason: 'END_TURN' } }),
];
const reply = (text: string) => [
    chunk({ contentStart: { type: 'TEXT', role: 'ASSISTANT', additionalModelFields: FINAL } }),
    chunk({ textOutput: { content: text } }),
    chunk({ contentEnd: { type: 'TEXT', stopReason: 'PARTIAL_TURN' } }),
    chunk({ contentStart: { type: 'AUDIO', role: 'ASSISTANT' } }),
    chunk({ audioOutput: { content: Buffer.from([1, 2]).toString('base64') } }),
    chunk({ contentEnd: { type: 'AUDIO', stopReason: 'END_TURN' } }),
];
const flush = () => new Promise((resolve) => setImmediate(resolve));

function nova(meetingMode: MeetingMode = 'normal', activationMode = 'wake_phrase'): { a: any; played: Buffer[] } {
    const agent = new NovaAgent({
        modelId: 'amazon.nova-2-sonic-v1:0',
        systemPrompt: 'You are Alex, an AI meeting assistant.',
        activationMode: activationMode as any,
        meetingMode,
    });
    const a = agent as any;
    const played: Buffer[] = [];
    a.playAudio = async (b: Buffer) => { played.push(b); };
    a.writePlayback = (b: Buffer) => { played.push(b); return true; };
    a.markSpeaking = () => {};
    a.playConfirmationTone = () => {};
    a.session = {
        id: 's1', isActive: true, queue: [], promptName: 'p', audioContentId: 'a', signal: null,
        isAudioContentStartSent: true,
    };
    a._isActive = true;
    return { a, played };
}

async function stream(a: any, events: unknown[]): Promise<void> {
    async function* body() {
        for (const event of events) yield event;
    }
    await a.processResponseStream({ body: body() }, a.session);
    for (let i = 0; i < 5; i++) await flush();
}

/** As activate() leaves it, a minute ago: activated, and past the follow-on
 *  window of the request it was woken for, so every later turn stands on its own. */
function activated(a: any): void {
    a._isActivated = true;
    a.replyGate = new WakeReplyGate();
    a.replyGate.noteActivated(Date.now() - 60_000);
}

test('nova: in wake-phrase mode a reply to conversation not addressed to it is not played or transcribed', async () => {
    const { a, played } = nova();
    activated(a);
    await stream(a, [
        ...userTurn('trying to open a connection which has a delay in itself so if'),
        ...reply("Okay, I understand. You're confirming that the previous system would trigger activation."),
    ]);
    assert.equal(played.length, 0, 'the unaddressed reply was played');
    assert.ok(logs.some((l) => l.includes('[wake-gate] action=withheld-reply reason=not-addressed')));
    // Kept out of the history the next session is seeded from, too.
    assert.ok(!a.conversationHistory.some((t: any) => t.role === 'ASSISTANT'));
});

test('nova: in wake-phrase mode a turn that says the wake phrase is answered', async () => {
    const { a, played } = nova();
    activated(a);
    await stream(a, [...userTurn('hey alex what is on the agenda'), ...reply('The agenda has three items.')]);
    assert.equal(played.length, 1);
});

test('nova: "Okay Alex" addresses the assistant like "OK Alex"', async () => {
    const { a, played } = nova();
    activated(a);
    await stream(a, [...userTurn('okay alex what is on the agenda'), ...reply('The agenda has three items.')]);
    assert.equal(played.length, 1);
});

test('nova: the gate leaves always-listening assistant mode alone', async () => {
    const { a, played } = nova('normal', 'always_active');
    a._isActivated = true;
    await stream(a, [...userTurn('what did we decide last week'), ...reply('You decided to ship on Friday.')]);
    assert.equal(played.length, 1);
});

test('nova: a lookup asked for as the reply to an unaddressed turn is not run', async () => {
    const { a } = nova();
    a.strandsLambdaArn = 'strands';
    activated(a);
    const invoked: unknown[] = [];
    a.invokeStrandsAgent = async (content: unknown) => { invoked.push(content); return { message: 'x' }; };
    await stream(a, [
        ...userTurn('there is a bunch of messages from lma'),
        chunk({ toolUse: { toolName: 'strands_agent', toolUseId: 'lookup-1', content: '{"query":"messages"}' } }),
        chunk({ contentEnd: { type: 'TOOL' } }),
    ]);
    assert.equal(invoked.length, 0);
    assert.ok(logs.some((l) => l.includes('[lookup] action=skipped reason=unaddressed-turn muted=false')));
});

// ---------------------------------------------------------------------------
// The prompt for the state
// ---------------------------------------------------------------------------

function systemPrompt(a: any): string {
    return a.buildSystemPromptContent();
}

test('the wake-phrase assistant prompt says to answer only addressed requests, and asks for no acknowledgements', () => {
    const prompt = systemPrompt(nova('normal', 'wake_phrase').a);
    assert.ok(prompt.includes(WAKE_PHRASE_MODE_PROMPT));
    assert.match(prompt, /Answer only a request that is addressed to you with your wake phrase/);
    assert.match(prompt, /When you have answered, stop and stay silent/);
    // The general block asks for "Okay" on every interruption, which in a
    // meeting is an "Okay" to every participant who talks over the assistant.
    assert.doesNotMatch(prompt, /IMMEDIATELY acknowledge the interruption/);
    // Stopping still works the same way.
    assert.match(prompt, /STOP REQUESTS:/);
});

test('the wake-phrase section is only in the prompt for wake-phrase assistant mode', () => {
    for (const [meetingMode, activationMode] of [
        ['normal', 'always_active'],
        ['group', 'always_active'],
        ['translator', 'always_active'],
    ] as Array<[MeetingMode, string]>) {
        assert.doesNotMatch(systemPrompt(nova(meetingMode, activationMode).a), /WAKE PHRASE MODE/, `${meetingMode}/${activationMode}`);
    }
});

// ---------------------------------------------------------------------------
// Stopping
// ---------------------------------------------------------------------------

test("nova: a stop request on the model's own transcript is acted on in code", async () => {
    // The live meeting's second and third stops reached only this transcript.
    for (const said of ['hey alex stop', 'okay hey alex stop', 'Okay, Alex. Stop.', 'alex stop']) {
        const { a } = nova();
        activated(a);
        const asked: string[] = [];
        a.requestStopTalking = (reason: string) => { asked.push(reason); return true; };
        await stream(a, userTurn(said));
        assert.equal(asked.length, 1, `not acted on: "${said}"`);
    }
});

test('nova: a turn that mentions stopping is not a stop request', async () => {
    const { a } = nova();
    activated(a);
    const asked: string[] = [];
    a.requestStopTalking = (reason: string) => { asked.push(reason); return true; };
    await stream(a, userTurn("she's getting better at stopping now so that's a good thing"));
    await stream(a, userTurn('hey alex why did the build stop'));
    assert.equal(asked.length, 0);
});

test('nova: a stop request ends the wake-phrase activation once it has silenced the assistant', async () => {
    const agent = new NovaAgent({
        modelId: 'amazon.nova-2-sonic-v1:0',
        systemPrompt: 'You are Alex, an AI meeting assistant.',
        activationMode: 'wake_phrase',
        meetingMode: 'normal',
    });
    const a = agent as any;
    a._isActivated = true;
    assert.equal(agent.requestStopTalking('test'), true);
    await new Promise((resolve) => setTimeout(resolve, STOP_REQUEST_TOOL_GRACE_MS + 100));
    assert.equal(agent.isMuted(), true);
    await new Promise((resolve) => setTimeout(resolve, 400));
    assert.equal(agent.isActivated(), false, 'still activated after the stop');
    assert.ok(STOP_DEACTIVATE_MAX_MS >= 1000, 'leaves room for the acknowledgement');
});

test('nova: being woken again before the stop closes the activation keeps the new activation', async () => {
    const agent = new NovaAgent({
        modelId: 'amazon.nova-2-sonic-v1:0',
        systemPrompt: 'You are Alex, an AI meeting assistant.',
        activationMode: 'wake_phrase',
        meetingMode: 'normal',
    });
    const a = agent as any;
    a._isActivated = true;
    a.playbackEndsAt = Date.now() + 1000; // the acknowledgement is still playing
    a.mute('test');
    a.endActivationAfterStop('test');
    // A wake phrase in the meantime unmutes, as activate() does.
    a.unmute('wake phrase activation');
    await new Promise((resolve) => setTimeout(resolve, 1400));
    assert.equal(agent.isActivated(), true);
});

// ---------------------------------------------------------------------------
// A new session's setup goes first
// ---------------------------------------------------------------------------

test("nova: the meeting's audio is not queued on a session before its setup", () => {
    const { a } = nova('normal', 'always_active');
    a._isActivated = true;
    a.session.isAudioContentStartSent = false;
    const audioEvents = () => a.session.queue.filter((e: any) => e.event?.audioInput).length;
    a.sendAudioChunk(Buffer.alloc(4));
    assert.equal(audioEvents(), 0, 'audio queued ahead of sessionStart');
    a.session.isAudioContentStartSent = true;
    a.sendAudioChunk(Buffer.alloc(4));
    assert.equal(audioEvents(), 1);
});
