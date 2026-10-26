/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The Nova Sonic activation window in `wake_phrase` mode: an idle timeout that
 * starts again whenever the assistant is busy, not a deadline from the wake
 * phrase.
 *
 * The case these tests are built around was recorded on a live meeting with a
 * 30-second window: a lookup and a long answer used the window up between them,
 * the session closed while the answer was still playing, and a participant who
 * then said "stop" was talking to a session that could no longer hear it. So
 * the window must not lapse while a lookup is running or while the meeting can
 * still hear the assistant, and must still lapse — closing the session — once
 * the assistant has been idle for the whole of it.
 *
 * Time is mocked (`setTimeout` and `Date`), so a 30-second window costs
 * nothing. Playback is a stand-in that records what was written; the estimate
 * of when it finishes is the agent's own.
 *
 * What is NOT covered here: how long real PulseAudio playback lags Nova's
 * delivery, and what the RMS detector reports for real speech. Both need a live
 * meeting.
 */
import { strict as assert } from 'node:assert';
import { afterEach, beforeEach, mock, test } from 'node:test';
import {
    ACTIVATION_RECHECK_MS,
    ACTIVATION_USER_TURN_GRACE_MS,
    NovaAgent,
    STOP_REQUEST_TOOL_GRACE_MS,
} from './nova-agent.js';

const WINDOW_S = 30;
const WINDOW_MS = WINDOW_S * 1000;
/** 16 kHz, 16-bit mono: bytes of playback PCM per millisecond. */
const BYTES_PER_MS = 32;

let captured: string[] = [];
const realLog = console.log;

beforeEach(() => {
    mock.timers.enable({ apis: ['setTimeout', 'Date'], now: 1_000_000 });
    captured = [];
    console.log = (...args: unknown[]) => {
        captured.push(args.map((arg) => String(arg)).join(' '));
    };
});

afterEach(() => {
    console.log = realLog;
    mock.timers.reset();
});

interface Harness {
    agent: NovaAgent;
    /** Bytes written to the playback stand-in. */
    written: () => number;
    /** How many playback streams were killed. */
    kills: () => number;
    /** How many times the session was closed. */
    closes: () => number;
    audible: { value: boolean };
}

function harness(overrides: { activationMode?: string; activationDuration?: number } = {}): Harness {
    const agent = new NovaAgent({
        modelId: 'amazon.nova-2-sonic-v1:0',
        systemPrompt: 'You are Alex, an AI meeting assistant.',
        activationMode: overrides.activationMode || 'wake_phrase',
        activationDuration: overrides.activationDuration,
        meetingMode: 'normal',
    });
    const a = agent as any;
    let written = 0;
    let kills = 0;
    let closes = 0;
    a.ensurePacatStream = () => {
        if (!a.pacatProcess) {
            a.pacatProcess = {
                killed: false,
                stdin: { destroyed: false, write: (b: Buffer) => { written += b.length; return true; }, end() {} },
                kill() { this.killed = true; kills++; },
            };
        }
        return a.pacatProcess;
    };
    a.closeSession = async () => {
        closes++;
        a.session = null;
        a._isActive = false;
    };
    const audible = { value: false };
    a.audibleOutput = () => audible.value;
    // Stand in for an established session so activate() does not reach Bedrock.
    a._isActive = true;
    a.session = { isActive: true, queue: [], promptName: 'p', audioContentId: 'a' };
    return { agent, written: () => written, kills: () => kills, closes: () => closes, audible };
}

/** Nova delivering `ms` of speech in one burst, as it does: far faster than
 *  the meeting hears it. */
function speak(agent: NovaAgent, ms: number): void {
    (agent as any).playAudio(Buffer.alloc(ms * BYTES_PER_MS));
}

function tick(ms: number): void {
    // In steps, so each timer the agent arms on the way is run at its own time.
    let left = ms;
    while (left > 0) {
        const step = Math.min(left, 250);
        mock.timers.tick(step);
        left -= step;
    }
}

async function settle(): Promise<void> {
    // Let promise continuations scheduled by the agent run.
    for (let i = 0; i < 5; i++) await Promise.resolve();
}

// ---------------------------------------------------------------------------
// Still a timeout
// ---------------------------------------------------------------------------

test('an idle activation lapses after the window and closes the session', async () => {
    const h = harness();
    await h.agent.activate(WINDOW_S);
    tick(WINDOW_MS - 100);
    assert.equal(h.agent.isActivated(), true, 'must not lapse early');
    tick(200);
    assert.equal(h.agent.isActivated(), false, 'an idle window must still lapse on time');
    await settle();
    assert.equal(h.closes(), 1, 'wake_phrase mode closes the session to save cost');
    assert.ok(captured.some((l) => l.startsWith('⏰ Activation window elapsed')));
});

