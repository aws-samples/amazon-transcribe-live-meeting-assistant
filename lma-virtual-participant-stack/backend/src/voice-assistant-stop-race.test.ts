/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Which of the two stop-request routes silences the assistant, and what the
 * meeting hears while that is decided.
 *
 * Both routes recognise "Hey Alex, stop talking" and only one of them can
 * speak. The tool route is the model noticing, so it is the only route that can
 * produce the spoken "Okay"; the transcript route is the faster one and the one
 * that guarantees silence. These tests pin the handover between them: the
 * transcript route holds its mute for STOP_REQUEST_TOOL_GRACE_MS, stands down
 * if the tool call lands inside that window, and acts if it does not. They also
 * pin the `[stop-request]` log lines, because those lines are the only account
 * a live meeting leaves of which route acted and why: renaming one without
 * noticing would leave a live test with nothing to read but the silence it was
 * sent to explain.
 *
 * What is NOT covered here: whether Nova calls the tool at all, how long it
 * takes in a real meeting, and whether PulseAudio actually carries the word.
 * All three need a live meeting.
 */
import { strict as assert } from 'node:assert';
import { test } from 'node:test';
import {
    NovaAgent,
    STOP_REQUEST_TOOL_GRACE_MS,
    STOP_REQUEST_TOOL_BACKSTOP_MS,
} from './nova-agent.js';
import { MeetingMode } from './nova-sonic-config-loader.js';

/** A Nova agent with no session and no Bedrock client, with playback stubbed
 *  out so nothing tries to spawn pacat. */
function novaAgent(overrides: { meetingMode?: MeetingMode; activationMode?: string } = {}): NovaAgent {
    const agent = new NovaAgent({
        modelId: 'amazon.nova-2-sonic-v1:0',
        systemPrompt: 'You are Alex, an AI meeting assistant.',
        activationMode: overrides.activationMode || 'wake_phrase',
        meetingMode: overrides.meetingMode || 'normal',
    });
    (agent as any).stopPacatStream = (mode: string = 'immediate') => {
        playbackTeardowns.push(mode);
    };
    return agent;
}

let playbackTeardowns: string[] = [];
let captured: string[] = [];
const realLog = console.log;

/** Start collecting console output. node's test runner gives each file its own
 *  process and runs the tests in it one at a time, so this is not shared. */
function capture(): void {
    captured = [];
    playbackTeardowns = [];
    console.log = (...args: unknown[]) => {
        captured.push(args.map((arg) => String(arg)).join(' '));
    };
}

function release(): void {
    console.log = realLog;
}

/** Just the instrumentation lines, which are what CloudWatch is filtered on. */
function stopLines(): string[] {
    return captured.filter((line) => line.startsWith('[stop-request]'));
}

/** Every line produced by engaging the audio gate, however it was engaged. */
function gateEngagements(): string[] {
    return captured.filter((line) => line.includes('Nova agent muted'));
}

function delay(ms: number): Promise<void> {
    return new Promise((resolve) => setTimeout(resolve, ms));
}

/** Long enough for the transcript route's fallback to have fired. */
const PAST_GRACE_MS = STOP_REQUEST_TOOL_GRACE_MS + 150;
/** Long enough for the tool route's backstop to have fired as well. */
const PAST_BACKSTOP_MS = STOP_REQUEST_TOOL_BACKSTOP_MS + 150;

// ---------------------------------------------------------------------------
// The window itself
// ---------------------------------------------------------------------------

test('the transcript route holds its mute for the window the constant names', async () => {
    capture();
    try {
        assert.equal(STOP_REQUEST_TOOL_GRACE_MS, 500);

        const agent = novaAgent();
        const startedAt = Date.now();
        assert.equal(agent.requestStopTalking('test'), true);

        // The request is accepted, but the gate is deliberately still open: this
        // is the room the model needs to say "Okay" out loud.
        assert.equal(agent.isMuted(), false, 'the window must not be skipped');
        await delay(STOP_REQUEST_TOOL_GRACE_MS / 2);
        assert.equal(agent.isMuted(), false, 'the window must not be cut short');

        await delay(PAST_GRACE_MS - STOP_REQUEST_TOOL_GRACE_MS / 2);
        assert.equal(agent.isMuted(), true);
        assert.ok(
            Date.now() - startedAt >= STOP_REQUEST_TOOL_GRACE_MS,
            'silence must not arrive before the window has elapsed',
        );

        const lines = stopLines().join('\n');
        assert.match(lines, /route=transcript action=deferred[^\n]*window_ms=500/);
        assert.match(lines, /route=transcript action=fired-fallback[^\n]*tool_seen=false/);
    } finally {
        release();
    }
});

