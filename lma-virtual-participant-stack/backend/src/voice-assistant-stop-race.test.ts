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
 * produce the spoken "Okay"; the transcript route is expected to be the faster
 * one, and is the one that guarantees silence either way. These tests pin the
 * handover between them: the transcript route holds its mute for
 * STOP_REQUEST_TOOL_GRACE_MS, stands down if the tool call lands inside that
 * window, and acts if it does not. They also
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
    STOP_ACK_START_WINDOW_MS,
    STOP_ACK_MAX_AUDIO_MS,
} from './nova-agent.js';
import { MeetingMode } from './nova-sonic-config-loader.js';
import { simliAvatar } from './simli-avatar.js';

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

/** The same agent without the playback stub, for the tests whose subject is
 *  the real stopPacatStream. */
function bareAgent(): NovaAgent {
    return new NovaAgent({
        modelId: 'amazon.nova-2-sonic-v1:0',
        systemPrompt: 'You are Alex, an AI meeting assistant.',
        activationMode: 'wake_phrase',
        meetingMode: 'normal',
    });
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
        // The shipped value, so that retuning it from live measurements is a
        // deliberate act with somewhere to record the new one. Everything else
        // below derives from the constant, so a retune moves the behaviour
        // assertions with it and only this line has to be updated.
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
        assert.match(
            lines,
            new RegExp(`route=transcript action=deferred[^\\n]*window_ms=${STOP_REQUEST_TOOL_GRACE_MS}`),
        );
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

test('a tool call that arrives too late is timed and does not silence twice', async () => {
    // By how much the window was too short is the measurement the 500 ms is to
    // be tuned from, and it is only available on exactly the requests where
    // the window failed — so the time the transcript route recognised the
    // request has to outlive the request itself.
    capture();
    try {
        const agent = novaAgent();
        agent.requestStopTalking('test');
        await delay(PAST_GRACE_MS);
        assert.equal(agent.isMuted(), true, 'the fallback has already acted');

        (agent as any).noteMuteToolRequested();
        const claimed = stopLines().find((line) => line.includes('action=claimed'));
        assert.ok(claimed, 'a late tool call must still be reported');
        assert.match(claimed!, /late=true/, 'it must say which side of the deadline it landed on');
        assert.match(claimed!, /transcript_deferred=true/, 'it belongs to the transcript request');
        const latency = Number(/tool_latency_ms=(\d+)/.exec(claimed!)?.[1]);
        assert.ok(
            latency >= STOP_REQUEST_TOOL_GRACE_MS,
            `the latency must be measured, not discarded; got ${claimed}`,
        );

        await (agent as any).handleMuteTool('tool-use-id');
        // The gate was already shut, so nothing was drained and the word, if it
        // was generated at all, was dropped.
        assert.match(stopLines().join('\n'), /action=acknowledged[^\n]*gate=already-shut/);

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

test('a tool call after the assistant has been asked back is not timed against the old request', async () => {
    // The remembered time is scoped to the stretch of silence one request
    // caused. Once the assistant is audible again, a tool call belongs to
    // something else and attributing it to the old request would report a
    // latency that measures nothing.
    capture();
    try {
        const agent = novaAgent();
        agent.requestStopTalking('test');
        await delay(PAST_GRACE_MS);
        agent.unmute('wake phrase activation');
        assert.equal(agent.isMuted(), false);

        (agent as any).noteMuteToolRequested();
        const claimed = stopLines().filter((line) => line.includes('action=claimed')).pop();
        assert.match(claimed!, /late=false/);
        assert.match(claimed!, /tool_latency_ms=n\/a/);
        assert.match(claimed!, /transcript_deferred=false/);

        await delay(PAST_BACKSTOP_MS);
    } finally {
        release();
    }
});

test('a tool call long after the request is not timed against it either', async () => {
    // A silenced assistant goes on hearing the meeting, and in always_active
    // mode it can stay silent for many minutes, so a `mute` call well after the
    // fact is about something else. Reporting a latency for it would put a
    // number worth nothing into the set the window is to be tuned from.
    capture();
    try {
        const agent = novaAgent({ meetingMode: 'normal', activationMode: 'always_active' });
        agent.requestStopTalking('test');
        await delay(PAST_GRACE_MS);
        assert.equal(agent.isMuted(), true);

        // Still silent — nothing has asked the assistant back — but far enough
        // past the request that no tool call could be answering it.
        await delay(STOP_REQUEST_TOOL_BACKSTOP_MS * 2);
        (agent as any).noteMuteToolRequested();

        const claimed = stopLines().filter((line) => line.includes('action=claimed')).pop();
        assert.match(claimed!, /late=false/);
        assert.match(claimed!, /tool_latency_ms=n\/a/);
        assert.match(claimed!, /transcript_deferred=false/);

        await delay(PAST_BACKSTOP_MS);
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

test('group mode asked back inside the window keeps the assistant audible', async () => {
    // Group mode clears the audio gate directly rather than through unmute(),
    // so it has to call the request off itself. Without that, "Hey Alex, what's
    // next?" right after a stop would be answered and then cut off by the
    // backstop a second later.
    capture();
    try {
        const agent = novaAgent({ meetingMode: 'group', activationMode: 'always_active' });
        (agent as any)._isMuted = false; // as if a wake phrase had made it audible
        agent.requestStopTalking('test');
        (agent as any).noteMuteToolRequested();
        await (agent as any).handleMuteTool('tool-use-id');
        assert.equal(agent.isMuted(), true);

        (agent as any).handleUserTurnWakePhrase('Hey Alex, what is next on the agenda?');
        assert.equal(agent.isMuted(), false, 'the wake phrase must make it audible');

        await delay(PAST_BACKSTOP_MS);
        assert.equal(agent.isMuted(), false, 'the backstop must not re-silence it');
        assert.match(stopLines().join('\n'), /action=cancelled why=addressed-again/);
        (agent as any).clearConversationTimeout();
    } finally {
        release();
    }
});

test('unmuting an assistant that is already audible calls off nothing', async () => {
    // unmute() calls a request off, which is right once the assistant is
    // actually silent: it has been asked back. An assistant that is still
    // audible has not been asked back by anything, and calling the request off
    // there would reopen the route by which a misread stop utterance disarms
    // its own fallback.
    capture();
    try {
        const agent = novaAgent();
        agent.requestStopTalking('test');
        assert.equal(agent.isMuted(), false, 'still inside the window');

        agent.unmute('already audible');
        await delay(PAST_GRACE_MS);
        assert.equal(agent.isMuted(), true, 'the request must still be honoured');
        assert.doesNotMatch(stopLines().join('\n'), /action=cancelled/);
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

test('the model\'s own reading of the request cannot disarm the fallback', async () => {
    // handleUserTurnWakePhrase reads Nova's transcript of speech that Amazon
    // Transcribe has already matched, and two independent recognisers need not
    // agree on whether an utterance is strictly a stop command. Both utterances
    // below carry a wake phrase; only the first is a strict stop. Neither may
    // call off the silence the transcript route guaranteed, because at this
    // point either could be a second reading of the same request — and a single
    // misheard word would otherwise leave a participant who asked for quiet
    // with an assistant still talking.
    capture();
    try {
        for (const heardByNova of ['Hey Alex, stop talking', 'Hey Alex, stop talking about that']) {
            for (const mode of ['normal', 'group'] as MeetingMode[]) {
                const agent = novaAgent({ meetingMode: mode, activationMode: 'always_active' });
                (agent as any)._isMuted = false; // group starts muted
                agent.requestStopTalking('test');
                (agent as any).handleUserTurnWakePhrase(heardByNova);

                await delay(PAST_GRACE_MS);
                assert.equal(
                    agent.isMuted(),
                    true,
                    `${mode}: the request must survive Nova hearing "${heardByNova}"`,
                );
                (agent as any).clearConversationTimeout();
            }
        }
    } finally {
        release();
    }
});

test('an activation inside the window keeps the assistant audible', async () => {
    // A request can outlive the activation it was made during: the activation
    // lapses and a wake phrase starts a new one while the window is still open.
    // Starting to listen must not be followed by silence.
    //
    // activate() is driven directly because it is the entry point under test.
    // Reaching it through the scribe is not the point and is not possible for
    // an assistant that is still activated, which is the only kind the scribe
    // asks to stop — hence the narrow claim here, and none in the user docs.
    capture();
    try {
        const agent = novaAgent({ meetingMode: 'normal', activationMode: 'wake_phrase' });
        (agent as any)._isActive = true; // stand in for an established session
        agent.requestStopTalking('test');
        await agent.activate(1);

        await delay(PAST_GRACE_MS);
        assert.equal(agent.isMuted(), false, 'an activation must not be followed by silence');
        assert.match(stopLines().join('\n'), /action=cancelled why=activated/);

        // And it leaves no stop time behind. The assistant was never silenced,
        // so the unmute() that normally clears it never ran; a tool call now is
        // about something else, and timing it against the request that was
        // called off would put a reading worth nothing into the data the window
        // is tuned from.
        (agent as any).noteMuteToolRequested();
        const claimed = stopLines().filter((line) => line.includes('action=claimed')).pop();
        assert.match(claimed!, /late=false/);
        assert.match(claimed!, /tool_latency_ms=n\/a/);

        await delay(PAST_BACKSTOP_MS);
        agent.deactivate();
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

test('a handed-off playback stream stays reachable', () => {
    // Letting the buffer empty means a second process is briefly alive. Two
    // streams on `agent_output` at once would be audible as garble, so barge-in
    // has to be able to cut off the one still draining, and an assistant that
    // speaks again has to replace rather than talk over it.
    const agent = bareAgent();

    const killed: string[] = [];
    const fakePacat = () => ({
        killed: false,
        kill: (signal: string) => killed.push(signal),
        stdin: { end: () => {}, destroyed: false },
    });

    (agent as any).pacatProcess = fakePacat();
    (agent as any).stopPacatStream('drain');
    assert.ok((agent as any).drainingPacatProcess, 'the drained stream must be held, not forgotten');
    assert.deepEqual(killed, [], 'draining must not cut the buffer off');

    // Barge-in, an ordinary mute and shutdown all reach it.
    (agent as any).stopPacatStream('immediate');
    assert.equal((agent as any).drainingPacatProcess, null);
    assert.deepEqual(killed, ['SIGTERM']);

    // So does being about to be heard again: ensurePacatStream clears the way
    // rather than leaving the old stream playing under the new one. A live
    // stream is set up first so that it returns there and spawns no real pacat,
    // which is not what is being asserted and is not present in a unit test.
    const live = fakePacat();
    (agent as any).pacatProcess = live;
    (agent as any).stopPacatStream('drain');
    assert.ok((agent as any).drainingPacatProcess);

    (agent as any).pacatProcess = fakePacat();
    assert.equal((agent as any).ensurePacatStream(), (agent as any).pacatProcess);
    assert.equal((agent as any).drainingPacatProcess, null);
    assert.deepEqual(killed, ['SIGTERM', 'SIGTERM']);
});

test('a second hand-off does not leave the first stream playing', () => {
    // Only one stream can be held, so an earlier hand-off that has not finished
    // gives way rather than being forgotten while still audible.
    const agent = bareAgent();

    const killed: string[] = [];
    const fakePacat = (tag: string) => ({
        tag,
        killed: false,
        kill: (signal: string) => killed.push(`${tag}:${signal}`),
        stdin: { end: () => {}, destroyed: false },
    });

    (agent as any).pacatProcess = fakePacat('first');
    (agent as any).stopPacatStream('drain');
    (agent as any).pacatProcess = fakePacat('second');
    (agent as any).stopPacatStream('drain');

    assert.deepEqual(killed, ['first:SIGTERM'], 'the earlier stream gives way');
    assert.equal((agent as any).drainingPacatProcess.tag, 'second');
});

test('one hand-off\'s cap cannot cut short a later one', async () => {
    // Each hand-off is bounded, and by its own clock. An earlier cap firing
    // must not end a stream handed off after it, which would clip a later
    // acknowledgement to whatever was left of the earlier budget.
    const agent = bareAgent();
    const capMs: number = (agent as any).PLAYBACK_DRAIN_CAP_MS;

    const killed: string[] = [];
    const fakePacat = (tag: string) => ({
        tag,
        killed: false,
        kill: (signal: string) => killed.push(`${tag}:${signal}`),
        stdin: { end: () => {}, destroyed: false },
    });

    // The two hand-offs are separated by most of a cap, so the window between
    // the first cap firing and the second's is wide: the assertion below sits
    // 300 ms after the first deadline and 600 ms before the second, which a
    // loaded runner's timer overshoot is not going to cross. Tightening this
    // gap is what would make the suite flaky, not the real timers themselves.
    const gapMs = Math.round(capMs * 0.6);

    (agent as any).pacatProcess = fakePacat('first');
    (agent as any).stopPacatStream('drain');
    await delay(gapMs);
    (agent as any).pacatProcess = fakePacat('second');
    (agent as any).stopPacatStream('drain');
    assert.deepEqual(killed, ['first:SIGTERM']);

    // Past the first hand-off's cap, well inside the second's.
    await delay(capMs - gapMs + 300);
    assert.deepEqual(killed, ['first:SIGTERM'], 'the later stream must still be playing');
    assert.equal((agent as any).drainingPacatProcess?.tag, 'second');

    // And its own cap does end it.
    await delay(gapMs + 300);
    assert.deepEqual(killed, ['first:SIGTERM', 'second:SIGTERM']);
    assert.equal((agent as any).drainingPacatProcess, null);
});

test('a stream reporting its exit forgets only itself', () => {
    // A handed-off stream exits after a replacement may already have been
    // spawned. Clearing the replacement's reference would orphan it: nothing
    // left could stop it, and the next chunk would spawn a third alongside it.
    const agent = bareAgent();
    const live = { killed: false, kill: () => {}, stdin: { end: () => {}, destroyed: false } };
    const departed = { killed: true, kill: () => {}, stdin: { end: () => {}, destroyed: false } };

    (agent as any).pacatProcess = live;
    (agent as any).drainingPacatProcess = departed;

    // What every pacat close/error handler calls with the stream it was given.
    (agent as any).forgetPacatStream(departed);
    assert.equal((agent as any).pacatProcess, live, 'the stream in use must survive');
    assert.equal((agent as any).drainingPacatProcess, null, 'the departed stream is forgotten');

    // And a stream that is the one in use is forgotten, as before.
    (agent as any).forgetPacatStream(live);
    assert.equal((agent as any).pacatProcess, null);
});

test('cutting a drain short stops the lips with the sound', async () => {
    // The avatar holds its own copy of the speech for lip-sync. A drain that
    // runs out has been heard in full and the lips are already done, but one
    // cut short — by the cap, by barge-in, or by the assistant speaking again —
    // discards audio the avatar would otherwise go on mouthing silently. The
    // immediate path has always cleared it; the drain path has to as well.
    const agent = bareAgent();
    const realIsConnected = simliAvatar.isConnected;
    const realClear = simliAvatar.clearAudioBuffer;
    let cleared = 0;
    try {
        (simliAvatar as any).isConnected = () => true;
        (simliAvatar as any).clearAudioBuffer = async () => {
            cleared += 1;
        };

        // A stream still playing when something ends it.
        (agent as any).drainingPacatProcess = { killed: false, kill: () => {} };
        (agent as any).killDrainingPacatStream();
        assert.equal(cleared, 1, 'the avatar must stop with the audio');

        // A stream that ran out on its own, by the route production takes: a
        // self-exiting process keeps `killed === false`, and it is its `close`
        // handler calling forgetPacatStream that drops the reference, so there
        // is nothing left here to end. Its audio was played in full and the
        // lips kept up with it, so clearing the buffer would truncate them.
        const ranOut = { killed: false, kill: () => {} };
        (agent as any).drainingPacatProcess = ranOut;
        (agent as any).forgetPacatStream(ranOut);
        (agent as any).killDrainingPacatStream();
        assert.equal(cleared, 1, 'a drain that ran out needs no intervention');
    } finally {
        (simliAvatar as any).isConnected = realIsConnected;
        (simliAvatar as any).clearAudioBuffer = realClear;
    }
});

test('speaking again clears the way before handing the avatar new audio', async () => {
    // ensurePacatStream makes way for an utterance by ending a stream that is
    // still draining, and ending one clears the avatar's buffer. The chunk
    // therefore has to reach playback first: offered to the avatar before that,
    // the opening of the new utterance is discarded with the audio it was
    // making way for.
    const agent = novaAgent();
    const order: string[] = [];
    (agent as any).ensurePacatStream = () => {
        order.push('playback');
        return null;
    };

    const realIsConnected = simliAvatar.isConnected;
    const realSend = simliAvatar.sendAudioData;
    try {
        (simliAvatar as any).isConnected = () => true;
        (simliAvatar as any).sendAudioData = async () => {
            order.push('avatar');
        };
        await (agent as any).playAudio(Buffer.alloc(320));
        assert.deepEqual(order, ['playback', 'avatar']);
    } finally {
        (simliAvatar as any).isConnected = realIsConnected;
        (simliAvatar as any).sendAudioData = realSend;
        (agent as any).stopPacatStream();
    }
});

test('a playback stream that has gone away reports no age', async () => {
    // playback_age_ms is the line that says whether the acknowledgement was
    // written to a stream spawned for it. The age of a process that no longer
    // exists would answer that question wrongly, and confidently.
    capture();
    try {
        const agent = novaAgent();
        (agent as any).pacatSpawnedAt = Date.now() - 5000;
        (agent as any).pacatProcess = null;

        await (agent as any).handleMuteTool('tool-use-id');
        const acknowledged = stopLines().find((line) => line.includes('action=acknowledged'));
        assert.ok(acknowledged);
        assert.match(acknowledged!, /playback_age_ms=-1/);
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
        // `route` names where the request was recognised, as on every other
        // line in the family — here the transcript, with no tool call seen.
        assert.match(drops[0], /route=transcript/);
        assert.match(drops[0], /tool_seen=false/);

        await delay(PAST_GRACE_MS);
    } finally {
        release();
    }
});

test('an acknowledgement lost to the mute gate names that gate', async () => {
    // Group mode's auto-mute can shut the gate while a request is still
    // resolving, which is the mute-gate way of losing the word outright — as
    // opposed to losing audio that came after a drained one, below.
    capture();
    try {
        const agent = novaAgent({ meetingMode: 'group', activationMode: 'always_active' });
        (agent as any)._isMuted = false;
        agent.requestStopTalking('test');
        (agent as any).noteMuteToolRequested();
        (agent as any).executeAutoMute();
        assert.equal(agent.isMuted(), true, 'the gate shut without draining anything');

        await (agent as any).playAudio(Buffer.alloc(320));

        const drops = stopLines().filter((line) => line.includes('-dropped'));
        assert.equal(drops.length, 1);
        assert.match(drops[0], /action=ack-dropped/);
        assert.match(drops[0], /gate=muted/);
        assert.match(drops[0], /route=transcript/);

        await delay(PAST_BACKSTOP_MS);
        (agent as any).clearConversationTimeout();
    } finally {
        release();
    }
});

test('audio after a drained acknowledgement is not called a lost one', async () => {
    // Once the tool route has shut the gate behind a drained acknowledgement,
    // the word was allowed out. Anything after it is something else the model
    // went on saying, and reporting that as a lost acknowledgement would claim
    // a failure in a meeting that heard the word perfectly well.
    capture();
    try {
        const agent = novaAgent();
        agent.requestStopTalking('test');
        (agent as any).noteMuteToolRequested();
        await (agent as any).handleMuteTool('tool-use-id');
        assert.equal(agent.isMuted(), true);
        assert.match(stopLines().join('\n'), /action=acknowledged[^\n]*gate=draining/);

        await (agent as any).playAudio(Buffer.alloc(320));

        const drops = stopLines().filter((line) => line.includes('-dropped'));
        assert.equal(drops.length, 1);
        assert.match(drops[0], /action=post-gate-dropped/);
        assert.doesNotMatch(drops[0], /action=ack-dropped/);

        await delay(PAST_BACKSTOP_MS);
    } finally {
        release();
    }
});

test('a request the model raised on its own is reported as the tool route', async () => {
    // No transcript segment matched — the model noticed by itself. The report
    // has to say so, or a CloudWatch filter reading these lines attributes the
    // request to a route that never saw it.
    capture();
    try {
        const agent = novaAgent();
        (agent as any).noteMuteToolRequested();
        assert.match(stopLines().join('\n'), /route=tool action=claimed[^\n]*transcript_deferred=false/);
        assert.match(stopLines().join('\n'), /tool_latency_ms=n\/a/);

        await (agent as any).handleMuteTool('tool-use-id');
        assert.equal(agent.isMuted(), true);
        await (agent as any).playAudio(Buffer.alloc(320));

        const drops = stopLines().filter((line) => line.includes('-dropped'));
        assert.equal(drops.length, 1);
        assert.match(drops[0], /route=tool/);
        assert.match(drops[0], /action=post-gate-dropped/);

        await delay(PAST_BACKSTOP_MS);
        assert.equal(agent.isMuted(), true);
        // Its backstop closing out is the tool route's too: `route` names where
        // the request was recognised, on every line in the family.
        assert.match(stopLines().join('\n'), /route=tool action=stood-down/);
        assert.doesNotMatch(stopLines().join('\n'), /route=transcript/);
    } finally {
        release();
    }
});

test('a transcript stop catching up with the model is not called a deferral', async () => {
    // The model asked to mute itself and the transcript only then matched the
    // same utterance. Nothing was deferred on the transcript route, and the
    // account must not say it was.
    capture();
    try {
        const agent = novaAgent();
        (agent as any).noteMuteToolRequested();
        assert.equal(agent.requestStopTalking('test'), true);
        assert.match(stopLines().join('\n'), /action=already-claimed-by-tool/);
        assert.doesNotMatch(stopLines().join('\n'), /action=already-deferred/);

        await delay(PAST_BACKSTOP_MS);
        assert.equal(agent.isMuted(), true, 'either way it ends in silence');
    } finally {
        release();
    }
});

test('calling off a request the model raised on its own names the tool route', async () => {
    // Every line in the family names where the request was recognised, so that
    // one CloudWatch filter reads coherently; this is the last of them.
    capture();
    try {
        const agent = novaAgent();
        (agent as any).noteMuteToolRequested();
        await (agent as any).handleMuteTool('tool-use-id');
        assert.equal(agent.isMuted(), true);

        agent.unmute('wake phrase activation');
        const cancelled = stopLines().find((line) => line.includes('action=cancelled'));
        assert.ok(cancelled, 'being asked back must be reported');
        assert.match(cancelled!, /route=tool/);
        assert.doesNotMatch(stopLines().join('\n'), /route=transcript/);

        await delay(PAST_BACKSTOP_MS);
        assert.equal(agent.isMuted(), false, 'the backstop must not re-silence it');
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

// ---------------------------------------------------------------------------
// An acknowledgement spoken after the tool call
// ---------------------------------------------------------------------------

/** Bytes of 16 kHz, 16-bit mono playback PCM per millisecond. */
const BYTES_PER_MS = 32;

/** Record what reaches playback instead of spawning pacat. */
function recordPlayback(agent: NovaAgent): number[] {
    const written: number[] = [];
    (agent as any).ensurePacatStream = () => ({
        stdin: { destroyed: false, write: (chunk: Buffer) => written.push(chunk.length) },
    });
    return written;
}

/** A chunk of `ms` milliseconds of playback audio. */
function chunk(ms: number): Buffer {
    return Buffer.alloc(ms * BYTES_PER_MS);
}

function playedMs(written: number[]): number {
    return written.reduce((sum, bytes) => sum + bytes, 0) / BYTES_PER_MS;
}

test('the acknowledgement windows are the values the constants name', () => {
    // As with the grace window above: retuning these from live measurements is a
    // deliberate act, and everything else below derives from the constants.
    assert.equal(STOP_ACK_START_WINDOW_MS, 1500);
    assert.equal(STOP_ACK_MAX_AUDIO_MS, 1500);
});

test('an acknowledgement spoken after the mute tool call is played through the shut gate', async () => {
    // Measured live: Nova calls `mute` first and says "Okay" in its response to
    // the tool result, after the gate has shut. That word is the answer to the
    // request, so it is let through, and the gate holds for everything after it.
    capture();
    try {
        const agent = novaAgent();
        const written = recordPlayback(agent);
        agent.requestStopTalking('test');
        (agent as any).noteMuteToolRequested();
        await (agent as any).handleMuteTool('tool-use-id');
        assert.equal(agent.isMuted(), true);
        assert.deepEqual(playbackTeardowns, ['drain']);

        (agent as any).noteAudioBlockStarted();
        await (agent as any).playAudio(chunk(100));
        await (agent as any).playAudio(chunk(100));
        assert.equal(playedMs(written), 200, 'the acknowledgement must be played');
        (agent as any).noteAudioBlockEnded();
        assert.deepEqual(playbackTeardowns, ['drain', 'drain'], 'what was written must be left to drain');

        await (agent as any).playAudio(chunk(100));
        assert.equal(playedMs(written), 200, 'audio after the acknowledgement must stay dropped');
        assert.equal(agent.isMuted(), true);

        const lines = stopLines().join('\n');
        assert.match(lines, /action=ack-started delay_ms=\d+ gate=shut/);
        assert.match(lines, /action=ack-played audio_ms=200 gate=shut/);
        assert.match(lines, /action=post-gate-dropped/);
        assert.doesNotMatch(lines, /action=ack-dropped/);

        await delay(PAST_BACKSTOP_MS);
        assert.equal(agent.isMuted(), true, 'the request still ends in silence');
    } finally {
        release();
    }
});

test('an acknowledgement that runs on is cut at the budget', async () => {
    // A model that ignored the prompt and went on with its answer after the tool
    // call is heard for no longer than the budget.
    capture();
    try {
        const agent = novaAgent();
        const written = recordPlayback(agent);
        (agent as any).noteMuteToolRequested();
        await (agent as any).handleMuteTool('tool-use-id');

        (agent as any).noteAudioBlockStarted();
        for (let i = 0; i < STOP_ACK_MAX_AUDIO_MS / 100 + 5; i++) {
            await (agent as any).playAudio(chunk(100));
        }
        assert.equal(playedMs(written), STOP_ACK_MAX_AUDIO_MS, 'no more than the budget may be played');
        assert.match(
            stopLines().join('\n'),
            new RegExp(`action=ack-capped audio_ms=${STOP_ACK_MAX_AUDIO_MS} gate=shut`),
        );

        // A second block after the cap is not a second acknowledgement.
        (agent as any).noteAudioBlockStarted();
        await (agent as any).playAudio(chunk(100));
        assert.equal(playedMs(written), STOP_ACK_MAX_AUDIO_MS);

        await delay(PAST_BACKSTOP_MS);
    } finally {
        release();
    }
});

test('audio that starts after the acknowledgement window stays dropped', async () => {
    capture();
    try {
        const agent = novaAgent();
        const written = recordPlayback(agent);
        (agent as any).noteMuteToolRequested();
        await (agent as any).handleMuteTool('tool-use-id');
        (agent as any).stopAck.toolAt = Date.now() - STOP_ACK_START_WINDOW_MS - 1;

        (agent as any).noteAudioBlockStarted();
        await (agent as any).playAudio(chunk(100));
        assert.deepEqual(written, []);
        assert.doesNotMatch(stopLines().join('\n'), /action=ack-started/);

        await delay(PAST_BACKSTOP_MS);
    } finally {
        release();
    }
});

test('the answer already being spoken at the tool call is not taken for the acknowledgement', async () => {
    // Only a block that starts after the tool call can be the acknowledgement.
    // The rest of the answer the participant asked to stop is still cut.
    capture();
    try {
        const agent = novaAgent();
        const written = recordPlayback(agent);
        (agent as any).noteAudioBlockStarted(); // the answer, under way
        await (agent as any).playAudio(chunk(100));
        (agent as any).noteMuteToolRequested();
        await (agent as any).handleMuteTool('tool-use-id');

        await (agent as any).playAudio(chunk(100));
        await (agent as any).playAudio(chunk(100));
        assert.equal(playedMs(written), 100, 'only the audio before the gate may be played');
        assert.match(stopLines().join('\n'), /action=post-gate-dropped/);

        await delay(PAST_BACKSTOP_MS);
    } finally {
        release();
    }
});

test('an acknowledgement already playing when the gate shuts keeps its playback stream', async () => {
    // Handing the stream off to drain mid-word would make the next chunk spawn a
    // new stream, and spawning one kills the stream holding the start of the word.
    capture();
    try {
        const agent = novaAgent();
        const written = recordPlayback(agent);
        (agent as any).noteMuteToolRequested();
        (agent as any).noteAudioBlockStarted();
        await (agent as any).playAudio(chunk(100));

        await (agent as any).handleMuteTool('tool-use-id');
        assert.equal(agent.isMuted(), true);
        assert.deepEqual(playbackTeardowns, [], 'the stream must not be handed off mid-acknowledgement');
        assert.ok(gateEngagements().some((line) => line.includes('holding playback for the acknowledgement')));

        await (agent as any).playAudio(chunk(100));
        assert.equal(playedMs(written), 200);
        (agent as any).noteAudioBlockEnded();
        assert.deepEqual(playbackTeardowns, ['drain']);
        assert.match(stopLines().join('\n'), /action=ack-played audio_ms=200 gate=shut/);

        await delay(PAST_BACKSTOP_MS);
    } finally {
        release();
    }
});

test('a barge-in, being asked back or an absolute mute ends the acknowledgement', async () => {
    capture();
    try {
        for (const end of ['interrupted', 'unmute', 'mute'] as const) {
            const agent = novaAgent();
            const written = recordPlayback(agent);
            (agent as any).noteMuteToolRequested();
            await (agent as any).handleMuteTool('tool-use-id');
            (agent as any).noteAudioBlockStarted();
            await (agent as any).playAudio(chunk(100));
            assert.equal(playedMs(written), 100, `${end}: the acknowledgement started`);

            if (end === 'interrupted') {
                (agent as any).handleInterruption();
            } else if (end === 'unmute') {
                agent.unmute('wake phrase activation');
            } else {
                agent.mute('auto-mute');
            }
            assert.equal((agent as any).stopAck, null, `${end}: the exemption must be withdrawn`);
            if (end !== 'unmute') {
                await (agent as any).playAudio(chunk(100));
                assert.equal(playedMs(written), 100, `${end}: nothing more may be let through`);
            }
            assert.match(stopLines().join('\n'), new RegExp(`action=ack-cut why=${end === 'interrupted' ? 'interrupted' : end === 'unmute' ? 'unmuted' : 'muted'}`));
            (agent as any).cancelStopRequest('test-cleanup');
        }
    } finally {
        release();
    }
});

test('a tool call after the fallback has already silenced the assistant opens no acknowledgement', async () => {
    capture();
    try {
        const agent = novaAgent();
        const written = recordPlayback(agent);
        agent.requestStopTalking('test');
        await delay(PAST_GRACE_MS);
        assert.equal(agent.isMuted(), true);

        (agent as any).noteMuteToolRequested();
        (agent as any).noteAudioBlockStarted();
        await (agent as any).playAudio(chunk(100));
        assert.deepEqual(written, []);
        assert.equal((agent as any).stopAck, null);
    } finally {
        release();
    }
});

test('a barge-in marker before the acknowledgement starts leaves its window open', async () => {
    // The marker for the utterance that asked for silence can arrive after the
    // tool call. Closing the window on it would drop the very word this exists
    // to let through.
    capture();
    try {
        const agent = novaAgent();
        const written = recordPlayback(agent);
        (agent as any).noteMuteToolRequested();
        await (agent as any).handleMuteTool('tool-use-id');
        (agent as any).handleInterruption();
        assert.notEqual((agent as any).stopAck, null);

        // The acknowledgement block clears the barge-in gate as it opens, as
        // processResponseStream does for every new audio block.
        (agent as any)._isInterrupted = false;
        (agent as any).noteAudioBlockStarted();
        await (agent as any).playAudio(chunk(100));
        assert.equal(playedMs(written), 100);
        (agent as any).noteAudioBlockEnded();

        await delay(PAST_BACKSTOP_MS);
    } finally {
        release();
    }
});
