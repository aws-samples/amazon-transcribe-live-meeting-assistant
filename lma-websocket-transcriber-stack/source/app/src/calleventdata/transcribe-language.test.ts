/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The identified language of an Amazon Transcribe result, on the
 * ADD_TRANSCRIPT_SEGMENT records written for it.
 *
 * Drives the real writer: only the Kinesis client's `send` is replaced, so the
 * record checked is the one the shipping code builds.
 */
import assert from 'node:assert/strict';
import { afterEach, beforeEach, test, mock } from 'node:test';

import { KinesisClient } from '@aws-sdk/client-kinesis';
import type { FastifyInstance } from 'fastify';
import type { TranscriptEvent } from '@aws-sdk/client-transcribe-streaming';

import { resultLanguageCode, writeTranscriptionSegment } from './transcribe';
import type { CallMetaData } from './eventtypes';

const quietServer = {
    log: { debug: () => {}, info: () => {}, warn: () => {}, error: () => {} },
} as unknown as FastifyInstance;

type SentRecord = { EventType?: string; Channel?: string; Transcript?: string; LanguageCode?: string };

let sent: SentRecord[] = [];

beforeEach(() => {
    sent = [];
    mock.method(KinesisClient.prototype, 'send', async (command: { input: { Data: Buffer } }) => {
        sent.push(JSON.parse(Buffer.from(command.input.Data).toString('utf8')));
        return {};
    });
});

afterEach(() => {
    mock.restoreAll();
});

const meeting = (): CallMetaData =>
    ({
        callId: 'language-test-call',
        samplingRate: 16000,
        callEvent: 'START',
        activeSpeaker: 'Alice',
        agentId: 'Bob',
    }) as unknown as CallMetaData;

const event = (languageCode?: string, channelId = 'ch_0'): TranscriptEvent => ({
    Transcript: {
        Results: [
            {
                ResultId: 'r1',
                ChannelId: channelId,
                IsPartial: false,
                StartTime: 0.5,
                EndTime: 1.5,
                ...(languageCode === undefined ? {} : { LanguageCode: languageCode as never }),
                Alternatives: [
                    {
                        Transcript: 'hola a todos',
                        Items: [
                            { Content: 'hola', Type: 'pronunciation', StartTime: 0.5, EndTime: 0.8 },
                            { Content: 'a', Type: 'pronunciation', StartTime: 0.8, EndTime: 0.9 },
                            { Content: 'todos', Type: 'pronunciation', StartTime: 0.9, EndTime: 1.5 },
                        ],
                    },
                ],
            },
        ],
    },
});

test('the identified language of a result is sent on its segment', async () => {
    await writeTranscriptionSegment(event('es-US'), meeting(), quietServer);

    assert.equal(sent.length, 1);
    assert.equal(sent[0].EventType, 'ADD_TRANSCRIPT_SEGMENT');
    assert.equal(sent[0].LanguageCode, 'es-US');
    assert.equal(sent[0].Transcript, 'hola a todos');
});

test('a result with no identified language sends a segment without the field', async () => {
    await writeTranscriptionSegment(event(undefined), meeting(), quietServer);

    assert.equal(sent.length, 1);
    assert.equal('LanguageCode' in sent[0], false);
});

test('the microphone channel carries its own result language', async () => {
    await writeTranscriptionSegment(event('en-US', 'ch_1'), meeting(), quietServer);

    assert.equal(sent[0].Channel, 'LOCAL');
    assert.equal(sent[0].LanguageCode, 'en-US');
});

test('a blank language code is treated as none', () => {
    assert.equal(resultLanguageCode({ LanguageCode: '  ' as never }), undefined);
    assert.equal(resultLanguageCode({}), undefined);
    assert.equal(resultLanguageCode({ LanguageCode: 'hi-IN' as never }), 'hi-IN');
});
