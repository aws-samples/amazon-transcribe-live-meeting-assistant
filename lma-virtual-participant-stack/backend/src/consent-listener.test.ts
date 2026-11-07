/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The consent listener (consent-listener.ts) against fake engines, and the
 * real engine adapters in scribe.ts read from the source.
 *
 * Not covered: a live meeting on either engine. The end-to-end check that
 * nothing reaches the Kinesis writer before consent is granted is in
 * meeting-consent.test.ts, where the flow drives the listener.
 */
import { strict as assert } from 'node:assert';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import { ConsentListener, HeardResult, ListenerEngines } from './consent-listener.js';
import { noteParticipantSeen } from './consent-roster.js';
import type { SpokenUtterance } from './spoken-replies.js';
import type { AsrEngineName } from './asr-microvm-client.js';

/** One second of mono 16 kHz PCM16. */
const SECOND = 32000;

interface FakeState {
    opened: string[];
    closed: number;
    pushed: number;
    onFinal: ((result: HeardResult) => void) | null;
    onEnded: (() => void) | null;
    feed: ((pcm: Buffer) => void) | null;
    capturing: boolean;
}

function fakeEngines(engine: AsrEngineName, options: { microvmAvailable?: boolean } = {}): { engines: ListenerEngines; state: FakeState } {
    const state: FakeState = { opened: [], closed: 0, pushed: 0, onFinal: null, onEnded: null, feed: null, capturing: false };
    const open = (kind: string) => async (onFinal: (result: HeardResult) => void, onEnded: () => void) => {
        state.opened.push(kind);
        state.onFinal = onFinal;
        state.onEnded = onEnded;
        return {
            push: (pcm: Buffer) => {
                state.pushed += pcm.length;
            },
            close: async () => {
                state.closed += 1;
            },
        };
    };
    const engines: ListenerEngines = {
        resolveEngine: async () => engine,
        openTranscribe: open('transcribe'),
        openMicrovm: options.microvmAvailable === false ? async () => null : open('microvm'),
        captureMeetingAudio: (onPcm) => {
            state.capturing = true;
            state.feed = onPcm;
            return () => {
                state.capturing = false;
                state.feed = null;
            };
        },
    };
    return { engines, state };
}

async function until(condition: () => boolean): Promise<void> {
    for (let i = 0; i < 200 && !condition(); i += 1) await new Promise((resolve) => setImmediate(resolve));
    assert.ok(condition(), 'timed out');
}

function listenerWith(engines: ListenerEngines, heard: SpokenUtterance[], currentSpeaker = 'none'): ConsentListener {
    return new ConsentListener({
        engines,
        ownIdentities: ['LMA (Ana Silva)', 'LMA'],
        lagSeconds: () => 0,
        currentSpeaker: () => currentSpeaker,
        onHeard: (utterance) => heard.push(utterance),
        sleep: async () => undefined,
        healthySessionMs: 0,
    });
}

for (const engine of ['transcribe', 'microvm'] as const) {
    test(`the listener streams the meeting audio to the meeting's engine and attributes each final to the speaker shown (${engine})`, async () => {
        const heard: SpokenUtterance[] = [];
        const { engines, state } = fakeEngines(engine);
        const listener = listenerWith(engines, heard, 'Ana Silva');
        listener.start();
        await until(() => state.capturing);
        assert.deepEqual(state.opened, [engine]);
        assert.equal(listener.engine, engine);

        state.feed!(Buffer.alloc(2 * SECOND));
        noteParticipantSeen('Bo Chen'); // shown speaking from 2 s on this session's clock
        state.feed!(Buffer.alloc(2 * SECOND));
        assert.equal(state.pushed, 4 * SECOND);

        state.onFinal!({ text: 'I consent', startSec: 0.5, endSec: 1.2 });
        state.onFinal!({ text: 'I object', startSec: 2.4, endSec: 3.0 });
        assert.deepEqual(heard, [
            { speaker: 'Ana Silva', text: 'I consent' },
            { speaker: 'Bo Chen', text: 'I object' },
        ]);
        await listener.stop();
        assert.equal(state.closed, 1);
        assert.equal(state.capturing, false);
    });
}

test('with no MicroVM engine session, the listener uses Amazon Transcribe', async () => {
    const { engines, state } = fakeEngines('microvm', { microvmAvailable: false });
    const listener = listenerWith(engines, []);
    listener.start();
    await until(() => state.capturing);
    assert.deepEqual(state.opened, ['transcribe']);
    assert.equal(listener.engine, 'transcribe');
    await listener.stop();
});

test('a reply heard before anybody was shown speaking has no speaker', async () => {
    const heard: SpokenUtterance[] = [];
    const { engines, state } = fakeEngines('transcribe');
    const listener = listenerWith(engines, heard);
    listener.start();
    await until(() => state.capturing);
    state.onFinal!({ text: 'I consent', startSec: 0, endSec: 1 });
    assert.deepEqual(heard, [{ speaker: null, text: 'I consent' }]);
    await listener.stop();
});

test("the VP's own name shown speaking is never a reply's speaker", async () => {
    const heard: SpokenUtterance[] = [];
    const { engines, state } = fakeEngines('transcribe');
    const listener = listenerWith(engines, heard, 'LMA');
    listener.start();
    await until(() => state.capturing);
    state.onFinal!({ text: 'I object', startSec: 0, endSec: 1 });
    assert.deepEqual(heard, [{ speaker: null, text: 'I object' }]);
    await listener.stop();
});

