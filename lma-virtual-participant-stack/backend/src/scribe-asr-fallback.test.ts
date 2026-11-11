/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * A MicroVM released before its session is finished keeps reconnecting against
 * nothing. Asserted at the source level, like scribe-recording-source.test.ts.
 */
import { strict as assert } from 'node:assert';
import { test } from 'node:test';
import { readFileSync } from 'node:fs';

const src = readFileSync(
    new URL('./scribe.ts', import.meta.url).pathname.replace('/dist/', '/src/'),
    'utf8',
);

function methodBody(name: string): string {
    const start = src.indexOf(name);
    assert.notEqual(start, -1, `${name} not found in scribe.ts`);
    const rest = src.slice(start);
    const end = rest.slice(1).search(/\n {4}(private|public|async|\/\*\*)/);
    return end === -1 ? rest : rest.slice(0, end + 1);
}

test('every MicroVM release in the ASR path is preceded by finishing the sessions', () => {
    const body = methodBody('private async runMicrovmTranscriptionOnce');
    const releases = [...body.matchAll(/this\.releaseMicrovm\(meetingSession\)/g)].map((m) => m.index ?? -1);
    assert.ok(releases.length >= 2, 'expected the not-ready branch and the normal end to release');
    let from = 0;
    for (const at of releases) {
        const before = body.slice(from, at);
        assert.match(before, /finishMicrovmSessions\(\)/, `a release at offset ${at} is not preceded by finishMicrovmSessions()`);
        from = at;
    }
});

test('stopping transcription finishes every engine session', () => {
    assert.match(methodBody('async stopTranscription'), /finishMicrovmSessions\(\)/);
});

test('the meeting is captured from before the MicroVM is acquired, not from when it is ready', () => {
    const body = methodBody('private async runMicrovmTranscriptionOnce');
    const capture = body.indexOf('this.startMeetingAudioFanout(');
    const recording = body.indexOf('this.audioStream(recordingStream)');
    const acquire = body.indexOf('acquireLease(callId)');
    assert.ok(capture !== -1 && recording !== -1 && acquire !== -1);
    assert.ok(capture < acquire, 'the engine feed starts before the acquisition');
    assert.ok(recording < acquire, 'the recording starts with it, so the two clocks agree');
});

test('a run still starting its MicroVM finishes its own sessions, and is waited for longer', () => {
    const stop = methodBody('async stopTranscription');
    assert.match(stop, /if \(!starting\) \{\s*await this\.finishMicrovmSessions\(\);/);
    assert.match(stop, /starting \? ASR_STOP_STARTING_WAIT_MS : ASR_STOP_RELEASE_TIMEOUT_MS/);
    const run = methodBody('private async runMicrovmTranscriptionOnce');
    assert.match(run, /if \(!this\.isTranscribing\) \{[\s\S]*?finishMicrovmSessions\(\)[\s\S]*?releaseMicrovm\(meetingSession\)/);
});
