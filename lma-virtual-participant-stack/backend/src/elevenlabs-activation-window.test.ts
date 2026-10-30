/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The ElevenLabs activation window in `wake_phrase` mode: an idle timeout that
 * starts again whenever the assistant is busy, not a deadline from the wake
 * phrase. These mirror voice-assistant-activation-window.test.ts, which covers
 * the same rules on Nova Sonic.
 *
 * Time is mocked (`setTimeout` and `Date`). Playback is a stand-in for paplay
 * that takes as long as the PCM it is given would take to play; the WebSocket
 * is a stand-in that records what was sent and how often it was closed.
 *
 * What is NOT covered here: how ElevenLabs paces its audio chunks on a real
 * conversation, how long PulseAudio lags paplay exiting, and what the RMS
 * detector reports for real speech. Those need a live meeting.
 */
import { strict as assert } from 'node:assert';
import { afterEach, beforeEach, mock, test } from 'node:test';
import { ElevenLabsAgent } from './elevenlabs-agent.js';
import * as nova from './nova-agent.js';
import {
    ACTIVATION_AUDIBLE_HOLD_MAX_MS,
    ACTIVATION_RECHECK_MS,
    ACTIVATION_USER_TURN_GRACE_MS,
} from './voice-activation-window.js';

const WINDOW_S = 30;
const WINDOW_MS = WINDOW_S * 1000;
/** 16 kHz, 16-bit mono: bytes of playback PCM per millisecond. */
const BYTES_PER_MS = 32;
/** The pause the agent leaves after its queue drains before it counts as quiet. */
const QUEUE_TAIL_MS = 500;

let captured: string[] = [];
const realLog = console.log;
const realError = console.error;

beforeEach(() => {
    mock.timers.enable({ apis: ['setTimeout', 'Date'], now: 1_000_000 });
    captured = [];
    console.log = (...args: unknown[]) => {
        captured.push(args.map((arg) => String(arg)).join(' '));
    };
    console.error = console.log;
});

afterEach(() => {
    console.log = realLog;
    console.error = realError;
    mock.timers.reset();
});

interface Harness {
    agent: ElevenLabsAgent;
    a: any;
    /** Milliseconds of PCM handed to playback. */
    playedMs: () => number;
    /** How many times the conversation WebSocket was closed. */
    closes: () => number;
    sent: any[];
    audible: { value: boolean };
}

function harness(overrides: { activationMode?: string; activationDuration?: number } = {}): Harness {
    const agent = new ElevenLabsAgent({
        apiKey: 'test-key',
        activationMode: overrides.activationMode || 'wake_phrase',
        activationDuration: overrides.activationDuration,
        meetingMode: 'normal',
        strandsLambdaArn: 'arn:aws:lambda:us-east-1:123456789012:function:strands',
    });
    const a = agent as any;
    let playedMs = 0;
    let closes = 0;
    const sent: any[] = [];
    a.playChunk = (pcm: Buffer) => {
        const ms = pcm.length / BYTES_PER_MS;
        playedMs += ms;
        return new Promise<void>((resolve) => setTimeout(resolve, ms));
    };
    // Stand in for an open conversation so activate() does not reach ElevenLabs.
    a.ws = {
        readyState: 1,
        send: (m: string) => sent.push(JSON.parse(m)),
        close: () => { closes++; },
    };
    a.isConnected = true;
    const audible = { value: false };
    a.audibleOutput = () => audible.value;
    return { agent, a, playedMs: () => playedMs, closes: () => closes, sent, audible };
}

async function settle(): Promise<void> {
    for (let i = 0; i < 10; i++) await Promise.resolve();
}

async function tick(ms: number): Promise<void> {
    // In steps, letting promise continuations run between them, so the
    // playback queue advances as it would on a real clock.
    let left = ms;
    while (left > 0) {
        const step = Math.min(left, 250);
        mock.timers.tick(step);
        left -= step;
        await settle();
    }
}

/** ElevenLabs delivering `ms` of speech as one `audio` event. */
async function speak(h: Harness, ms: number): Promise<void> {
    await h.a.handleWebSocketMessage({
        type: 'audio',
        audio_event: { audio_base_64: Buffer.alloc(ms * BYTES_PER_MS).toString('base64') },
    });
}