test('a session that ends on its own is reopened; after stop nothing more is reported', async () => {
    const heard: SpokenUtterance[] = [];
    const { engines, state } = fakeEngines('transcribe');
    const listener = listenerWith(engines, heard);
    listener.start();
    await until(() => state.capturing);
    const first = state.onFinal!;
    state.onEnded!();
    await until(() => state.opened.length === 2 && state.capturing);
    first({ text: 'I consent', startSec: 0, endSec: 1 });
    assert.deepEqual(heard, [], 'a closed session reports nothing');

    const second = state.onFinal!;
    await listener.stop();
    second({ text: 'I consent', startSec: 0, endSec: 1 });
    assert.deepEqual(heard, []);
    assert.equal(listener.listening, false);
    assert.equal(state.capturing, false);
});

test('listening stops after repeated failures to open a session, until it is asked for again', async () => {
    const opened: string[] = [];
    const engines: ListenerEngines = {
        resolveEngine: async () => 'transcribe',
        openTranscribe: async () => {
            opened.push('transcribe');
            return null;
        },
        openMicrovm: async () => null,
        captureMeetingAudio: () => () => undefined,
    };
    const listener = new ConsentListener({ engines, ownIdentities: [], lagSeconds: () => 0, sleep: async () => undefined, maxFailures: 3 });
    listener.start();
    await until(() => !listener.listening);
    assert.equal(opened.length, 3);
    listener.start();
    await until(() => opened.length === 6);
    await listener.stop();
});

// ─── The real engine adapters, read from scribe.ts ──────────────────────────

const scribe = readFileSync(new URL('./scribe.ts', import.meta.url).pathname.replace('/dist/', '/src/'), 'utf8');

function methodBody(name: string): string {
    const start = scribe.indexOf(name);
    assert.notEqual(start, -1, `${name} not found in scribe.ts`);
    const rest = scribe.slice(start);
    const end = rest.slice(1).search(/\n {4}(private|public|async|get |consentListenerEngines|\/\*\*)/);
    return end === -1 ? rest : rest.slice(0, end + 1);
}

test('the listening sessions hand on final results only, and write nothing anywhere', () => {
    const transcribe = methodBody('private openListenerTranscribe');
    assert.match(transcribe, /if \(result\.IsPartial\) \{\s*continue;/);
    const microvm = methodBody('private async openListenerMicrovm');
    assert.match(microvm, /if \(!segment\.isPartial\) \{\s*onFinal\(/);
    const engines = methodBody('consentListenerEngines(): ListenerEngines');
    for (const body of [transcribe, microvm, engines]) {
        assert.doesNotMatch(body, /send\w*Segment|sendRecord|kinesisStreamManager\.send|recordingStream|meetingToCombinedPipe|voiceAssistant|processTranscriptResult/);
        assert.doesNotMatch(body, /log\.\w+\([^)]*\btext\b/, 'no log line carries what was heard');
    }
    // Meeting audio only: the voice assistant's voice is on agent_output.
    assert.match(engines, /spawnPcmCapture\('meeting_audio\.monitor', 'consent listening'\)/);
});

test('a MicroVM acquired for listening is handed to transcription, and released when it is not taken', () => {
    // Both acquire under one lock, so they never both launch one.
    const once = methodBody('private async runMicrovmTranscriptionOnce');
    assert.match(once, /await this\.withLeaseLock\(async \(\) => \{[\s\S]*?const pending = this\.pendingLease;[\s\S]*?pending \?\? \(await acquireLease\(callId\)\)/);
    assert.match(methodBody('private async openListenerMicrovm'), /await this\.withLeaseLock\(async \(\) => \{[\s\S]*?this\.microvmLease \?\? this\.pendingLease/);
    // Released after any acquisition still under way, when nobody took it.
    assert.match(methodBody('private releasePendingLease'), /return this\.withLeaseLock\(/);
    assert.match(methodBody('async stopTranscription'), /this\.meetingOver = true;\s*await this\.releasePendingLease\(\);/);
    assert.match(methodBody('async startTranscription'), /if \(engine !== 'microvm'\) \{[\s\S]*?await this\.releasePendingLease\(\);/);
});

test('live final participant results reach the consent flow before they are written, the assistant channel never', () => {
    const transcribe = methodBody('private async handleTranscriptEvents');
    const noted = transcribe.indexOf('this.noteSpokenResult(result');
    assert.ok(noted > 0 && noted < transcribe.indexOf('sendAddTranscriptSegment('), 'read before the result is written');
    assert.ok(transcribe.indexOf("resultChannel(result, stereo) === 'assistant'") < noted, 'after the assistant channel is set aside');
    const microvm = methodBody('private handleMeetingAsrSegment');
    assert.ok(microvm.indexOf('noteSpokenUtterance(') < microvm.indexOf('sendAsrTranscriptSegment('));
    assert.doesNotMatch(methodBody('private handleAgentAsrSegment'), /noteSpokenUtterance/);
    assert.doesNotMatch(methodBody('private handleAssistantChannelResult'), /noteSpokenUtterance|noteSpokenResult/);
});
