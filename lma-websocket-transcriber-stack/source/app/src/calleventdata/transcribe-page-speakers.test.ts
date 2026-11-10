/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * How Amazon Transcribe rows on a diarized meeting-audio channel are named:
 * driven through the real segment writer, with only the Kinesis client's
 * `send` replaced so the records can be read back.
 */
process.env['AWS_REGION'] = process.env['AWS_REGION'] || 'us-east-1';

import assert from 'node:assert/strict';
import { afterEach, beforeEach, test } from 'node:test';
import { FastifyInstance } from 'fastify';
import { KinesisClient } from '@aws-sdk/client-kinesis';
import { TranscriptEvent } from '@aws-sdk/client-transcribe-streaming';

import { writeTranscriptionSegment } from './transcribe';
import { CallMetaData } from './eventtypes';
import { pageSpeakersFor } from './speaker-fusion';

const fakeServer = {
    log: { info: () => undefined, warn: () => undefined, error: () => undefined, debug: () => undefined },
} as unknown as FastifyInstance;

const written: Array<{ Speaker: string; Channel: string }> = [];
const originalSend = KinesisClient.prototype.send;

beforeEach(() => {
    written.length = 0;
    KinesisClient.prototype.send = (async (command: { input: { Data: Buffer } }) => {
        written.push(JSON.parse(command.input.Data.toString()));
        return {};
    }) as unknown as typeof KinesisClient.prototype.send;
});

afterEach(() => {
    KinesisClient.prototype.send = originalSend;
});

const meeting = (): CallMetaData =>
    ({
        callId: `page-speakers-${Math.random()}`,
        callEvent: 'START',
        activeSpeaker: 'Other Participant',
        agentId: 'Alex',
        samplingRate: 16000,
        channels: {},
    }) as CallMetaData;

let resultCount = 0;
/** One final Transcribe result on the meeting-audio channel: one voice, words spread over [start, end]. */
const final = (start: number, end: number, voice: string): TranscriptEvent => {
    resultCount += 1;
    const words = 'one two three four five six'.split(' ');
    const step = (end - start) / words.length;
    return {
        Transcript: {
            Results: [
                {
                    ResultId: `res-${resultCount}`,
                    IsPartial: false,
                    ChannelId: 'ch_0',
                    StartTime: start,
                    EndTime: end,
                    Alternatives: [
                        {
                            Transcript: words.join(' '),
                            Items: words.map((Content, i) => ({
                                Content,
                                Type: 'pronunciation',
                                StartTime: start + i * step,
                                EndTime: start + (i + 1) * step,
                                Speaker: voice,
                            })),
                        },
                    ],
                },
            ],
        },
    };
};

const speakersOf = async (callMetadata: CallMetaData, results: TranscriptEvent[]): Promise<string[]> => {
    for (const result of results) {
        await writeTranscriptionSegment(result, callMetadata, fakeServer, 0, { diarizeSystemChannel: true });
    }
    return written.map((row) => row.Speaker);
};

test('a diarized meeting-audio row is named by the page during its audio, and by a proven voice', async () => {
    process.env['SPEAKER_CHANGE_LAG_SECONDS'] = '0';
    const callMetadata = meeting();
    const page = pageSpeakersFor(callMetadata);
    page.record(0, 'Ana Ruiz');
    page.record(30, 'Sam Lee');
    // What the client says NOW; every row below is written after this.
    callMetadata.activeSpeaker = 'Sam Lee';
    const speakers = await speakersOf(callMetadata, [
        ...[0, 6, 12, 18, 24].map((t) => final(t, t + 5, '0')),
        final(30, 48, '1'),
        // Ana's short reply while the page still shows Sam.
        final(48.3, 49.8, '0'),
    ]);
    delete process.env['SPEAKER_CHANGE_LAG_SECONDS'];
    assert.deepEqual(speakers, [
        ...Array(5).fill('Ana Ruiz (spk_0)'),
        'Sam Lee (spk_1)',
        'Ana Ruiz (spk_0)',
    ]);
});

test('a client that reports no speakers gets the rows it always got', async () => {
    const callMetadata = meeting();
    const speakers = await speakersOf(callMetadata, [
        ...[0, 6, 12, 18, 24].map((t) => final(t, t + 5, '0')),
        final(30, 34, '1'),
    ]);
    assert.deepEqual(speakers, [...Array(5).fill('Other Participant (spk_0)'), 'Other Participant (spk_1)']);
});
