/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The engine path keeps the assistant's voice out of the diarized session by feeding
 * two separate sources, like Stream Audio's two channels. Asserted at the source level,
 * like scribe-recording-source.test.ts.
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

test('the combined stream never reaches the engine; each session has its own source', () => {
    const run = methodBody('private async runMicrovmTranscription');
    assert.doesNotMatch(run, /pushPcm\(event/);
    assert.doesNotMatch(run, /AudioEvent\.AudioChunk/);
    assert.match(run, /channel: 'CALLER'/);
    assert.match(run, /channel: 'AGENT'/);
    assert.match(run, /diarize: false/);
    assert.match(run, /voiceAssistant\.isEnabled\(\)/);
    assert.match(methodBody('private startMeetingAudioFanout'), /meeting_audio\.monitor/);
    assert.match(methodBody('private startMeetingAudioFanout'), /onMeetingPcm/);
    const agent = methodBody('private startAgentAudioCapture');
    assert.match(agent, /agent_output\.monitor/);
    assert.doesNotMatch(agent, /meeting_audio|combined_audio/);
});

test('a pause keeps the sessions and feeds them silence instead of ending the run', () => {
    const run = methodBody('private async runMicrovmTranscription');
    assert.doesNotMatch(run, /!details\.start\) \{\s*break/);
    assert.match(methodBody('private startMeetingAudioFanout'), /Buffer\.alloc\(chunk\.length\)/);
    assert.match(methodBody('private startAgentAudioCapture'), /Buffer\.alloc\(chunk\.length\)/);
});

test('a dead meeting session hands the rest of the meeting to Amazon Transcribe', () => {
    const run = methodBody('private async runMicrovmTranscription');
    assert.match(run, /meetingSession\.hasGivenUp/);
    assert.match(run, /fellBack = true/);
    assert.match(run, /transcribeTimeOffsetSeconds = Math\.max/);
});

test('meeting rows are attributed by start time and drive wake phrases; assistant rows are the VP and do not', () => {
    const meeting = methodBody('private handleMeetingAsrSegment');
    assert.match(meeting, /rosterTimeline\.speakerAt\(segment\.startSec\)/);
    assert.match(meeting, /processTranscriptResult\(/);
    const agent = methodBody('private handleAgentAsrSegment');
    assert.match(agent, /channel: 'AGENT'/);
    assert.match(agent, /lmaIdentity/);
    assert.match(agent, /translator/);
    assert.doesNotMatch(agent, /processTranscriptResult/);
    assert.match(methodBody('async speakerChange'), /rosterTimeline\.record\(/);
    assert.match(methodBody('private teardownSessionProcesses'), /agentAudioProcess/);
});