test('the configured duration is used when activate() is given none', async () => {
    // The scribe activates without a duration, so the deployment's
    // VoiceAssistantActivationDuration is what decides the window.
    const h = harness({ activationDuration: 60 });
    await h.agent.activate(undefined);
    tick(45_000);
    assert.equal(h.agent.isActivated(), true);
    tick(15_100);
    assert.equal(h.agent.isActivated(), false);
});

// ---------------------------------------------------------------------------
// Held open while busy
// ---------------------------------------------------------------------------

test('the window does not lapse while the answer is still playing', async () => {
    // The recorded meeting: the answer arrives late in the window, in a burst
    // that takes far longer to play than to deliver.
    const h = harness();
    await h.agent.activate(WINDOW_S);
    tick(22_000);
    speak(h.agent, 26_000);
    tick(8_500); // past the original deadline
    assert.equal(h.agent.isActivated(), true, 'must not close mid-answer');
    assert.ok(captured.some((l) => l === '⏳ Activation window held open (playback)'));

    // Idle time counts from the end of playback, at 48 s.
    tick(48_000 + WINDOW_MS - 100 - 30_500);
    assert.equal(h.agent.isActivated(), true, 'the window is counted from the end of the answer');
    tick(ACTIVATION_RECHECK_MS + 200);
    assert.equal(h.agent.isActivated(), false);
    assert.equal(h.kills(), 0, 'nothing was left to cut by the time it lapsed');
});

test('the window does not lapse while the output detector still hears the assistant', async () => {
    // Playback running later than the estimate says, as PulseAudio latency can.
    const h = harness();
    await h.agent.activate(WINDOW_S);
    tick(20_000);
    speak(h.agent, 12_000);
    h.audible.value = true;
    tick(17_000); // 37 s: 5 s past the estimated end of playback
    assert.equal(h.agent.isActivated(), true);
    assert.ok(captured.some((l) => l === '⏳ Activation window held open (audible)'));
    h.audible.value = false;
    tick(WINDOW_MS - 2000);
    assert.equal(h.agent.isActivated(), true, 'counted from when it went quiet');
    tick(3000);
    assert.equal(h.agent.isActivated(), false);
});

test('a detector that never reports silence cannot hold the session open', async () => {
    const h = harness();
    await h.agent.activate(WINDOW_S);
    h.audible.value = true;
    tick(WINDOW_MS + ACTIVATION_RECHECK_MS);
    assert.equal(h.agent.isActivated(), false);
});

test('the window does not lapse during a lookup, and gives the answer the whole window', async () => {
    const h = harness();
    const a = h.agent as any;
    a.invokeStrandsAgent = () => new Promise((resolve) => setTimeout(() => resolve({ message: 'ok' }), 25_000));
    a.sendToolResult = async () => {};
    await h.agent.activate(WINDOW_S);
    tick(20_000);
    const lookup = a.processToolUseAsync('tool-1', 'strands_agent', { content: '{}' });
    tick(15_000); // 35 s: past the deadline, lookup still running
    assert.equal(h.agent.isActivated(), true, 'must not close while the lookup runs');
    assert.ok(captured.some((l) => l === '⏳ Activation window held open (tool)'));
    tick(10_000); // the lookup finished at 45 s
    await settle();
    await lookup;
    tick(2_000);
    await settle();
    // The answer: 10 s of speech that starts arriving 1 s after the result.
    speak(h.agent, 10_000);
    tick(20_000);
    assert.equal(h.agent.isActivated(), true, 'the answer is not cut short');
    tick(WINDOW_MS);
    assert.equal(h.agent.isActivated(), false, 'and the session closes once it has been idle');
});

test('overlapping lookups keep the window open until the last one finishes', async () => {
    const h = harness();
    const a = h.agent as any;
    const durations = [5_000, 25_000];
    a.invokeStrandsAgent = () => {
        const ms = durations.shift();
        return new Promise((resolve) => setTimeout(() => resolve({ message: 'ok' }), ms));
    };
    a.sendToolResult = async () => {};
    await h.agent.activate(WINDOW_S);
    tick(25_000);
    a.processToolUseAsync('tool-1', 'strands_agent', { content: '{}' });
    tick(1_000);
    a.processToolUseAsync('tool-2', 'strands_agent', { content: '{}' });
    for (let i = 0; i < 40; i++) {
        tick(250);
        await settle();
    }
    // 36 s: the first lookup has finished and its padding has elapsed.
    assert.equal(a._isProcessingTool, true, 'the second lookup is still running');
    tick(10_000);
    await settle();
    assert.equal(h.agent.isActivated(), true);
});

