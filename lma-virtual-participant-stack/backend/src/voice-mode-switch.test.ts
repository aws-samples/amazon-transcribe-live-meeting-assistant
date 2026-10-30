/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Changing the voice assistant's mode by asking it.
 *
 * The Nova tests drive processResponseStream with Nova's events in live order:
 * the participant's turn, the `set_mode` tool use, the end of the tool block,
 * then the spoken confirmation. `reconfigure` is stood in for, so what is
 * checked is the request the voice route makes and when it makes it.
 *
 * What is NOT covered here: whether the model calls the tool for the phrasings
 * people use. That needs the live service.
 */
import { strict as assert } from 'node:assert';
import { afterEach, beforeEach, mock, test } from 'node:test';
import { MeetingMode } from './nova-sonic-config-loader.js';
import { NovaAgent } from './nova-agent.js';
import {
    isStopTranslatingRequest,
    ModeState,
    predictVoiceModeChange,
    translatorAddressees,
    voiceModeCommand,
    voiceModeSentence,
} from './voice-mode-switch.js';

// ---------------------------------------------------------------------------
// The vocabulary and the sentences
// ---------------------------------------------------------------------------

test('each spoken mode is the chat command of the same name', () => {
    assert.deepEqual(voiceModeCommand('translator'), { meetingMode: 'translator', activationMode: 'always_active' });
    assert.deepEqual(voiceModeCommand('assistant'), { meetingMode: 'normal' });
    assert.deepEqual(voiceModeCommand('listen'), { meetingMode: 'group', activationMode: 'always_active' });
    assert.deepEqual(voiceModeCommand('always_listening'), { activationMode: 'always_active' });
    assert.deepEqual(voiceModeCommand('wake_phrase'), { activationMode: 'wake_phrase' });
});

const normalWake: ModeState = { meetingMode: 'normal', activationMode: 'wake_phrase', reconfiguring: false };
const listening: ModeState = { meetingMode: 'group', activationMode: 'always_active', reconfiguring: false };

test('the prediction follows reconfigure: in progress, then unchanged, then the combination rule', () => {
    assert.deepEqual(predictVoiceModeChange(normalWake, voiceModeCommand('translator')), { result: 'applied' });
    assert.deepEqual(predictVoiceModeChange(normalWake, voiceModeCommand('assistant')), { result: 'unchanged' });
    assert.deepEqual(predictVoiceModeChange(normalWake, voiceModeCommand('wake_phrase')), { result: 'unchanged' });
    assert.deepEqual(
        predictVoiceModeChange(listening, voiceModeCommand('wake_phrase')),
        { result: 'declined', reason: 'needs-always-listening' },
    );
    assert.deepEqual(
        predictVoiceModeChange({ ...normalWake, reconfiguring: true }, voiceModeCommand('wake_phrase')),
        { result: 'declined', reason: 'in-progress' },
    );
});

test('the confirmation is one sentence naming the outcome', () => {
    const say = (state: ModeState, mode: Parameters<typeof voiceModeCommand>[0]) => {
        const command = voiceModeCommand(mode);
        return voiceModeSentence(state, command, predictVoiceModeChange(state, command));
    };
    assert.equal(say(normalWake, 'translator'), 'Switching to translator mode.');
    assert.equal(say(normalWake, 'listen'), 'Switching to listen mode.');
    assert.equal(say(listening, 'assistant'), 'Switching to assistant mode.');
    assert.equal(say(normalWake, 'assistant'), "I'm already in assistant mode.");
    assert.equal(say(normalWake, 'always_listening'), "Okay, I'm now always listening.");
    assert.equal(say(normalWake, 'wake_phrase'), "I'm already listening for the wake phrase.");
    assert.equal(say(listening, 'wake_phrase'), 'I have to keep listening in listen mode. Switch me to assistant mode first.');
});

test('a translator is addressed by a wake phrase or by the name in its pause phrase', () => {
    const names = translatorAddressees(['hey alex', 'ok alex'], ['translator mute', 'alex mute']);
    assert.deepEqual(names, ['translator', 'hey alex', 'ok alex', 'alex']);
    for (const text of ['Alex, stop translating.', 'Hey Alex, please stop translating', 'alex go back to assistant mode',
        'OK Alex, stop the translation now.']) {
        assert.equal(isStopTranslatingRequest(text, names), true, text);
    }
});

