/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Being able to tell the voice assistant to stop talking, in every meeting
 * mode, and being able to get it back afterwards.
 *
 * Three things have to hold together for "Hey Alex, stop talking" to work:
 * the `mute` tool has to exist in the mode the deployment is actually running,
 * the system prompt has to frame a stop request as a request for silence rather
 * than as the preface to a new request, and the next wake phrase has to make
 * the assistant audible again. Each is asserted below, along with the group and
 * translator state machines that must not change.
 *
 * What is NOT covered here: whether the model obeys the prompt, and whether
 * PulseAudio actually falls silent. Both need a live meeting.
 */
import { strict as assert } from 'node:assert';
import { test } from 'node:test';
import { NovaAgent, hasStopTalkingTool, STOP_REQUEST_TOOL_GRACE_MS } from './nova-agent.js';
import { ElevenLabsAgent } from './elevenlabs-agent.js';
import { NoOpAgent } from './noop-agent.js';
import { MeetingMode } from './nova-sonic-config-loader.js';

/**
 * Wait out the window the transcript route gives Nova to answer the request out
 * loud before silencing the assistant itself. The handover between the two
 * routes is covered in voice-assistant-stop-race.test.ts; here it is only
 * something to wait for.
 */
function afterStopWindow(): Promise<void> {
    return new Promise((resolve) => setTimeout(resolve, STOP_REQUEST_TOOL_GRACE_MS + 150));
}

/** A Nova agent with no session and no Bedrock client — enough to inspect the
 *  prompt, the tool set and the mute state machine without any network. */
function novaAgent(overrides: {
    meetingMode?: MeetingMode;
    activationMode?: string;
    strandsLambdaArn?: string;
} = {}): NovaAgent {
    return new NovaAgent({
        modelId: 'amazon.nova-2-sonic-v1:0',
        systemPrompt: 'You are Alex, an AI meeting assistant.',
        activationMode: overrides.activationMode || 'wake_phrase',
        meetingMode: overrides.meetingMode || 'normal',
        strandsLambdaArn: overrides.strandsLambdaArn,
    });
}

function toolNames(agent: NovaAgent): string[] {
    return (agent as any).buildToolSpecs().map((tool: any) => tool.toolSpec.name);
}

function systemPrompt(agent: NovaAgent): string {
    return (agent as any).buildSystemPromptContent();
}

// ---------------------------------------------------------------------------
// The tool exists in the mode people actually run
// ---------------------------------------------------------------------------

test('the stop-talking tool is registered in both conversational meeting modes', () => {
    // A deployment on wake_phrase activation runs meetingMode 'normal', which is
    // where most people meet the assistant, so that is the mode that most needs
    // a deterministic way to be told to stop.
    assert.equal(hasStopTalkingTool('normal'), true);
    assert.equal(hasStopTalkingTool('group'), true);
    assert.equal(hasStopTalkingTool('translator'), false);

    assert.deepEqual(toolNames(novaAgent({ meetingMode: 'normal' })), ['mute']);
    assert.deepEqual(toolNames(novaAgent({ meetingMode: 'group' })), ['mute']);
});

test('the stop-talking tool is registered alongside the meeting-assistant tool', () => {
    // Registering one tool must not displace the other.
    assert.deepEqual(
        toolNames(novaAgent({ meetingMode: 'normal', strandsLambdaArn: 'arn:aws:lambda:us-east-1:123456789012:function:strands' })),
        ['strands_agent', 'mute'],
    );
    assert.deepEqual(
        toolNames(novaAgent({ meetingMode: 'group', strandsLambdaArn: 'arn:aws:lambda:us-east-1:123456789012:function:strands' })),
        ['strands_agent', 'mute'],
    );
});

test('translator mode keeps exactly one trigger-phrase tool per state', () => {
    // Translator mute/unmute is a different tool with different semantics.
    // Exposing only the tool for the current state is what stops the model
    // from calling the opposite transition, and no meeting-assistant tool is
    // offered at all.
    const agent = novaAgent({
        meetingMode: 'translator',
        activationMode: 'always_active',
        strandsLambdaArn: 'arn:aws:lambda:us-east-1:123456789012:function:strands',
    });
    assert.deepEqual(toolNames(agent), ['mute']);

    (agent as any)._translatorState = 'MUTED';
    assert.deepEqual(toolNames(agent), ['unmute']);
});

// ---------------------------------------------------------------------------
// The prompt asks for one word, then silence
// ---------------------------------------------------------------------------

