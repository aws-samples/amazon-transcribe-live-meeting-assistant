/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * With a voice assistant, the Amazon Transcribe path sends one two-channel
 * stream: the participants on ch_0 and the assistant on ch_1. The mapping and
 * the interleaving are tested behaviourally; the wiring in scribe.ts at the
 * source level, like scribe-asr-channels.test.ts.
 */
import { strict as assert } from 'node:assert';
import { test } from 'node:test';
import { readFileSync } from 'node:fs';
import {
    ASSISTANT_CHANNEL_ID,
    MEETING_CHANNEL_ID,
    PcmQueue,
    StereoInterleaver,
    assistantSegmentFromResult,
    isOwnIdentity,
    resultChannel,
    writesAssistantChannel,
} from './transcribe-channels.js';
import { MAX_AUDIO_CHUNK_BYTES } from './scribe.js';

const scribe = readFileSync(
    new URL('./scribe.ts', import.meta.url).pathname.replace('/dist/', '/src/'),
    'utf8',
);

function methodBody(name: string): string {
    const start = scribe.indexOf(name);
    assert.notEqual(start, -1, `${name} not found in scribe.ts`);
    const rest = scribe.slice(start);
    const end = rest.slice(1).search(/\n {4}(private|public|async|\/\*\*)/);
    return end === -1 ? rest : rest.slice(0, end + 1);
}

function pcm(...samples: number[]): Buffer {
    const buf = Buffer.alloc(samples.length * 2);
    samples.forEach((sample, i) => buf.writeInt16LE(sample, i * 2));
    return buf;
}

function frames(buf: Buffer): Array<[number, number]> {
    const out: Array<[number, number]> = [];
    for (let i = 0; i + 4 <= buf.length; i += 4) {
        out.push([buf.readInt16LE(i), buf.readInt16LE(i + 2)]);
    }
    return out;
}

function result(channelId: string | undefined, text: string, isPartial = false, id = 'r1'): any {
    return {
        ResultId: id,
        ChannelId: channelId,
        IsPartial: isPartial,
        StartTime: 1.25,
        EndTime: 2.5,
        Alternatives: [{ Transcript: text, Items: [] }],
    };
}

// --- Channel mapping ---------------------------------------------------------

test('ch_0 is the participants and ch_1 the assistant, the same ids as Stream Audio', () => {
    assert.equal(MEETING_CHANNEL_ID, 'ch_0');
    assert.equal(ASSISTANT_CHANNEL_ID, 'ch_1');
    assert.equal(resultChannel(result('ch_0', 'hello'), true), 'meeting');
    assert.equal(resultChannel(result('ch_1', 'hello'), true), 'assistant');
    // A result with no channel id is the participants', as in the transcriber stack.
    assert.equal(resultChannel(result(undefined, 'hello'), true), 'meeting');
});

test('a mono stream has only the participants, whatever a result claims', () => {
    assert.equal(resultChannel(result('ch_1', 'hello'), false), 'meeting');
});

test('the assistant channel is written in every mode except translator', () => {
    assert.equal(writesAssistantChannel('normal'), true);
    assert.equal(writesAssistantChannel('group'), true);
    assert.equal(writesAssistantChannel(undefined), true);
    assert.equal(writesAssistantChannel('translator'), false);
});

test('an assistant result becomes an AGENT row under the VP, on the meeting timeline', () => {
    const partial = assistantSegmentFromResult(result('ch_1', 'Searching for the weather', true), 30, 'LMA Bot');
    const final = assistantSegmentFromResult(result('ch_1', 'Searching for the weather in Herndon.', false), 30, 'LMA Bot');
    assert.ok(partial && final);
    assert.equal(final.channel, 'AGENT');
    assert.equal(final.speaker, 'LMA Bot');
    assert.equal(final.startTime, 31.25);
    assert.equal(final.endTime, 32.5);
    assert.equal(final.transcript, 'Searching for the weather in Herndon.');
    assert.equal(partial.isPartial, true);
    assert.equal(final.isPartial, false);
    // The final updates the partial's row, not a new one.
    assert.equal(partial.segmentId, final.segmentId);
});