test('the assistant is silenced even when no tool call ever arrives', async () => {
    // The fallback is not optional. Being asked for quiet and ignored is a
    // worse outcome than being answered without the courtesy word.
    capture();
    try {
        for (const mode of ['normal', 'group'] as MeetingMode[]) {
            const agent = novaAgent({ meetingMode: mode, activationMode: 'always_active' });
            (agent as any)._isMuted = false; // group starts muted; exercise the transition
            agent.requestStopTalking('test');
            await delay(PAST_GRACE_MS);
            assert.equal(agent.isMuted(), true, `${mode}: a stop request must end in silence`);
            (agent as any).clearConversationTimeout();
        }
    } finally {
        release();
    }
});

test('the request is reported with the flags a live meeting is diagnosed from', async () => {
    capture();
    try {
        const agent = novaAgent();
        (agent as any)._isSpeaking = true; // mid-answer, which is when this happens
        agent.requestStopTalking('test');
        await delay(PAST_GRACE_MS);

        const deferred = stopLines().find((line) => line.includes('action=deferred'));
        assert.ok(deferred, 'the deferral must be reported');
        assert.match(deferred!, /speaking=true/, 'whether the agent was talking must be recorded');
        assert.match(deferred!, /interrupted=false/, 'the barge-in gate state must be recorded');
        assert.match(deferred!, /mode=normal/);
        // The utterance is participant speech and must not reach the log.
        for (const line of stopLines()) {
            assert.doesNotMatch(line, /stop talking|Hey Alex/i, `instrumentation leaked speech: ${line}`);
        }
    } finally {
        release();
    }
});

// ---------------------------------------------------------------------------
// Handover to the tool route
// ---------------------------------------------------------------------------

test('the transcript route stands down when the tool call arrives inside the window', async () => {
    capture();
    try {
        const agent = novaAgent();
        agent.requestStopTalking('test');
        assert.equal(agent.isMuted(), false);

        (agent as any).noteMuteToolRequested();
        await (agent as any).handleMuteTool('tool-use-id');
        assert.equal(agent.isMuted(), true, 'the tool route silences it');

        await delay(PAST_BACKSTOP_MS);
        const lines = stopLines().join('\n');
        assert.match(lines, /route=tool action=claimed[^\n]*transcript_deferred=true/);
        assert.match(lines, /route=tool action=claimed[^\n]*tool_latency_ms=\d+/);
        assert.match(lines, /route=transcript action=stood-down[^\n]*tool_seen=true/);
        assert.doesNotMatch(lines, /action=fired-fallback/);
    } finally {
        release();
    }
});

test('both routes firing engages the audio gate exactly once', async () => {
    capture();
    try {
        const agent = novaAgent();
        agent.requestStopTalking('test');
        (agent as any).noteMuteToolRequested();
        await (agent as any).handleMuteTool('tool-use-id');
        await delay(PAST_BACKSTOP_MS);

        assert.equal(
            gateEngagements().length,
            1,
            `the gate must engage once, saw: ${gateEngagements().join(' | ')}`,
        );
        assert.equal(agent.isMuted(), true);
    } finally {
        release();
    }
});

test('a tool call that arrives too late does not silence the assistant twice', async () => {
    capture();
    try {
        const agent = novaAgent();
        agent.requestStopTalking('test');
        await delay(PAST_GRACE_MS);
        assert.equal(agent.isMuted(), true, 'the fallback has already acted');

        (agent as any).noteMuteToolRequested();
        await (agent as any).handleMuteTool('tool-use-id');
        await delay(PAST_BACKSTOP_MS);

        assert.equal(
            gateEngagements().length,
            1,
            `the gate must engage once, saw: ${gateEngagements().join(' | ')}`,
        );
        assert.equal(agent.isMuted(), true);
    } finally {
        release();
    }
});