test('the system prompt asks for one short word and the tool, not a continuation', () => {
    for (const mode of ['normal', 'group'] as MeetingMode[]) {
        const prompt = systemPrompt(novaAgent({ meetingMode: mode }));
        assert.match(prompt, /STOP REQUESTS:/, `${mode} prompt must carry stop instructions`);
        assert.match(prompt, /exactly one short word/, `${mode} prompt must ask for one word`);
        assert.match(prompt, /Call the `mute` tool/, `${mode} prompt must ask for the tool`);
        assert.match(prompt, /request for silence, not the start of a new request/, `${mode} prompt must frame it as silence`);
        // The old unconditional instruction turned every interruption,
        // including a request to be quiet, into a preface for something else.
        assert.doesNotMatch(
            prompt,
            /THEN proceed with their new request/,
            `${mode} prompt must not tell the model to continue after a stop request`,
        );
    }
});

test('the prompt and the tool both require the stop to be addressed to the assistant', () => {
    // The assistant hears the whole meeting, so most "be quiet" in a transcript
    // is one participant talking to another. Acting on that takes the assistant
    // out of the meeting, and on an always_active deployment only a wake phrase
    // brings it back — so the limit has to be stated in the prompt and on the
    // tool, not just in the transcript matcher.
    for (const mode of ['normal', 'group'] as MeetingMode[]) {
        const agent = novaAgent({ meetingMode: mode, activationMode: 'always_active' });
        const prompt = systemPrompt(agent);
        assert.match(prompt, /addressed to YOU/, `${mode} prompt must require being addressed`);
        assert.match(prompt, /telling EACH OTHER to be quiet/, `${mode} prompt must exclude crossfire`);

        const mute = (agent as any).buildToolSpecs().find((t: any) => t.toolSpec.name === 'mute');
        assert.match(
            mute.toolSpec.description,
            /must be addressed to you/,
            `${mode} mute tool description must require being addressed`,
        );
    }
});

test('the fast acknowledgement for ordinary interruptions is kept', () => {
    // Barge-in on a normal correction or a new question should still get an
    // instant "Got it" — only the stop case changes.
    const prompt = systemPrompt(novaAgent({ meetingMode: 'normal' }));
    assert.match(prompt, /INTERRUPTION HANDLING:/);
    assert.match(prompt, /Keep it to 1-2 words for minimal interruption/);
    assert.match(prompt, /Otherwise, proceed with their new request/);
});

test('the translator prompt is replaced wholesale and carries no stop instructions', () => {
    // Translator mode must stay a pure interpreter: no interruption handling,
    // no stop handling, nothing but the trigger phrase.
    const prompt = systemPrompt(novaAgent({ meetingMode: 'translator', activationMode: 'always_active' }));
    assert.match(prompt, /TRANSLATOR MODE:/);
    assert.match(prompt, /PAUSE TRIGGER/);
    assert.doesNotMatch(prompt, /STOP REQUESTS/);
    assert.doesNotMatch(prompt, /INTERRUPTION HANDLING/);
});

test('group mode keeps its own passive-listening instructions', () => {
    const prompt = systemPrompt(novaAgent({ meetingMode: 'group', activationMode: 'always_active' }));
    assert.match(prompt, /GROUP MEETING MODE:/);
    assert.match(prompt, /you will only be heard when someone addresses you by name/);
});

// ---------------------------------------------------------------------------
// The mute state machine
// ---------------------------------------------------------------------------

test('each meeting mode starts in its own audible state', () => {
    // Group is the only mode that starts muted. Nothing here may change for a
    // deployment that has not asked for the stop feature.
    assert.equal(novaAgent({ meetingMode: 'normal' }).isMuted(), false);
    assert.equal(novaAgent({ meetingMode: 'group', activationMode: 'always_active' }).isMuted(), true);
    assert.equal(novaAgent({ meetingMode: 'translator', activationMode: 'always_active' }).isMuted(), false);
});

test('a stop request silences the assistant in normal mode', async () => {
    const agent = novaAgent({ meetingMode: 'normal' });
    assert.equal(agent.requestStopTalking('test'), true);
    await afterStopWindow();
    assert.equal(agent.isMuted(), true);
});

test('a stop request silences the assistant in group mode', async () => {
    const agent = novaAgent({ meetingMode: 'group', activationMode: 'always_active' });
    (agent as any)._isMuted = false; // as if a wake phrase had made it audible
    assert.equal(agent.requestStopTalking('test'), true);
    await afterStopWindow();
    assert.equal(agent.isMuted(), true);
});