test('assistant rows from different sessions never share an id', () => {
    const first = assistantSegmentFromResult(result('ch_1', 'Hello', false, 'same'), 0, 'LMA');
    const second = assistantSegmentFromResult(result('ch_1', 'Hello', false, 'same'), 120, 'LMA');
    assert.notEqual(first?.segmentId, second?.segmentId);
});

test('an assistant result with no text writes nothing', () => {
    assert.equal(assistantSegmentFromResult(result('ch_1', '   '), 0, 'LMA'), null);
});

test('an assistant result without result-level times uses its items', () => {
    const segment = assistantSegmentFromResult(
        {
            ResultId: 'r9',
            ChannelId: 'ch_1',
            IsPartial: false,
            Alternatives: [
                {
                    Transcript: 'One two.',
                    Items: [
                        { Type: 'pronunciation', Content: 'One', StartTime: 4, EndTime: 4.3 },
                        { Type: 'pronunciation', Content: 'two', StartTime: 4.4, EndTime: 4.8 },
                        { Type: 'punctuation', Content: '.' },
                    ],
                },
            ],
        },
        10,
        'LMA',
    );
    assert.equal(segment?.startTime, 14);
    assert.equal(segment?.endTime, 14.8);
});

test('the VP is recognised by either of its identities', () => {
    assert.equal(isOwnIdentity('LMA Bot', ['LMA Bot', 'Scribe']), true);
    assert.equal(isOwnIdentity(' Scribe ', ['LMA Bot', 'Scribe']), true);
    assert.equal(isOwnIdentity('Alice', ['LMA Bot', 'Scribe']), false);
    assert.equal(isOwnIdentity('', ['', 'Scribe']), false);
});

// --- Interleaving ------------------------------------------------------------

test('two mono captures interleave as left = participants, right = assistant', () => {
    const stereo = new StereoInterleaver();
    assert.equal(stereo.pushLeft(pcm(1, 2, 3)).length, 0, 'nothing is sent until the other side has audio');
    const out = stereo.pushRight(pcm(-1, -2));
    assert.deepEqual(frames(out), [[1, -1], [2, -2]]);
    assert.deepEqual(frames(stereo.pushRight(pcm(-3, -4))), [[3, -3]]);
    assert.deepEqual(frames(stereo.pushLeft(pcm(4))), [[4, -4]]);
});

test('output is whole stereo frames even when a pipe splits a sample', () => {
    const stereo = new StereoInterleaver();
    const left = pcm(100, 200);
    const right = pcm(-100, -200);
    let out = Buffer.concat([stereo.pushLeft(left.subarray(0, 3)), stereo.pushRight(right.subarray(0, 1))]);
    assert.equal(out.length % 4, 0);
    out = Buffer.concat([out, stereo.pushLeft(left.subarray(3)), stereo.pushRight(right.subarray(1))]);
    assert.deepEqual(frames(out), [[100, -100], [200, -200]]);
});

test('a silent side never holds up the other for longer than the skew allowance', () => {
    // 10 ms allowance = 160 samples at 16 kHz.
    const stereo = new StereoInterleaver(10);
    const left = pcm(...Array.from({ length: 400 }, (_, i) => i + 1));
    const out = frames(stereo.pushLeft(left));
    // Everything beyond the allowance goes out at once, paired with silence.
    assert.equal(out.length, 240);
    assert.deepEqual(out[0], [1, 0]);
    assert.deepEqual(out[239], [240, 0]);
    assert.equal(stereo.paddedRightSamples, 240);
    // Nothing is dropped: the held samples follow once the other side resumes.
    const resumed = frames(stereo.pushRight(pcm(...Array.from({ length: 160 }, () => 7))));
    assert.equal(resumed.length, 160);
    assert.deepEqual(resumed[0], [241, 7]);
    assert.deepEqual(resumed[159], [400, 7]);
});