test('a tool call whose handler never runs still ends in silence', async () => {
    // The tool-use event and the handler that acts on it are two steps, and the
    // bidirectional stream can close in between. Handing the request over must
    // not mean dropping the guarantee, so the fallback is extended rather than
    // cancelled.
    capture();
    try {
        const agent = novaAgent();
        agent.requestStopTalking('test');
        (agent as any).noteMuteToolRequested();

        await delay(PAST_GRACE_MS);
        assert.equal(agent.isMuted(), false, 'the tool route owns the request inside its own window');

        await delay(STOP_REQUEST_TOOL_BACKSTOP_MS);
        assert.equal(agent.isMuted(), true);
        assert.match(stopLines().join('\n'), /action=fired-fallback[^\n]*tool_seen=true/);
    } finally {
        release();
    }
});

// ---------------------------------------------------------------------------
// Repetition and idempotency
// ---------------------------------------------------------------------------

test('repeating the request inside the window neither stacks timers nor moves the deadline', async () => {
    capture();
    try {
        const agent = novaAgent();
        agent.requestStopTalking('first');
        await delay(STOP_REQUEST_TOOL_GRACE_MS * 0.6);

        assert.equal(agent.requestStopTalking('second'), true);
        assert.match(stopLines().join('\n'), /action=already-deferred/);

        // The first request's deadline still governs. Were the repeat to arm its
        // own timer, or push the deadline out, the assistant would still be
        // audible here.
        await delay(STOP_REQUEST_TOOL_GRACE_MS * 0.6);
        assert.equal(agent.isMuted(), true);

        await delay(PAST_GRACE_MS);
        assert.equal(
            stopLines().filter((line) => line.includes('action=fired-fallback')).length,
            1,
            'only one fallback may fire for one request',
        );
    } finally {
        release();
    }
});

test('a stop request for an assistant that is already silent defers nothing', () => {
    capture();
    try {
        // Group mode starts muted, so this is the ordinary way to reach it.
        const agent = novaAgent({ meetingMode: 'group', activationMode: 'always_active' });
        assert.equal(agent.isMuted(), true);
        assert.equal(agent.requestStopTalking('test'), true);
        assert.equal(agent.isMuted(), true);
        assert.match(stopLines().join('\n'), /action=already-silent/);
        assert.equal(
            (agent as any).stopRequest,
            null,
            'there is no acknowledgement to wait for when the gate is already shut',
        );
    } finally {
        release();
    }
});

test('being addressed again inside the window keeps the assistant audible', async () => {
    // A wake phrase after the tool route has acted means the participant wants
    // the assistant back. Without superseding the request still in flight, its
    // backstop would silence the assistant again moments later.
    capture();
    try {
        const agent = novaAgent();
        agent.requestStopTalking('test');
        (agent as any).noteMuteToolRequested();
        await (agent as any).handleMuteTool('tool-use-id');
        assert.equal(agent.isMuted(), true);

        agent.unmute('wake phrase activation');
        assert.equal(agent.isMuted(), false);

        await delay(PAST_BACKSTOP_MS);
        assert.equal(agent.isMuted(), false, 'the backstop must not re-silence an assistant asked back');
        assert.match(stopLines().join('\n'), /action=cancelled/);
    } finally {
        release();
    }
});

// ---------------------------------------------------------------------------
// Modes that stay out of the race
// ---------------------------------------------------------------------------

test('translator mode declines outright and arms nothing', async () => {
    capture();
    try {
        const agent = novaAgent({ meetingMode: 'translator', activationMode: 'always_active' });
        assert.equal(agent.requestStopTalking('test'), false);
        assert.equal(agent.isMuted(), false);
        assert.equal((agent as any).stopRequest, null, 'a declined request must leave no timer behind');
        assert.match(stopLines().join('\n'), /action=declined-translator/);

        await delay(PAST_GRACE_MS);
        assert.equal(agent.isMuted(), false, 'translator mode must still be translating');
        assert.equal((agent as any)._translatorState, 'UNMUTED');
    } finally {
        release();
    }
});

