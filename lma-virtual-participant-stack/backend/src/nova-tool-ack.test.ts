/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The acknowledgement a `strands_agent` lookup gets: the model's own sentence
 * when it says one, otherwise a clip the agent rendered in the same voice at
 * startup, played TOOL_ACK_FALLBACK_MS after the tool-use event.
 *
 * Recorded on a live meeting: the model's "Let me search..." started within
 * about 150 ms of the tool-use event for each first question after a wake
 * phrase, and said nothing for any follow-up inside the same session, leaving
 * 7-10 seconds of silence. These tests drive the agent with the same events in
 * the same order, and pin the `[tool-ack]` lines, which are what a live meeting
 * is diagnosed from.
 *
 * Time is mocked (`setTimeout` and `Date`). Playback is a stand-in that records
 * what was written.
 *
 * What is NOT covered here: whether the model obeys the prompt, and what the
 * rendered clip sounds like. Both need a live meeting.
 */
import { strict as assert } from 'node:assert';
import { afterEach, beforeEach, mock, test } from 'node:test';
import {
    NovaAgent,
    STOP_REQUEST_TOOL_GRACE_MS,
    TOOL_ACK_FALLBACK_MS,
    TOOL_ACK_LATE_PREAMBLE_MS,
} from './nova-agent.js';

/** A stand-in clip: 1.5 s of PCM at 32 bytes/ms, filled so it is recognisable. */
const CLIP = Buffer.alloc(1500 * 32, 7);

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

function ackLines(): string[] {
    return captured.filter((l) => l.startsWith('[tool-ack]'));
}

interface Harness {
    agent: NovaAgent;
    a: any;
    writes: Buffer[];
    kills: () => number;
    clipWrites: () => number;
}

function harness(options: { clip?: Buffer | null } = {}): Harness {
    const agent = new NovaAgent({
        modelId: 'amazon.nova-2-sonic-v1:0',
        systemPrompt: 'You are Alex, an AI meeting assistant.',
        activationMode: 'wake_phrase',
        meetingMode: 'normal',
    });
    const a = agent as any;
    const writes: Buffer[] = [];
    let kills = 0;
    a.ensurePacatStream = () => {
        if (!a.pacatProcess) {
            a.pacatProcess = {
                killed: false,
                stdin: { destroyed: false, write: (b: Buffer) => { writes.push(b); return true; }, end() {} },
                kill() { this.killed = true; kills++; },
            };
        }
        return a.pacatProcess;
    };
    a.audibleOutput = () => false;
    a.closeSession = async () => {};
    a.session = { isActive: true, queue: [], promptName: 'p', audioContentId: 'a' };
    a._isActive = true;
    a._isActivated = true;
    a.toolAckClip = options.clip === undefined ? CLIP : options.clip;
    return { agent, a, writes, kills: () => kills, clipWrites: () => writes.filter((w) => w === CLIP).length };
}

/** Nova's events, in the order a live stream delivers them. */
function userText(h: Harness): void {
    h.a.lastUserTextAt = Date.now();
}
function toolUse(h: Harness, id = 'tool-1'): void {
    h.a.noteStrandsToolRequested(id);
}
function audioBlock(h: Harness, ms = 1000): void {
    h.a.noteAudioBlockStarted();
    h.a.noteAssistantAudioBlockStarted();
    h.a.playAudio(Buffer.alloc(ms * 32, 1));
}
function audioBlockEnd(h: Harness): void {
    h.a.noteAudioBlockEnded();
    h.a.dropCurrentAudioBlock = false;
}

test('the model speaking its own preamble is recorded and no clip is played', () => {
    const h = harness();
    userText(h);
    toolUse(h);
    mock.timers.tick(15);
    audioBlock(h);
    mock.timers.tick(TOOL_ACK_FALLBACK_MS * 3);
    assert.equal(h.clipWrites(), 0);
    assert.deepEqual(ackLines(), ['[tool-ack] source=model latency_ms=15']);
});

test('a preamble spoken just before the tool call counts as the acknowledgement', () => {
    const h = harness();
    userText(h);
    mock.timers.tick(200);
    audioBlock(h);
    mock.timers.tick(600);
    toolUse(h);
    mock.timers.tick(TOOL_ACK_FALLBACK_MS * 3);
    assert.equal(h.clipWrites(), 0);
    assert.deepEqual(ackLines(), ['[tool-ack] source=model latency_ms=-600']);
});

test('a follow-up lookup with no preamble gets the clip after the fallback delay', () => {
    // The recorded follow-ups: a tool call and then nothing until the answer.
    const h = harness();
    userText(h);
    toolUse(h);
    mock.timers.tick(TOOL_ACK_FALLBACK_MS - 1);
    assert.equal(h.clipWrites(), 0, 'the model is given the whole delay first');
    mock.timers.tick(1);
    assert.equal(h.clipWrites(), 1);
    assert.deepEqual(ackLines(), [`[tool-ack] source=fallback latency_ms=${TOOL_ACK_FALLBACK_MS} audio_ms=1500`]);
});

