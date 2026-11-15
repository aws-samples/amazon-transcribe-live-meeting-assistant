/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * "LMA mute" and "LMA unmute" in the meeting chat, and the translator pausing
 * only when its pause phrase was said.
 *
 * On a live Zoom meeting "LMA mute", "LMA resume" and "LMA unmute" were typed
 * and none of them was a command: each was added to the transcript as meeting
 * chat, nothing was posted back, and the assistant's state did not change.
 * These tests hold the commands to being recognised, applied in code, and
 * answered in chat on every route.
 *
 * What is NOT covered here: each platform's chat scraper delivering the text,
 * which chat-commands.test.ts covers as far as it is node-side.
 */
import { strict as assert } from 'node:assert';
import { afterEach, beforeEach, mock, test } from 'node:test';
import {
    applyAssistantChatCommand,
    createActivationRestore,
    parseAssistantChatCommand,
    voiceControlChatReply,
} from './assistant-toggles.js';
import { matchesEndCommand } from './details.js';
import { NovaAgent } from './nova-agent.js';
import { ElevenLabsAgent } from './elevenlabs-agent.js';
import { NoOpAgent } from './noop-agent.js';
import { MeetingMode } from './nova-sonic-config-loader.js';
import { routeLogsToConsole } from './logger.js';

routeLogsToConsole();

let logs: string[] = [];
beforeEach(() => {
    logs = [];
    mock.method(console, 'log', (...args: unknown[]) => { logs.push(args.map(String).join(' ')); });
});
afterEach(() => mock.restoreAll());

function nova(meetingMode: MeetingMode = 'normal', activationMode = 'wake_phrase'): any {
    const agent = new NovaAgent({
        modelId: 'amazon.nova-2-sonic-v1:0',
        systemPrompt: 'You are Alex, an AI meeting assistant.',
        activationMode: activationMode as any,
        meetingMode,
    });
    const a = agent as any;
    a.playConfirmationTone = () => {};
    a.refreshSession = async () => {};
    return a;
}

async function typed(assistant: any, message: string): Promise<string[]> {
    const command = parseAssistantChatCommand(message);
    assert.ok(command, `"${message}" is not a command`);
    return applyAssistantChatCommand(assistant, command, 'Bob', createActivationRestore());
}

// ---------------------------------------------------------------------------
// The vocabulary
// ---------------------------------------------------------------------------

test('"LMA mute" and its phrasings are the mute command', () => {
    for (const message of ['LMA mute', 'lma MUTE', '@LMA, mute!', 'LMA quiet', 'LMA be quiet', 'LMA stop talking', 'mute LMA']) {
        assert.deepEqual(parseAssistantChatCommand(message), { voice: 'mute' }, message);
    }
});

test('"LMA unmute" and "LMA resume" are the unmute command', () => {
    for (const message of ['LMA unmute', 'LMA resume', 'lma Resume.', 'LMA speak']) {
        assert.deepEqual(parseAssistantChatCommand(message), { voice: 'unmute' }, message);
    }
});

test('"LMA stop" still removes the VP, and is not a mute', () => {
    assert.equal(matchesEndCommand('LMA stop'), true);
    assert.equal(parseAssistantChatCommand('LMA stop'), null);
    assert.equal(matchesEndCommand('LMA stop talking'), false);
});

test('a message that only mentions muting is meeting chat', () => {
    for (const message of ['mute', 'LMA mute the slides', 'can LMA mute itself', 'please unmute Bob']) {
        assert.equal(parseAssistantChatCommand(message)?.voice, undefined, message);
    }
});

// ---------------------------------------------------------------------------
// Assistant mode
// ---------------------------------------------------------------------------

test('"LMA mute" silences the assistant at once, ends its activation, and says so in chat', async () => {
    const a = nova();
    a._isActivated = true;
    const reply = await typed(a, 'LMA mute');
    assert.equal(a.isMuted(), true);
    assert.equal(a.isActivated(), false, 'the activation is still listening');
    assert.equal(reply.length, 1);
    assert.match(reply[0], /^Live Meeting Assistant: voice assistant muted\./);
    assert.match(reply[0], /"LMA unmute"/);
    assert.ok(logs.some((l) => l.includes('[voice-control]') && l.includes('action=mute result=applied state=muted')));
});

test('a wake phrase does not bring back an assistant muted from chat', async () => {
    const a = nova();
    await typed(a, 'LMA mute');
    let opened = false;
    a.openAssistantSession = async () => { opened = true; return true; };
    await a.activate(undefined, 'hey alex what is next');
    assert.equal(opened, false, 'a session was opened for the wake phrase');
    assert.equal(a.isActivated(), false);
    assert.equal(a.isMuted(), true);
    // Nor does one heard on an open session.
    a.handleUserTurnWakePhrase('hey alex are you there');
    assert.equal(a.isMuted(), true);
});

