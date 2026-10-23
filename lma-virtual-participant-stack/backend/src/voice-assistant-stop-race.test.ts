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

test('a fresh wake-phrase activation inside the window keeps the assistant audible', async () => {
    capture();
    try {
        const agent = novaAgent({ meetingMode: 'normal', activationMode: 'wake_phrase' });
        (agent as any)._isActive = true; // stand in for an established session
        agent.requestStopTalking('test');
        await agent.activate(1);

        await delay(PAST_GRACE_MS);
        assert.equal(agent.isMuted(), false, 'an activation must not be followed by silence');
        assert.match(stopLines().join('\n'), /action=cancelled why=activated/);
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

    (agent as any).pacatProcess = fakePacat('first');
    (agent as any).stopPacatStream('drain');
    await delay(300);
    (agent as any).pacatProcess = fakePacat('second');
    (agent as any).stopPacatStream('drain');
    assert.deepEqual(killed, ['first:SIGTERM']);

    // Past the first hand-off's cap, inside the second's.
    await delay(capMs - 300 + 150);
    assert.deepEqual(killed, ['first:SIGTERM'], 'the later stream must still be playing');
    assert.equal((agent as any).drainingPacatProcess?.tag, 'second');

    // And its own cap does end it.
    await delay(450);
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
        assert.match(drops[0], /route=transcript/);

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

        const drops = stopLines().filter((line) => line.includes('action=ack-dropped'));
        assert.equal(drops.length, 1);
        assert.match(drops[0], /route=tool/);

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