async function heard(h: Harness, text: string): Promise<void> {
    await h.a.handleWebSocketMessage({ type: 'user_transcript', user_transcription_event: { user_transcript: text } });
}

function lookup(h: Harness, id: string): Promise<void> {
    return h.a.handleWebSocketMessage({
        type: 'client_tool_call',
        client_tool_call: { tool_name: 'strands_agent', tool_call_id: id, parameters: { query: 'q' } },
    });
}

// ---------------------------------------------------------------------------
// One set of rules for both providers
// ---------------------------------------------------------------------------

test('the shared window uses the same timings as the Nova Sonic agent', () => {
    // nova-agent.ts still carries its own copy of the window; this fails if
    // either copy changes without the other.
    assert.equal(ACTIVATION_RECHECK_MS, nova.ACTIVATION_RECHECK_MS);
    assert.equal(ACTIVATION_AUDIBLE_HOLD_MAX_MS, nova.ACTIVATION_AUDIBLE_HOLD_MAX_MS);
    assert.equal(ACTIVATION_USER_TURN_GRACE_MS, nova.ACTIVATION_USER_TURN_GRACE_MS);
});

// ---------------------------------------------------------------------------
// Still a timeout
// ---------------------------------------------------------------------------

test('elevenlabs: an idle activation lapses after the window and closes the connection', async () => {
    const h = harness();
    await h.agent.activate(WINDOW_S);
    await tick(WINDOW_MS - 100);
    assert.equal(h.agent.isActivated(), true, 'must not lapse early');
    await tick(200);
    assert.equal(h.agent.isActivated(), false, 'an idle window must still lapse on time');
    assert.equal(h.closes(), 1, 'wake_phrase mode closes the connection to save cost');
    assert.ok(captured.some((l) => l.startsWith('⏰ Activation window elapsed')));
});

test('elevenlabs: the configured duration is used when activate() is given none', async () => {
    const h = harness({ activationDuration: 60 });
    await h.agent.activate(undefined);
    await tick(45_000);
    assert.equal(h.agent.isActivated(), true);
    await tick(15_100);
    assert.equal(h.agent.isActivated(), false);
});

// ---------------------------------------------------------------------------
// Held open while busy
// ---------------------------------------------------------------------------

test('elevenlabs: the window does not lapse while the answer is still playing', async () => {
    const h = harness();
    await h.agent.activate(WINDOW_S);
    await tick(22_000);
    await speak(h, 26_000); // plays from 22 s to 48 s
    await tick(8_500); // past the original deadline
    assert.equal(h.agent.isActivated(), true, 'must not close mid-answer');
    assert.ok(captured.some((l) => l === '⏳ Activation window held open (audio-arriving)'));

    // Idle time counts from the end of playback, at 48.5 s.
    await tick(48_000 + QUEUE_TAIL_MS + WINDOW_MS - 30_500 - 1000);
    assert.equal(h.agent.isActivated(), true, 'the window is counted from the end of the answer');
    await tick(ACTIVATION_RECHECK_MS + 1500);
    assert.equal(h.agent.isActivated(), false);
    assert.equal(h.playedMs(), 26_000, 'the whole answer was played');
});

test('elevenlabs: a gap between audio chunks does not use up the window', async () => {
    const h = harness();
    await h.agent.activate(WINDOW_S);
    await tick(25_000);
    await speak(h, 4_000);
    await tick(8_000); // queue drained at ~29.5 s; a pause before the next chunk
    await speak(h, 4_000);
    await tick(4_000);
    assert.equal(h.agent.isActivated(), true);
    assert.equal(h.playedMs(), 8_000);
});

test('elevenlabs: the window does not lapse while the output detector still hears the assistant', async () => {
    const h = harness();
    await h.agent.activate(WINDOW_S);
    await tick(20_000);
    await speak(h, 5_000);
    await tick(5_000 + QUEUE_TAIL_MS); // queue drained at 25.5 s
    h.audible.value = true; // PulseAudio still playing after paplay exited
    await tick(8_000); // 33.5 s: past the original deadline
    assert.equal(h.agent.isActivated(), true);
    assert.ok(captured.some((l) => l === '⏳ Activation window held open (audible)'));
    h.audible.value = false;
    await tick(WINDOW_MS - 2000);
    assert.equal(h.agent.isActivated(), true, 'counted from when it went quiet');
    await tick(3000);
    assert.equal(h.agent.isActivated(), false);
});

