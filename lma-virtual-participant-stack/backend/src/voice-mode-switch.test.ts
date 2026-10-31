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
import { applyAssistantChatCommand } from './assistant-toggles.js';
import { MeetingMode } from './nova-sonic-config-loader.js';
import { NovaAgent } from './nova-agent.js';
import { onAssistantTurn } from './translator-transcript.js';
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

test('stopping translation needs no name, but nothing else may be asked for', () => {
    const names = translatorAddressees(['hey alex'], ['translator mute', 'alex mute']);
    for (const text of ['stop translating', 'Stop translating.', 'Stop translation, please.', 'translator off',
        'Exit translator mode.', 'Hey Alex, turn off the translator', 'end the translation', 'OK, stop translating now']) {
        assert.equal(isStopTranslatingRequest(text, names), true, text);
    }
    for (const text of ['Alex, stop translating this part later', 'I asked Alex to stop translating yesterday',
        'we should stop translating the slides after lunch', 'did you stop translating', 'alexander stop translating',
        'translator mute', 'stop', '']) {
        assert.equal(isStopTranslatingRequest(text, names), false, text);
    }
    // The short forms switch when said on their own, as an answer would be,
    // and not inside a question.
    assert.equal(isStopTranslatingRequest('Assistant mode?', names), true);
    assert.equal(isStopTranslatingRequest('is it in assistant mode', names), false);
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
        // The one refusal these tests need: leaving always-listening in listen mode.
        if (!request.meetingMode && request.activationMode === 'wake_phrase' && a.meetingMode === 'group') {
            return {
                result: 'declined', changed: [], sessionRebuilt: false,
                meetingModeBefore: before.meetingMode, meetingModeAfter: before.meetingMode,
                activationModeBefore: before.activationMode, activationModeAfter: before.activationMode,
            };
        }
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

test('nova: a set_mode for the mode it is in says so, and is recorded like the chat command', async () => {
    const { a, requests } = nova();
    await stream(a, [...userTurn('hey alex assistant mode'), ...setMode('assistant'), ...spoken()]);
    const [result] = toolResults(a);
    assert.equal(result.status, 'unchanged');
    assert.match(result.instruction, /I'm already in assistant mode\./);
    // Asked at once, without waiting for the sentence: it rebuilds nothing.
    assert.equal(requests.length, 1);
    assert.equal(requests[0].route, 'voice');
    assert.equal(a.meetingMode, 'normal');
});

test('nova: a "wake mode" refused in listen mode is restored on going back to assistant mode', async () => {
    const { a, requests } = nova('normal', 'always_active');
    // The restore memory is the meeting's, shared with the tests above; an
    // explicit activation command clears it, as it does in a meeting.
    await applyAssistantChatCommand(a, { activationMode: 'always_active' }, null);
    requests.length = 0;
    for (const [said, mode] of [['hey alex listen mode', 'listen'], ['hey alex wake mode', 'wake_phrase'],
        ['hey alex go back to assistant mode', 'assistant']]) {
        a.session.queue = [];
        await stream(a, [...userTurn(said), ...setMode(mode), ...spoken()]);
    }
    assert.deepEqual(
        requests.map((r) => [r.meetingMode, r.activationMode]),
        [['group', 'always_active'], [undefined, 'wake_phrase'], ['normal', 'wake_phrase']],
    );
    assert.equal(a.activationMode, 'wake_phrase');
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

test('nova: "Alex, stop translating" also works while the translator is paused', async () => {
    const { a, requests } = nova('translator');
    a._translatorState = 'MUTED';
    await stream(a, userTurn('alex stop translating'));
    assert.equal(requests.length, 1);
    assert.equal(requests[0].meetingMode, 'normal');
});

test('nova: a pause asked for after a declined "stop translating" is a pause', async () => {
    const { a } = nova('translator');
    a.reconfigure = async () => ({ result: 'declined', changed: [], sessionRebuilt: false,
        meetingModeBefore: 'translator', meetingModeAfter: 'translator',
        activationModeBefore: 'always_active', activationModeAfter: 'always_active' });
    a.MUTE_TOOL_AUDIO_FLUSH_MS = 0;
    a.refreshSession = async () => {};
    await stream(a, userTurn('stop translating'));
    a.session = { id: 's2', isActive: true, queue: [], promptName: 'p', audioContentId: 'a', signal: null };
    await stream(a, [...userTurn('translator mute'), ...muteTool()]);
    await new Promise((resolve) => setTimeout(resolve, 10));
    assert.equal(a._translatorState, 'MUTED');
});

test('nova: a declined "stop translating" leaves the translator heard', async () => {
    const { a } = nova('translator');
    a.reconfigure = async () => ({ result: 'declined', changed: [], sessionRebuilt: false,
        meetingModeBefore: 'translator', meetingModeAfter: 'translator',
        activationModeBefore: 'always_active', activationModeAfter: 'always_active' });
    const session = a.session;
    await stream(a, userTurn('alex stop translating'));
    assert.equal(session.silenced, false);
    assert.ok(logs.some((l) => l === '[voice-mode] action=stop-translating-declined meetingMode=translator'));
});

test('nova: without the rendered clip, leaving translator mode is confirmed with the pause tone', async () => {
    const { a } = nova('translator');
    const tones: string[] = [];
    a.playConfirmationTone = (kind: string) => { tones.push(kind); };
    await stream(a, userTurn('alex stop translating'));
    assert.deepEqual(tones, ['mute']);
});

test('nova: a second set_mode while one is waiting is told the switch is still under way', async () => {
    const { a, requests } = nova('normal', 'wake_phrase');
    await stream(a, [...userTurn('hey alex switch to translator mode'), ...setMode('translator')]);
    await stream(a, [...userTurn('hey alex listen mode'), ...setMode('listen')]);
    const results = toolResults(a);
    assert.equal(results[1].status, 'declined');
    assert.match(results[1].instruction, /I'm still switching modes\. Please ask again in a moment\./);
    await stream(a, spoken());
    assert.deepEqual(requests.map((r) => r.meetingMode), ['translator'], 'only the first is applied');
});

test('nova: a switch another route has already made is not applied again', async () => {
    const { a, requests } = nova('normal', 'wake_phrase');
    await stream(a, [...userTurn('hey alex switch to translator mode'), ...setMode('translator')]);
    // A chat command lands while the confirmation is being spoken.
    a.meetingMode = 'translator';
    a.activationMode = 'always_active';
    await stream(a, spoken());
    assert.equal(requests.length, 0);
    assert.ok(logs.some((l) => l === '[voice-mode] action=superseded after=confirmed now=unchanged'));
});

const finalAnswer = (text: string) => [
    chunk({ contentStart: { type: 'TEXT', role: 'ASSISTANT', additionalModelFields: FINAL } }),
    chunk({ textOutput: { content: text } }),
    chunk({ contentEnd: { type: 'TEXT', stopReason: 'END_TURN' } }),
];

test('nova: replies nobody heard are kept out of the history and the transcript', async () => {
    const published: string[] = [];
    const unsubscribe = onAssistantTurn((text: string) => { published.push(text); });
    try {
        // The translation of "Alex, stop translating".
        const t = nova('translator');
        await stream(t.a, [...userTurn('alex stop translating'), ...spoken(), ...finalAnswer('Alex, deja de traducir.')]);
        // The reply to an unaddressed set_mode call, whose final text arrives
        // after its audio has ended.
        const n = nova();
        await stream(n.a, [...userTurn('we should try translator mode'), ...setMode('translator'), ...spoken(),
            ...finalAnswer('Okay, switching us to translator mode.')]);
        assert.deepEqual(published, []);
        for (const a of [t.a, n.a]) {
            assert.equal(a.conversationHistory.filter((turn: any) => turn.role === 'ASSISTANT').length, 0);
        }
        // The next answer is kept as usual.
        await stream(n.a, [...userTurn('hey alex what time is it'), ...spoken(), ...finalAnswer('It is three.')]);
        assert.deepEqual(published, ['It is three.']);
    } finally {
        unsubscribe();
    }
});

test('nova: other speech in translator mode is translated as usual', async () => {
    const { a, requests, played } = nova('translator');
    a.startNextTranslatorSession = () => {};
    await stream(a, [...userTurn('alex said we should stop translating the slides'), ...spoken()]);
    assert.equal(requests.length, 0);
    assert.deepEqual(played.map((b) => [...b]), [[1, 2]]);
});

// ---------------------------------------------------------------------------
// Leaving translator mode without the name, and the paused translator
// ---------------------------------------------------------------------------

const muteTool = () => [
    chunk({ toolUse: { toolName: 'mute', toolUseId: 'mt-1', content: '{}' } }),
    chunk({ contentEnd: { type: 'TOOL' } }),
];

test('nova: "stop translating" without the name leaves translator mode', async () => {
    const { a, requests } = nova('translator');
    await stream(a, userTurn('stop translating'));
    assert.equal(requests.length, 1);
    assert.equal(requests[0].route, 'voice');
    assert.equal(requests[0].meetingMode, 'normal');
    assert.ok(logs.some((l) => l === '[voice-mode] mode=assistant action=stop-translating route=nova'));
});

test('nova: the mute call that follows "stop translating" leaves translator mode rather than pausing', async () => {
    const { a, requests } = nova('translator');
    const tones: string[] = [];
    a.playConfirmationTone = (kind: string) => { tones.push(kind); };
    // Nova's live order: its transcript of the turn, then the mute call.
    await stream(a, [...userTurn('stop translating'), ...muteTool()]);
    assert.equal(requests.length, 1);
    assert.equal(requests[0].meetingMode, 'normal');
    assert.equal(a._translatorState, 'UNMUTED', 'not paused');
    assert.ok(logs.some((l) => l === '[voice-mode] action=mute-tool-ignored why=stop-translating'));
    // The only tone is the confirmation of leaving (no rendered clip here).
    assert.deepEqual(tones, ['mute']);
});

test('nova: a mute call just after the participants\' transcript heard "stop translating" is taken as it', async () => {
    const { a, requests } = nova('translator');
    // Nova's own transcript of the turn was wrong; Amazon Transcribe's was not,
    // and the switch it started has already been made.
    a.leaveTranslatorRequestedAt = Date.now();
    await stream(a, [...userTurn('stop translate in'), ...muteTool()]);
    assert.equal(a._translatorState, 'UNMUTED');
    assert.equal(requests.length, 1);
    assert.equal(requests[0].meetingMode, 'normal');
    assert.ok(logs.some((l) => l === '[voice-mode] action=mute-tool-taken-as-stop-translating'));
});

test('nova: a sentence that contains "stop translating" is translated, on either transcript', async () => {
    const { a, requests } = nova('translator');
    a.startNextTranslatorSession = () => {};
    a.noteParticipantUtterance('I think we should stop translating the slides after lunch.');
    await stream(a, userTurn('i think we should stop translating the slides after lunch'));
    assert.equal(requests.length, 0);
});

test('nova: "Stop translating." on the participants\' transcript leaves translator mode, once', async () => {
    const { a, requests } = nova('translator');
    a.noteParticipantUtterance('Stop translating.');
    // Nova's transcript of the same turn arrives while the switch is under way.
    await stream(a, userTurn('stop translating'));
    assert.equal(requests.length, 1);
    assert.equal(requests[0].meetingMode, 'normal');
    assert.ok(logs.some((l) => l === '[voice-mode] mode=assistant action=stop-translating route=transcript'));
});

test('nova: a wake phrase while the translator is paused leaves for assistant mode and passes the question on', async () => {
    const { a, requests } = nova('translator');
    // An explicit activation command clears the meeting's restore memory, so
    // the assistant stays always listening and the question goes as a message.
    await applyAssistantChatCommand(a, { activationMode: 'always_active' }, null);
    requests.length = 0;
    const asked: string[] = [];
    a.sendUserMessage = (text: string) => { asked.push(text); };
    a._translatorState = 'MUTED';
    a.noteParticipantUtterance('Hey Alex, did you stop translating?');
    for (let i = 0; i < 5; i++) await flush();
    assert.equal(requests.length, 1);
    assert.equal(requests[0].meetingMode, 'normal');
    assert.deepEqual(asked, ['did you stop translating']);
    assert.ok(logs.some((l) => l.endsWith('route=transcript why=wake-phrase-while-paused')));
});

test('nova: on wake-phrase activation the question is handed over as an activation', async () => {
    const { a } = nova('translator');
    const activations: Array<string | undefined> = [];
    a.activate = async (_duration: number | undefined, context?: string) => { activations.push(context); };
    a.reconfigure = async () => {
        a.meetingMode = 'normal';
        a.activationMode = 'wake_phrase';
        return { result: 'applied', changed: ['meetingMode', 'activationMode'], sessionRebuilt: true,
            meetingModeBefore: 'translator', meetingModeAfter: 'normal',
            activationModeBefore: 'always_active', activationModeAfter: 'wake_phrase' };
    };
    a._translatorState = 'MUTED';
    a.noteParticipantUtterance('Hey Alex, where did you go');
    for (let i = 0; i < 5; i++) await flush();
    assert.deepEqual(activations, ['Hey Alex, where did you go']);
});

test('nova: while paused, the wake phrase heard by Nova defers to the participants\' transcript', async () => {
    const { a, requests } = nova('translator');
    a.noteParticipantUtterance('Bonjour.');
    a._translatorState = 'MUTED';
    await stream(a, userTurn('hey alex are you there'));
    assert.equal(requests.length, 0, 'acted on once, from the transcript, not twice');
});

test('nova: "Hey Alex, stop" while paused stays paused', async () => {
    const { a, requests } = nova('translator');
    a._translatorState = 'MUTED';
    a.noteParticipantUtterance('Hey Alex, stop.');
    assert.equal(requests.length, 0);
    assert.equal(a._translatorState, 'MUTED');
});

test('nova: a paused translator is not sent the meeting once the participants\' transcript is there', () => {
    const { a } = nova('translator');
    const audioEvents = () => a.session.queue.filter((e: any) => e.event?.audioInput).length;
    a._translatorState = 'MUTED';
    a.sendAudioChunk(Buffer.alloc(4));
    assert.equal(audioEvents(), 1, 'without the transcript it still hears the resume phrase itself');
    a.noteParticipantUtterance('Bonjour.');
    a.sendAudioChunk(Buffer.alloc(4));
    assert.equal(audioEvents(), 1, 'withheld');
    a._translatorState = 'UNMUTED';
    a.sendAudioChunk(Buffer.alloc(4));
    assert.equal(audioEvents(), 2, 'translating, it hears the meeting again');
});

test('nova: the resume phrase on the participants\' transcript resumes a paused translator', async () => {
    const { a, requests } = nova('translator');
    const tones: string[] = [];
    let refreshes = 0;
    a.playConfirmationTone = (kind: string) => { tones.push(kind); };
    a.refreshSession = async () => { refreshes += 1; };
    a._translatorState = 'MUTED';
    a._isMuted = true;
    a.noteParticipantUtterance('Translator, unmute.');
    assert.equal(a._translatorState, 'UNMUTED');
    assert.equal(a._isMuted, false);
    assert.equal(refreshes, 1);
    assert.deepEqual(tones, ['unmute']);
    assert.equal(requests.length, 0);
    // The paused session's own unmute call, arriving after, changes nothing.
    await stream(a, [chunk({ toolUse: { toolName: 'unmute', toolUseId: 'u-1', content: '{}' } }),
        chunk({ contentEnd: { type: 'TOOL' } })]);
    assert.equal(refreshes, 1);
});