test('translator mode takes stop requests only from its own pause phrase', () => {
    // Muting the translator from the transcript route would leave its state
    // machine saying UNMUTED with only the `mute` tool registered, so the
    // configured resume phrase would have no tool to call and the translator
    // would never speak again.
    const agent = novaAgent({ meetingMode: 'translator', activationMode: 'always_active' });
    assert.equal(agent.requestStopTalking('test'), false);
    assert.equal(agent.isMuted(), false);
    assert.equal((agent as any)._translatorState, 'UNMUTED');
    assert.deepEqual(toolNames(agent), ['mute']);
    // A decline must leave nothing armed either: see
    // voice-assistant-stop-race.test.ts for the deferral it is declining.
    assert.equal((agent as any).stopRequest, null);
});

test('only translator mode sounds a cue when the mute tool fires', async () => {
    // The conversational modes answer "Okay" out loud, and the transcript route
    // in scribe.ts silences the assistant without a sound, so a tone on the
    // tool path would make one request land two different ways depending on
    // which route reached the audio gate first. Translator mode replaces the
    // base prompt and so says nothing, leaving the tone its only confirmation.
    for (const mode of ['normal', 'group'] as MeetingMode[]) {
        const agent = novaAgent({ meetingMode: mode, activationMode: 'always_active' });
        (agent as any)._isMuted = false; // group starts muted; exercise the transition
        const tones: string[] = [];
        (agent as any).playConfirmationTone = (kind: string) => tones.push(kind);
        await (agent as any).handleMuteTool('tool-use-id');
        assert.deepEqual(tones, [], `${mode}: being asked to stop must simply go quiet`);
        assert.equal(agent.isMuted(), true, `${mode}: the audio gate must still engage`);
    }

    const translator = novaAgent({ meetingMode: 'translator', activationMode: 'always_active' });
    const translatorTones: string[] = [];
    (translator as any).playConfirmationTone = (kind: string) => translatorTones.push(kind);
    (translator as any).refreshSession = async () => {};
    await (translator as any).handleMuteTool('tool-use-id');
    assert.deepEqual(translatorTones, ['mute'], 'the translator pause cue is audible');
    assert.equal(translator.isMuted(), true);
});

test('a disabled voice assistant has nothing to silence', () => {
    assert.equal(new NoOpAgent().requestStopTalking('test'), false);
    // No API key means the ElevenLabs provider is not running at all.
    assert.equal(new ElevenLabsAgent({ apiKey: '' }).requestStopTalking('test'), false);
});

// ---------------------------------------------------------------------------
// Getting the assistant back
// ---------------------------------------------------------------------------

test('being addressed again makes the assistant audible after a stop request', async () => {
    // Without this a single "stop talking" would silence the assistant for the
    // rest of the meeting.
    const agent = novaAgent({ meetingMode: 'normal', activationMode: 'wake_phrase' });
    agent.requestStopTalking('test');
    await afterStopWindow();
    assert.equal(agent.isMuted(), true);

    // Stand in for an established session so activate() does not reach Bedrock.
    (agent as any)._isActive = true;
    await agent.activate(1);
    assert.equal(agent.isMuted(), false, 'the next wake phrase must make it audible again');
    agent.deactivate();
});

test('a wake phrase in the user transcript makes it audible again in normal mode', async () => {
    // The always_active path: activate() returns early there, so the FINAL user
    // transcript is the only route back.
    const agent = novaAgent({ meetingMode: 'normal', activationMode: 'always_active' });
    agent.requestStopTalking('test');
    await afterStopWindow();
    (agent as any).handleUserTurnWakePhrase('Hey Alex, what did we decide about the launch date?');
    assert.equal(agent.isMuted(), false);
});

test('the utterance that asked for silence does not itself undo it', async () => {
    // "Hey Alex, stop talking" contains a wake phrase, and the mute it causes
    // can land either side of this transcript turn.
    for (const mode of ['normal', 'group'] as MeetingMode[]) {
        const agent = novaAgent({ meetingMode: mode, activationMode: 'always_active' });
        agent.requestStopTalking('test');
        await afterStopWindow();
        assert.equal(agent.isMuted(), true);
        (agent as any).handleUserTurnWakePhrase('Hey Alex, stop talking');
        assert.equal(agent.isMuted(), true, `${mode}: a stop request must not unmute`);
        (agent as any).clearConversationTimeout();
    }
});

test('group mode still unmutes on an ordinary wake phrase', () => {
    // The primary group-mode trigger, unchanged: it starts muted and the wake
    // phrase is what makes it audible.
    const agent = novaAgent({ meetingMode: 'group', activationMode: 'always_active' });
    assert.equal(agent.isMuted(), true);
    (agent as any).handleUserTurnWakePhrase('Hey Alex, can you summarise the last ten minutes?');
    assert.equal(agent.isMuted(), false);
    (agent as any).clearConversationTimeout();
});