test('elevenlabs: a detector that never reports silence cannot hold the connection open', async () => {
    const h = harness();
    await h.agent.activate(WINDOW_S);
    h.audible.value = true;
    await tick(WINDOW_MS + ACTIVATION_RECHECK_MS);
    assert.equal(h.agent.isActivated(), false, 'nothing was played, so the detector is not believed');

    const after = harness();
    await after.agent.activate(WINDOW_S);
    await speak(after, 2_000);
    await tick(2_000 + QUEUE_TAIL_MS);
    after.audible.value = true;
    await tick(ACTIVATION_AUDIBLE_HOLD_MAX_MS + WINDOW_MS + 2 * ACTIVATION_RECHECK_MS);
    assert.equal(after.agent.isActivated(), false, 'believed only shortly past the end of playback');
});

test('elevenlabs: the window does not lapse during a lookup, and gives the answer the whole window', async () => {
    const h = harness();
    h.a.invokeStrandsAgent = () => new Promise((resolve) => setTimeout(() => resolve('ok'), 25_000));
    await h.agent.activate(WINDOW_S);
    await tick(20_000);
    const call = lookup(h, 'tool-1');
    await tick(15_000); // 35 s: past the deadline, lookup still running
    assert.equal(h.agent.isActivated(), true, 'must not close while the lookup runs');
    assert.ok(captured.some((l) => l === '⏳ Activation window held open (tool)'));
    await tick(10_000); // the lookup finished at 45 s
    await call;
    assert.ok(h.sent.some((m) => m.type === 'client_tool_result' && m.tool_call_id === 'tool-1'));
    // Generating the answer takes a while; nothing is playing in between.
    await tick(3_000);
    await speak(h, 10_000);
    await tick(20_000);
    assert.equal(h.agent.isActivated(), true, 'the answer is not cut short');
    await tick(WINDOW_MS);
    assert.equal(h.agent.isActivated(), false, 'and the connection closes once it has been idle');
});

test('elevenlabs: overlapping lookups keep the window open until the last one finishes', async () => {
    const h = harness();
    const durations = [5_000, 25_000];
    h.a.invokeStrandsAgent = () => {
        const ms = durations.shift();
        return new Promise((resolve) => setTimeout(() => resolve('ok'), ms));
    };
    await h.agent.activate(WINDOW_S);
    await tick(25_000);
    lookup(h, 'tool-1');
    await tick(1_000);
    lookup(h, 'tool-2');
    await tick(10_000); // 36 s: the first lookup has finished, the second has not
    assert.equal(h.a.toolCallsInFlight, 1, 'the second lookup is still running');
    await tick(WINDOW_MS);
    assert.equal(h.agent.isActivated(), true, 'held for as long as any lookup runs');
});

// ---------------------------------------------------------------------------
// What restarts the countdown
// ---------------------------------------------------------------------------

test('elevenlabs: being addressed again re-arms a running window', async () => {
    const h = harness();
    await h.agent.activate(WINDOW_S);
    await tick(25_000);
    await heard(h, 'Hey Alex, and what about tomorrow?');
    assert.ok(captured.some((l) => l === '🔁 Activation window re-armed (addressed again)'));
    await tick(WINDOW_MS - 1000);
    assert.equal(h.agent.isActivated(), true);
    await tick(2000);
    assert.equal(h.agent.isActivated(), false);
});

test('elevenlabs: a barge-in restarts the countdown, so a correction is not cut off', async () => {
    const h = harness();
    await h.agent.activate(WINDOW_S);
    await tick(29_000);
    await h.a.handleWebSocketMessage({ type: 'interruption' });
    await tick(WINDOW_MS - 1000);
    assert.equal(h.agent.isActivated(), true);
    await tick(2000);
    assert.equal(h.agent.isActivated(), false);
});