test('"LMA unmute" brings it back and says how to address it', async () => {
    const a = nova();
    await typed(a, 'LMA mute');
    const reply = await typed(a, 'LMA unmute');
    assert.equal(a.isMuted(), false);
    assert.match(reply[0], /voice assistant unmuted\. Say "Hey Alex" to ask it something\./);
    // And the next wake phrase activates it again.
    a.openAssistantSession = async () => true;
    await a.activate(undefined, 'hey alex what is next');
    assert.equal(a.isActivated(), true);
    a.deactivate();
});

test('"LMA resume" after "LMA mute" unmutes, as it was typed for', async () => {
    const a = nova();
    await typed(a, 'LMA mute');
    await typed(a, 'LMA resume');
    assert.equal(a.isMuted(), false);
});

test('"LMA unmute" also clears a spoken stop', async () => {
    const a = nova();
    a.mute('stop requested in transcript');
    const reply = await typed(a, 'LMA unmute');
    assert.equal(a.isMuted(), false);
    assert.match(reply[0], /voice assistant unmuted/);
});

test('a repeated mute or an unneeded unmute is answered, not ignored', async () => {
    const a = nova();
    assert.match((await typed(a, 'LMA unmute'))[0], /the voice assistant is not muted/);
    await typed(a, 'LMA mute');
    assert.match((await typed(a, 'LMA mute'))[0], /the voice assistant is already muted/);
});

test('the unmute reply points at "LMA start" while the transcript is paused', () => {
    const [reply] = voiceControlChatReply('unmute', { result: 'applied', state: 'listening' }, true);
    assert.match(reply, /The transcript is paused; type "LMA start" to resume it\./);
    const [unpaused] = voiceControlChatReply('unmute', { result: 'applied', state: 'listening' }, false);
    assert.doesNotMatch(unpaused, /transcript/);
});

test('a mode change lifts a mute from chat', async () => {
    const a = nova();
    await typed(a, 'LMA mute');
    a.applyMeetingMode('normal');
    assert.equal(a.chatMuted, false);
});

test('a meeting with no voice assistant is told so', async () => {
    const reply = await typed(new NoOpAgent(), 'LMA mute');
    assert.match(reply[0], /there is no voice assistant in this meeting to mute/);
});

test('the ElevenLabs assistant is muted from chat the same way', async () => {
    const agent = new ElevenLabsAgent({ apiKey: 'key', activationMode: 'wake_phrase' } as any);
    const a = agent as any;
    a._isActivated = true;
    // Without a socket to close.
    a.deactivate = () => { a._isActivated = false; };
    await typed(a, 'LMA mute');
    assert.equal(agent.isMuted(), true);
    assert.equal(a._isActivated, false);
    await typed(a, 'LMA unmute');
    assert.equal(agent.isMuted(), false);
});

// ---------------------------------------------------------------------------
// Translator mode
// ---------------------------------------------------------------------------

test('in translator mode "LMA mute" pauses the translator and "LMA unmute" resumes it', async () => {
    const a = nova('translator', 'always_active');
    const paused = await typed(a, 'LMA mute');
    assert.equal(a._translatorState, 'MUTED');
    assert.match(paused[0], /translation paused\. Type "LMA unmute" to resume it\./);
    const resumed = await typed(a, 'LMA unmute');
    assert.equal(a._translatorState, 'UNMUTED');
    assert.match(resumed[0], /translation resumed\./);
});

test("the translator's mute tool is not acted on unless the pause phrase was said", async () => {
    // As on the live meeting: the model called `mute` on hearing this.
    const a = nova('translator', 'always_active');
    a.MUTE_TOOL_AUDIO_FLUSH_MS = 0;
    a.handleTranslatorUtterance('all right so i just turned on the translator mode just by typing lma translate', null, 'nova');
    await a.handleMuteTool('tool-1');
    assert.equal(a._translatorState, 'UNMUTED');
    assert.equal(a.isMuted(), false);
    assert.ok(logs.some((l) => l.includes('[translator] action=mute-tool-ignored why=no-pause-phrase')));
});

test("the translator's mute tool pauses it when the pause phrase was said", async () => {
    const a = nova('translator', 'always_active');
    a.MUTE_TOOL_AUDIO_FLUSH_MS = 0;
    a.handleTranslatorUtterance('translator mute', null, 'nova');
    await a.handleMuteTool('tool-1');
    assert.equal(a._translatorState, 'MUTED');
});

test("the pause phrase on the participants' transcript pauses the translator without the model", () => {
    const a = nova('translator', 'always_active');
    a.noteParticipantUtterance('Translator, mute.');
    assert.equal(a._translatorState, 'MUTED');
    assert.equal(a.isMuted(), true);
    assert.ok(logs.some((l) => l.includes('[translator] action=pause route=transcript')));
});