test('the translator pause tool does not enter the stop-request race', () => {
    // Translator `mute` is a different tool: an exact trigger phrase, no spoken
    // acknowledgement, and its own state machine. It must not arm a fallback
    // whose job is to silence a conversational assistant.
    capture();
    try {
        const agent = novaAgent({ meetingMode: 'translator', activationMode: 'always_active' });
        (agent as any).noteMuteToolRequested();
        assert.equal((agent as any).stopRequest, null);
        assert.deepEqual(stopLines(), []);
    } finally {
        release();
    }
});

test('a mode with no mute tool is silenced at once, having nothing to wait for', () => {
    // Stands in for a future meeting mode: deferring buys an acknowledgement
    // only where there is a tool to produce one, so a mode without one must not
    // pay the delay.
    capture();
    try {
        const agent = novaAgent();
        (agent as any).meetingMode = 'future-mode-with-no-tool';
        assert.equal(agent.requestStopTalking('test'), true);
        assert.equal(agent.isMuted(), true, 'nothing can say "Okay", so do not wait for it');
        assert.equal((agent as any).stopRequest, null);
        assert.match(stopLines().join('\n'), /action=muted-immediately/);
    } finally {
        release();
    }
});

// ---------------------------------------------------------------------------
// What happened to the acknowledgement
// ---------------------------------------------------------------------------

test('the tool route lets playback drain instead of discarding it', async () => {
    // The barge-in that precedes a stop request has already killed the playback
    // stream, so the acknowledgement is written to a pacat spawned for it. A
    // process that has not finished connecting to PulseAudio has played nothing
    // yet, so the buffer has to be allowed to empty rather than timed.
    capture();
    try {
        const agent = novaAgent();
        await (agent as any).handleMuteTool('tool-use-id');
        assert.deepEqual(playbackTeardowns, ['drain']);
        assert.match(stopLines().join('\n'), /route=tool action=acknowledged[^\n]*playback_age_ms=/);
    } finally {
        release();
    }
});

test('the translator pause still cuts playback off immediately', async () => {
    // Its cue has already sounded and the pause is meant to be absolute.
    capture();
    try {
        const agent = novaAgent({ meetingMode: 'translator', activationMode: 'always_active' });
        (agent as any).playConfirmationTone = () => {};
        (agent as any).refreshSession = async () => {};
        await (agent as any).handleMuteTool('tool-use-id');
        assert.deepEqual(playbackTeardowns, ['immediate']);
    } finally {
        release();
    }
});

test('an acknowledgement lost to the barge-in gate is reported once, by gate', async () => {
    // This is the second mechanism that can swallow the "Okay": the barge-in
    // gate drops assistant audio until Nova opens its next audio block. The
    // gate is deliberately not relaxed for a stop request, so this line is how
    // a live meeting tells us whether it needs to be.
    capture();
    try {
        const agent = novaAgent();
        agent.requestStopTalking('test');
        (agent as any)._isInterrupted = true;

        await (agent as any).playAudio(Buffer.alloc(320));
        await (agent as any).playAudio(Buffer.alloc(320));

        const drops = stopLines().filter((line) => line.includes('action=ack-dropped'));
        assert.equal(drops.length, 1, 'a chunk-rate gate must not flood the log');
        assert.match(drops[0], /gate=interrupted/);

        await delay(PAST_GRACE_MS);
    } finally {
        release();
    }
});

test('an acknowledgement lost to the mute gate names that gate instead', async () => {
    capture();
    try {
        const agent = novaAgent();
        agent.requestStopTalking('test');
        (agent as any).noteMuteToolRequested();
        await (agent as any).handleMuteTool('tool-use-id');
        assert.equal(agent.isMuted(), true);

        // Nova emitting the word after the gate shut: generated, never heard.
        await (agent as any).playAudio(Buffer.alloc(320));

        const drops = stopLines().filter((line) => line.includes('action=ack-dropped'));
        assert.equal(drops.length, 1);
        assert.match(drops[0], /gate=muted/);

        await delay(PAST_BACKSTOP_MS);
    } finally {
        release();
    }
});

test('ordinary barge-in with no stop request in flight reports nothing', async () => {
    // The drop report is scoped to a stop request, so the gate that makes
    // barge-in work is not made noisy by it.
    capture();
    try {
        const agent = novaAgent();
        (agent as any)._isInterrupted = true;
        await (agent as any).playAudio(Buffer.alloc(320));
        assert.deepEqual(stopLines(), []);
    } finally {
        release();
    }
});