test('a preamble that arrives after the clip is dropped, and the answer is not', () => {
    const h = harness();
    userText(h);
    toolUse(h);
    mock.timers.tick(TOOL_ACK_FALLBACK_MS + 500);
    const before = h.writes.length;
    audioBlock(h); // the model's own preamble, late
    assert.equal(h.writes.length, before, 'two acknowledgements back to back are not played');
    audioBlockEnd(h);
    assert.ok(ackLines().some((l) => l.startsWith('[tool-ack] action=late-preamble-dropped')));

    // The result goes back to the model, and its answer is played.
    h.a.noteToolResultReady('tool-1');
    mock.timers.tick(3000);
    audioBlock(h, 5000);
    assert.equal(h.writes.length, before + 1, 'the answer must be heard');
});

test('audio after the late-preamble window is played even before the result', () => {
    const h = harness();
    userText(h);
    toolUse(h);
    mock.timers.tick(TOOL_ACK_LATE_PREAMBLE_MS + 100);
    const before = h.writes.length;
    audioBlock(h);
    assert.equal(h.writes.length, before + 1);
});

test('a lookup that finishes inside the delay plays nothing', () => {
    const h = harness();
    toolUse(h);
    mock.timers.tick(400);
    h.a.noteToolResultReady('tool-1');
    mock.timers.tick(TOOL_ACK_FALLBACK_MS * 2);
    assert.equal(h.clipWrites(), 0);
    assert.deepEqual(ackLines(), ['[tool-ack] source=none reason=result-first latency_ms=400']);
});

test('a muted assistant plays no acknowledgement', () => {
    const h = harness();
    h.a._isMuted = true;
    toolUse(h);
    mock.timers.tick(TOOL_ACK_FALLBACK_MS);
    assert.equal(h.clipWrites(), 0);
    assert.match(ackLines()[0], /source=none reason=muted/);
});

test('a stop request before the delay means the clip never starts', () => {
    const h = harness();
    toolUse(h);
    mock.timers.tick(300);
    h.agent.requestStopTalking('test');
    mock.timers.tick(TOOL_ACK_FALLBACK_MS * 3);
    assert.equal(h.clipWrites(), 0);
    assert.match(ackLines()[0], /source=none reason=stop-request/);
});

test('a stop request while the clip plays cuts it off', () => {
    const h = harness();
    toolUse(h);
    mock.timers.tick(TOOL_ACK_FALLBACK_MS + 200);
    assert.equal(h.clipWrites(), 1);
    h.agent.requestStopTalking('test');
    assert.equal(h.kills(), 1, 'cut at once, not left to drain');
    assert.ok(ackLines().includes('[tool-ack] action=cut why=stop-request'));
    mock.timers.tick(STOP_REQUEST_TOOL_GRACE_MS + 100);
    assert.equal(h.agent.isMuted(), true);
});

test('the model calling mute while the clip plays cuts it off before its "Okay"', () => {
    const h = harness();
    toolUse(h);
    mock.timers.tick(TOOL_ACK_FALLBACK_MS + 200);
    h.a.noteMuteToolRequested();
    assert.equal(h.kills(), 1);
    assert.ok(ackLines().includes('[tool-ack] action=cut why=stop-request'));
});

test('a barge-in kills the clip like any other assistant audio', () => {
    const h = harness();
    toolUse(h);
    mock.timers.tick(TOOL_ACK_FALLBACK_MS + 200);
    h.a.handleInterruption();
    assert.equal(h.kills(), 1);
});

test('a barge-in before the follow-up does not suppress its acknowledgement', () => {
    // The recorded case: the previous answer was spoken over, the barge-in gate
    // stayed shut because Nova opened no block after it, and the next question's
    // lookup went unacknowledged.
    const h = harness();
    audioBlock(h);
    h.a.handleInterruption();
    mock.timers.tick(5000);
    userText(h);
    toolUse(h);
    mock.timers.tick(TOOL_ACK_FALLBACK_MS);
    assert.equal(h.clipWrites(), 1);
});

test('a barge-in after the tool call means somebody is talking, so nothing is played over them', () => {
    const h = harness();
    toolUse(h);
    mock.timers.tick(300);
    h.a.handleInterruption();
    mock.timers.tick(TOOL_ACK_FALLBACK_MS);
    assert.equal(h.clipWrites(), 0);
    assert.match(ackLines()[0], /source=none reason=interrupted/);
});

test('no clip is played while the assistant is still being heard', () => {
    const h = harness();
    audioBlock(h, 8000);
    mock.timers.tick(1000);
    userText(h);
    toolUse(h);
    mock.timers.tick(TOOL_ACK_FALLBACK_MS);
    assert.equal(h.clipWrites(), 0);
    assert.match(ackLines()[0], /source=none reason=playing/);
});

test('without a rendered clip the miss is still recorded', () => {
    const h = harness({ clip: null });
    toolUse(h);
    mock.timers.tick(TOOL_ACK_FALLBACK_MS);
    assert.equal(h.writes.length, 0);
    assert.match(ackLines()[0], /source=none reason=no-clip/);
});

test('a session replaced before the delay plays nothing into the new one', () => {
    const h = harness();
    toolUse(h);
    h.a.session = { isActive: true, queue: [], promptName: 'q', audioContentId: 'b' };
    mock.timers.tick(TOOL_ACK_FALLBACK_MS);
    assert.equal(h.clipWrites(), 0);
    assert.match(ackLines()[0], /source=none reason=session-changed/);
});