test('an utterance with no wake phrase leaves the mute state alone', async () => {
    const agent = novaAgent({ meetingMode: 'group', activationMode: 'always_active' });
    (agent as any).handleUserTurnWakePhrase('so the next thing on the agenda is hiring');
    assert.equal(agent.isMuted(), true, 'group mode must stay muted until addressed');

    const normal = novaAgent({ meetingMode: 'normal' });
    normal.requestStopTalking('test');
    await afterStopWindow();
    (normal as any).handleUserTurnWakePhrase('anyway, back to the roadmap');
    assert.equal(normal.isMuted(), true, 'normal mode must stay silent until addressed');
});

// ---------------------------------------------------------------------------
// ElevenLabs provider
// ---------------------------------------------------------------------------

test('the ElevenLabs provider silences itself on a stop request', () => {
    // LMA cannot register a tool or amend the prompt on this provider — the
    // agent's persona lives in the ElevenLabs dashboard — so the transcript
    // route is the whole mechanism there.
    const agent = new ElevenLabsAgent({ apiKey: 'test-key', meetingMode: 'normal' });
    assert.equal(agent.isMuted(), false);
    assert.equal(agent.requestStopTalking('test'), true);
    assert.equal(agent.isMuted(), true);
});

test('the ElevenLabs provider declines stop requests in translator mode', () => {
    const agent = new ElevenLabsAgent({ apiKey: 'test-key', meetingMode: 'translator' });
    assert.equal(agent.requestStopTalking('test'), false);
    assert.equal(agent.isMuted(), false);
});

test('the ElevenLabs provider reports both outcomes in the shared log family', () => {
    // One CloudWatch filter is meant to cover both providers, which only holds
    // if this one writes a line for the outcomes where nothing is muted too.
    const captured: string[] = [];
    const realLog = console.log;
    console.log = (...args: unknown[]) => {
        captured.push(args.map((arg) => String(arg)).join(' '));
    };
    try {
        new ElevenLabsAgent({ apiKey: 'test-key', meetingMode: 'normal' }).requestStopTalking('test');
        new ElevenLabsAgent({ apiKey: 'test-key', meetingMode: 'translator' }).requestStopTalking('test');
    } finally {
        console.log = realLog;
    }

    const lines = captured.filter((line) => line.startsWith('[stop-request]'));
    assert.equal(lines.length, 2, `both outcomes must be reported, saw: ${lines.join(' | ')}`);
    assert.match(lines[0], /route=transcript action=muted-immediately provider=elevenlabs mode=normal/);
    assert.match(lines[0], /speaking=\w+ muted=\w+/);
    assert.match(lines[1], /route=transcript action=declined-translator provider=elevenlabs mode=translator/);
    // The utterance is participant speech and must not reach the log.
    for (const line of lines) {
        assert.doesNotMatch(line, /stop talking|Hey Alex/i, `instrumentation leaked speech: ${line}`);
    }
});

test('the ElevenLabs provider comes back when addressed again', async () => {
    const agent = new ElevenLabsAgent({ apiKey: 'test-key', meetingMode: 'normal', activationMode: 'always_active' });
    agent.requestStopTalking('test');
    assert.equal(agent.isMuted(), true);
    await (agent as any).handleWebSocketMessage({
        type: 'user_transcript',
        user_transcription_event: { user_transcript: 'Hey Alex, where were we?' },
    });
    assert.equal(agent.isMuted(), false);
});

test('the ElevenLabs stop utterance does not itself undo the silence', async () => {
    for (const mode of ['normal', 'group'] as MeetingMode[]) {
        const agent = new ElevenLabsAgent({ apiKey: 'test-key', meetingMode: mode, activationMode: 'always_active' });
        agent.requestStopTalking('test');
        await (agent as any).handleWebSocketMessage({
            type: 'user_transcript',
            user_transcription_event: { user_transcript: 'Hey Alex, stop talking' },
        });
        assert.equal(agent.isMuted(), true, `${mode}: a stop request must not unmute`);
        (agent as any).clearConversationTimeout();
    }
});

test('the ElevenLabs group mode still unmutes on an ordinary wake phrase', async () => {
    const agent = new ElevenLabsAgent({ apiKey: 'test-key', meetingMode: 'group', activationMode: 'always_active' });
    assert.equal(agent.isMuted(), true);
    await (agent as any).handleWebSocketMessage({
        type: 'user_transcript',
        user_transcription_event: { user_transcript: 'Hey Alex, any action items so far?' },
    });
    assert.equal(agent.isMuted(), false);
    (agent as any).clearConversationTimeout();
});