test('stopping translation needs the name and nothing else asked for', () => {
    const names = translatorAddressees(['hey alex'], ['alex mute']);
    for (const text of ['stop translating', 'Alex, stop translating this part later',
        'I asked Alex to stop translating yesterday', 'alexander stop translating', '']) {
        assert.equal(isStopTranslatingRequest(text, names), false, text);
    }
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
const userTurn = (text: string) => [
    chunk({ contentStart: { type: 'TEXT', role: 'USER', additionalModelFields: FINAL } }),
    chunk({ textOutput: { content: text } }),
    chunk({ contentEnd: { type: 'TEXT', stopReason: 'END_TURN' } }),
];
const setMode = (mode: string) => [
    chunk({ toolUse: { toolName: 'set_mode', toolUseId: 'sm-1', content: JSON.stringify({ mode }) } }),
    chunk({ contentEnd: { type: 'TOOL' } }),
];
const spoken = () => [
    chunk({ contentStart: { type: 'AUDIO', role: 'ASSISTANT' } }),
    chunk({ audioOutput: { content: Buffer.from([1, 2]).toString('base64') } }),
    chunk({ contentEnd: { type: 'AUDIO', stopReason: 'END_TURN' } }),
];
const flush = () => new Promise((resolve) => setImmediate(resolve));

function nova(meetingMode: MeetingMode = 'normal', activationMode = 'always_active'): { a: any; requests: any[]; played: Buffer[] } {
    const agent = new NovaAgent({
        modelId: 'amazon.nova-2-sonic-v1:0',
        systemPrompt: 'You are Alex, an AI meeting assistant.',
        activationMode: activationMode as any,
        meetingMode,
        translatorLanguageA: 'English',
        translatorLanguageB: 'Spanish',
    });
    const a = agent as any;
    const requests: any[] = [];
    const played: Buffer[] = [];
    a.reconfigure = async (request: any) => {
        requests.push(request);
        const before = { meetingMode: a.meetingMode, activationMode: a.activationMode };
        if (request.meetingMode) a.meetingMode = request.meetingMode;
        if (request.activationMode) a.activationMode = request.activationMode;
        const changed = [
            ...(a.meetingMode !== before.meetingMode ? ['meetingMode'] : []),
            ...(a.activationMode !== before.activationMode ? ['activationMode'] : []),
        ];
        return {
            result: changed.length ? 'applied' : 'unchanged', changed, sessionRebuilt: true,
            meetingModeBefore: before.meetingMode, meetingModeAfter: a.meetingMode,
            activationModeBefore: before.activationMode, activationModeAfter: a.activationMode,
        };
    };
    a.playAudio = async (b: Buffer) => { played.push(b); };
    a.writePlayback = (b: Buffer) => { played.push(b); return true; };
    a.markSpeaking = () => {};
    a.playConfirmationTone = () => {};
    a.session = { id: 's1', isActive: true, queue: [], promptName: 'p', audioContentId: 'a', signal: null };
    a._isActive = true;
    a._isActivated = true;
    return { a, requests, played };
}

async function stream(a: any, events: unknown[]): Promise<void> {
    async function* body() {
        for (const event of events) yield event;
    }
    await a.processResponseStream({ body: body() }, a.session);
    for (let i = 0; i < 5; i++) await flush();
}

const toolResults = (a: any) => a.session.queue
    .filter((e: any) => e.event?.toolResult)
    .map((e: any) => JSON.parse(e.event.toolResult.content));

test('nova: set_mode is offered outside translator mode only, with its prompt', () => {
    for (const mode of ['normal', 'group'] as MeetingMode[]) {
        const { a } = nova(mode);
        assert.ok(a.buildToolSpecs().some((t: any) => t.toolSpec.name === 'set_mode'), mode);
        assert.match(a.buildSystemPromptContent(), /MODE CHANGES:/);
    }
    const { a } = nova('translator');
    assert.deepEqual(a.buildToolSpecs().map((t: any) => t.toolSpec.name), ['mute']);
    assert.doesNotMatch(a.buildSystemPromptContent(), /set_mode/);
});

test('nova: an addressed set_mode is confirmed, then switched once the confirmation is spoken', async () => {
    const { a, requests } = nova('normal', 'wake_phrase');
    await stream(a, [...userTurn('hey alex switch to translator mode'), ...setMode('translator')]);
    const [result] = toolResults(a);
    assert.equal(result.status, 'applied');
    assert.match(result.instruction, /^Say exactly "Switching to translator mode\." and nothing else\.$/);
    assert.equal(requests.length, 0, 'not before the confirmation has been spoken');

    await stream(a, spoken());
    assert.equal(requests.length, 1);
    assert.equal(requests[0].route, 'voice');
    assert.equal(requests[0].meetingMode, 'translator');
    assert.equal(requests[0].activationMode, 'always_active');
    assert.ok(logs.some((l) => l === '[voice-mode] action=switch after=confirmed'));
});

test('nova: the switch is made anyway if no confirmation is spoken', async () => {
    const { a, requests } = nova();
    a.VOICE_MODE_SWITCH_MAX_WAIT_MS = 10;
    await stream(a, [...userTurn('hey alex wake mode'), ...setMode('wake_phrase')]);
    await new Promise((resolve) => setTimeout(resolve, 30));
    assert.equal(requests.length, 1);
    assert.equal(requests[0].activationMode, 'wake_phrase');
});

test('nova: a set_mode nobody asked for by name is not acted on, and its reply is not heard', async () => {
    const { a, requests, played } = nova();
    await stream(a, [...userTurn('we should try translator mode next week'), ...setMode('translator'), ...spoken()]);
    assert.equal(toolResults(a)[0].status, 'not_done');
    assert.equal(requests.length, 0);
    assert.equal(played.length, 0, 'the reply to the call is dropped');

    // The next turn is heard as usual.
    await stream(a, [...userTurn('hey alex what time is it'), ...spoken()]);
    assert.equal(played.length, 1);
});

test('nova: a set_mode for the mode it is in says so and changes nothing', async () => {
    const { a, requests } = nova();
    await stream(a, [...userTurn('hey alex assistant mode'), ...setMode('assistant'), ...spoken()]);
    const [result] = toolResults(a);
    assert.equal(result.status, 'unchanged');
    assert.match(result.instruction, /I'm already in assistant mode\./);
    assert.equal(requests.length, 0);
});

test('nova: leaving listen mode by voice restores the activation it replaced', async () => {
    const { a, requests } = nova('normal', 'wake_phrase');
    await stream(a, [...userTurn('hey alex listen mode'), ...setMode('listen'), ...spoken()]);
    a.session.queue = [];
    await stream(a, [...userTurn('hey alex go back to assistant mode'), ...setMode('assistant'), ...spoken()]);
    assert.deepEqual(
        requests.map((r) => [r.meetingMode, r.activationMode]),
        [['group', 'always_active'], ['normal', 'wake_phrase']],
    );
});

test('nova: "Alex, stop translating" leaves translator mode and drops the translation of it', async () => {
    const { a, requests, played } = nova('translator');
    a.stoppedTranslatingClip = Buffer.from([7]);
    const session = a.session;
    await stream(a, [...userTurn('alex stop translating'), ...spoken()]);
    assert.equal(requests.length, 1);
    assert.equal(requests[0].route, 'voice');
    assert.equal(requests[0].meetingMode, 'normal');
    assert.equal(session.silenced, true);
    assert.deepEqual(played.map((b) => [...b]), [[7]], 'only the confirmation is heard');
});

test('nova: other speech in translator mode is translated as usual', async () => {
    const { a, requests, played } = nova('translator');
    a.startNextTranslatorSession = () => {};
    await stream(a, [...userTurn('alex said we should stop translating the slides'), ...spoken()]);
    assert.equal(requests.length, 0);
    assert.deepEqual(played.map((b) => [...b]), [[1, 2]]);
});