// ---------------------------------------------------------------------------
// What restarts the countdown
// ---------------------------------------------------------------------------

test('being addressed again re-arms a running window', async () => {
    const h = harness();
    await h.agent.activate(WINDOW_S);
    tick(25_000);
    (h.agent as any).handleUserTurnWakePhrase('Hey Alex, and what about tomorrow?');
    assert.ok(captured.some((l) => l === '🔁 Activation window re-armed (addressed again)'));
    tick(WINDOW_MS - 1000);
    assert.equal(h.agent.isActivated(), true);
    tick(2000);
    assert.equal(h.agent.isActivated(), false);
});

test('a barge-in restarts the countdown, so a correction is not cut off', async () => {
    // Recorded twice on the same meeting: the participant spoke over the
    // answer to correct it and the session closed under them.
    const h = harness();
    await h.agent.activate(WINDOW_S);
    tick(29_000);
    (h.agent as any).handleInterruption();
    tick(WINDOW_MS - 1000);
    assert.equal(h.agent.isActivated(), true);
    tick(2000);
    assert.equal(h.agent.isActivated(), false);
});

test('meeting talk holds a lapsing window only briefly, not indefinitely', async () => {
    const h = harness();
    const a = h.agent as any;
    await h.agent.activate(WINDOW_S);
    tick(WINDOW_MS - 500);
    a.lastUserTextAt = Date.now();
    tick(1000);
    assert.equal(h.agent.isActivated(), true, 'a question asked as the window lapses is given a moment');
    tick(ACTIVATION_USER_TURN_GRACE_MS + ACTIVATION_RECHECK_MS);
    assert.equal(h.agent.isActivated(), false, 'one turn is not a re-arm');

    // Continuous talk that never addresses the assistant is bounded too.
    const busy = harness();
    const b = busy.agent as any;
    await busy.agent.activate(WINDOW_S);
    for (let t = 0; t < 4 * WINDOW_MS; t += 1000) {
        b.lastUserTextAt = Date.now();
        tick(1000);
    }
    assert.equal(busy.agent.isActivated(), false, 'talk among participants cannot keep the session open forever');
});

// ---------------------------------------------------------------------------
// Stop requests and closing
// ---------------------------------------------------------------------------

test('a stop request during the tail of an answer is heard, because the session is still open', async () => {
    // The recorded failure: "stop" said after the window's original deadline,
    // while the answer was still playing, went nowhere.
    const h = harness();
    await h.agent.activate(WINDOW_S);
    tick(22_000);
    speak(h.agent, 26_000);
    tick(15_000); // 37 s: past the original deadline, answer still playing
    assert.equal(h.agent.isActivated(), true, 'the scribe only routes stop requests to an activated assistant');
    assert.equal(h.agent.requestStopTalking('test'), true);
    tick(STOP_REQUEST_TOOL_GRACE_MS + 100);
    assert.equal(h.agent.isMuted(), true);
    assert.ok(h.kills() >= 1, 'the rest of the answer is cut off');
    // Nothing is left playing, so the window counts down from here and closes.
    tick(WINDOW_MS + ACTIVATION_RECHECK_MS);
    assert.equal(h.agent.isActivated(), false);
});

test('a session is never closed with assistant audio left to play', async () => {
    // The window cannot lapse while audio is still to be heard; a direct
    // deactivate() cuts the audio off rather than leave it playing from a
    // session that could not hear "stop".
    const h = harness();
    await h.agent.activate(WINDOW_S);
    speak(h.agent, 10_000);
    h.agent.deactivate();
    assert.equal(h.kills(), 1);
    assert.ok(captured.some((l) => /^🔇 Cutting \d+ ms of unplayed assistant audio/.test(l)));
});

// ---------------------------------------------------------------------------
// Other modes
// ---------------------------------------------------------------------------

test('always_active has no window to lapse', async () => {
    const h = harness({ activationMode: 'always_active' });
    await h.agent.activate(WINDOW_S);
    tick(5 * WINDOW_MS);
    assert.equal(h.agent.isActivated(), true);
    assert.equal(h.closes(), 0);
    assert.ok(!captured.some((l) => l.startsWith('⏳') || l.startsWith('⏰')));
});

test('switching to always_active while a window is held open ends the window', async () => {
    const h = harness();
    await h.agent.activate(WINDOW_S);
    tick(22_000);
    speak(h.agent, 26_000);
    tick(10_000);
    (h.agent as any).applyActivationMode('always_active');
    tick(5 * WINDOW_MS);
    assert.equal(h.agent.isActivated(), true);
    assert.equal(h.closes(), 0);
});