test('the assistant side is not held up by a stalled participants capture either', () => {
    const stereo = new StereoInterleaver(10);
    const out = frames(stereo.pushRight(pcm(...Array.from({ length: 200 }, () => 5))));
    assert.equal(out.length, 40);
    assert.deepEqual(out[0], [0, 5]);
    assert.equal(stereo.paddedLeftSamples, 40);
});

test('frames are capped at a whole number of stereo samples', () => {
    assert.equal(MAX_AUDIO_CHUNK_BYTES % 4, 0);
});

test('the queue hands chunks to the reader in order and finishes on end', async () => {
    const queue = new PcmQueue();
    queue.push(pcm(1));
    queue.push(Buffer.alloc(0));
    const read: number[] = [];
    const reader = (async () => {
        for await (const chunk of queue) {
            read.push(chunk.readInt16LE(0));
        }
    })();
    await new Promise((resolve) => setImmediate(resolve));
    queue.push(pcm(2));
    queue.push(pcm(3));
    queue.end();
    queue.push(pcm(4));
    await reader;
    assert.deepEqual(read, [1, 2, 3]);
});

// --- Wiring in scribe.ts -----------------------------------------------------

test('the two-channel stream is used exactly when a voice assistant is configured', () => {
    const start = methodBody('async startTranscription');
    assert.match(start, /const stereo = voiceAssistant\.isEnabled\(\)/);
    assert.match(start, /this\.audioStream\(recordingStream, stereo\)/);
    assert.match(start, /if \(stereo\) \{\s*\/\/[^\n]*\n\s*transcriptionParams\.EnableChannelIdentification = true;\s*transcriptionParams\.NumberOfChannels = 2;/);
    assert.match(start, /this\.handleTranscriptEvents\(response, stereo\)/);
    assert.match(start, /this\.writeAudio\(response, stereo\)/);
    // One stream, not one per channel.
    assert.equal(start.match(/new StartStreamTranscriptionCommand/g)?.length, 1);
});

test('the channels come from the meeting-only and assistant-only sources; the recording stays combined', () => {
    const stream = methodBody('private async *audioStream');
    assert.match(stream, /combined_audio\.monitor/);
    assert.match(stream, /recordingStream\.write\(chunk\)/);
    assert.match(stream, /this\.startMeetingAudioFanout\(\(chunk\) => queue\.push\(stereoPcm\.pushLeft\(chunk\)\)\)/);
    assert.match(stream, /this\.startAgentAudioCapture\(\(chunk\) => queue\.push\(stereoPcm\.pushRight\(chunk\)\)\)/);
    // The fan-out is started once per session: by audioStream on two channels,
    // by writeAudio on one.
    assert.match(methodBody('private async writeAudio'), /if \(!stereo\) \{\s*this\.startMeetingAudioFanout\(\);/);
    assert.match(methodBody('private teardownSessionProcesses'), /this\.stereoQueue\.end\(\)/);
});

test('assistant-channel results become VP rows and never drive wake phrases or stop requests', () => {
    const events = methodBody('private async handleTranscriptEvents');
    const branch = events.indexOf("resultChannel(result, stereo) === 'assistant'");
    assert.notEqual(branch, -1);
    const skip = events.indexOf('continue;', branch);
    const wake = events.indexOf('this.processTranscriptResult(result)');
    assert.ok(skip > branch && skip < wake, 'the assistant branch ends before wake-phrase processing');
    const assistant = methodBody('private handleAssistantChannelResult');
    assert.match(assistant, /assistantSegmentFromResult\(result, sessionOffsetSeconds, this\.vpName\(\)\)/);
    assert.match(assistant, /sendAsrTranscriptSegment\(segment\)/);
    assert.doesNotMatch(assistant, /processTranscriptResult|sendAddTranscriptSegment|requestStopTalking/);
});

test('participant rows on two channels are never named after the VP', () => {
    const events = methodBody('private async handleTranscriptEvents');
    assert.match(events, /const speaker = stereo \? currentParticipantSpeaker : currentSpeaker/);
    assert.match(events, /sendAddTranscriptSegment\(\s*speaker,/);
    assert.match(methodBody('async speakerChange'), /!isOwnIdentity\(speaker, this\.ownIdentities\(\)\)/);
});