test('elevenlabs: meeting talk holds a lapsing window only briefly, not indefinitely', async () => {
    const h = harness();
    await h.agent.activate(WINDOW_S);
    await tick(WINDOW_MS - 500);
    await heard(h, 'what do we think about the launch date');
    await tick(1000);
    assert.equal(h.agent.isActivated(), true, 'a question asked as the window lapses is given a moment');
    await tick(ACTIVATION_USER_TURN_GRACE_MS + ACTIVATION_RECHECK_MS);
    assert.equal(h.agent.isActivated(), false, 'one turn is not a re-arm');

    const busy = harness();
    await busy.agent.activate(WINDOW_S);
    for (let t = 0; t < 4 * WINDOW_MS; t += 1000) {
        await heard(busy, 'and then the other team said they would follow up');
        await tick(1000);
    }
    assert.equal(busy.agent.isActivated(), false, 'talk among participants cannot keep the connection open forever');
});

// ---------------------------------------------------------------------------
// Stop requests and closing
// ---------------------------------------------------------------------------

test('elevenlabs: a stop request during the tail of an answer reaches an activated assistant', async () => {
    const h = harness();
    await h.agent.activate(WINDOW_S);
    await tick(22_000);
    await speak(h, 10_000);
    await speak(h, 10_000);
    await speak(h, 6_000);
    await tick(15_000); // 37 s: past the original deadline, answer still playing
    assert.equal(h.agent.isActivated(), true, 'the scribe only routes stop requests to an activated assistant');
    assert.equal(h.agent.requestStopTalking('test'), true);
    assert.equal(h.agent.isMuted(), true);
    // The chunk already handed to paplay finishes at 42 s; the queued one is dropped.
    await tick(5_000 + QUEUE_TAIL_MS + WINDOW_MS + 2 * ACTIVATION_RECHECK_MS);
    assert.equal(h.agent.isActivated(), false);
    assert.ok(h.playedMs() < 26_000, 'the queued rest of the answer was not played');
});

test('elevenlabs: a direct deactivate drops queued assistant audio', async () => {
    const h = harness();
    await h.agent.activate(WINDOW_S);
    await speak(h, 5_000);
    await speak(h, 5_000);
    h.agent.deactivate();
    assert.ok(captured.some((l) => l === '🔇 Dropping 1 queued audio chunks as the connection closes'));
    await tick(15_000);
    assert.equal(h.playedMs(), 5_000);
});

// ---------------------------------------------------------------------------
// Playback queue
// ---------------------------------------------------------------------------

test('elevenlabs: a chunk that arrives as the queue drains is still played', async () => {
    const h = harness();
    await h.agent.activate(WINDOW_S);
    await speak(h, 1_000);
    await tick(1_100); // played; inside the tail before the queue counts as idle
    await speak(h, 1_000);
    await tick(3_000);
    assert.equal(h.playedMs(), 2_000);
    assert.equal(h.a.audioQueue.length, 0);
});

test('elevenlabs: a chunk that fails to play does not hold the window open', async () => {
    const h = harness();
    h.a.playChunk = () => Promise.reject(new Error('paplay missing'));
    await h.agent.activate(WINDOW_S);
    await speak(h, 1_000);
    await tick(WINDOW_MS + QUEUE_TAIL_MS + 2 * ACTIVATION_RECHECK_MS);
    assert.equal(h.agent.isActivated(), false);
});

// ---------------------------------------------------------------------------
// Other modes and shutdown
// ---------------------------------------------------------------------------

test('elevenlabs: always_active has no window to lapse', async () => {
    const h = harness({ activationMode: 'always_active' });
    await h.agent.activate(WINDOW_S);
    await tick(5 * WINDOW_MS);
    assert.equal(h.agent.isActivated(), true);
    assert.equal(h.closes(), 0);
    assert.ok(!captured.some((l) => l.startsWith('⏳') || l.startsWith('⏰')));
});

test('elevenlabs: stop() ends a running window', async () => {
    const h = harness();
    await h.agent.activate(WINDOW_S);
    await h.agent.stop();
    await tick(2 * WINDOW_MS);
    assert.ok(!captured.some((l) => l.startsWith('⏰')));
});
