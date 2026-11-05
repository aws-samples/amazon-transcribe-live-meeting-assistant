/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The capture context a meeting's record is given: the capture source from the
 * START's clientType, and the engine, language and speaker separation it is
 * transcribed with -- on START, and on UPDATE_CAPTURE_CONTEXT.
 */
import assert from 'node:assert/strict';
import test from 'node:test';
import { FastifyInstance } from 'fastify';
import { KinesisClient } from '@aws-sdk/client-kinesis';

import {
    captureSourceFor,
    speakerSeparationFor,
    transcriptionContextFor,
    transcriptionEngineName,
} from './capture-context';
import { CallMetaData } from './eventtypes';
import { sessionSettingsFrom } from './session-settings';
import { writeCallStartEvent, writeCaptureContextEvent } from './transcribe';

const fakeServer = {
    log: {
        info: () => undefined,
        warn: () => undefined,
        error: () => undefined,
        debug: () => undefined,
    },
} as unknown as FastifyInstance;

const callMetaData = {
    callId: 'Weekly sync - 2026-11-05-10:00:00.000',
    fromNumber: 'Other Participant',
    toNumber: 'System',
    agentId: 'ana@example.com',
    samplingRate: 48000,
    callEvent: 'START',
    activeSpeaker: 'n/a',
    channels: {},
    accessToken: 'access',
} as CallMetaData;

const transcription = (values: Record<string, unknown>) => sessionSettingsFrom(values, {}).transcription;

test('each client the streaming API documents names its own capture source', () => {
    assert.equal(captureSourceFor('STREAM_AUDIO'), 'STREAM_AUDIO');
    assert.equal(captureSourceFor('BROWSER_EXTENSION'), 'BROWSER_EXTENSION');
    assert.equal(captureSourceFor('DESKTOP_APP'), 'DESKTOP_APP');
    assert.equal(captureSourceFor(' desktop_app '), 'DESKTOP_APP');
});

test('a client that names no type, or one this server does not know, is API', () => {
    assert.equal(captureSourceFor(undefined), 'API');
    assert.equal(captureSourceFor(''), 'API');
    assert.equal(captureSourceFor(7), 'API');
    assert.equal(captureSourceFor('MCP_CLIENT'), 'API');
    // Written by their own START events, never through the streaming API.
    assert.equal(captureSourceFor('VIRTUAL_PARTICIPANT'), 'API');
    assert.equal(captureSourceFor('UPLOAD'), 'API');
});

test('the engine is named as the meeting record stores it', () => {
    assert.equal(transcriptionEngineName('transcribe'), 'TRANSCRIBE');
    assert.equal(transcriptionEngineName('microvm'), 'MICROVM_ASR');
});

test('speaker separation names the channels it was asked for', () => {
    assert.equal(speakerSeparationFor({ diarizeSystemChannel: true, diarizeMicChannel: true }), 'BOTH');
    assert.equal(speakerSeparationFor({ diarizeSystemChannel: true, diarizeMicChannel: false }), 'REMOTE');
    assert.equal(speakerSeparationFor({ diarizeMicChannel: true }), 'LOCAL');
    assert.equal(speakerSeparationFor({}), 'NONE');
});

test('Amazon Transcribe records the language setting it was started with', () => {
    assert.deepEqual(transcriptionContextFor('transcribe', {}, transcription({ languageCode: 'es-US' })), {
        TranscriptionEngine: 'TRANSCRIBE',
        TranscriptionLanguage: 'es-US',
        SpeakerSeparation: 'NONE',
    });
    assert.equal(
        transcriptionContextFor('transcribe', {}, transcription({ languageCode: 'identify-language' }))
            .TranscriptionLanguage,
        'identify-language'
    );
});

test('the MicroVM records no language, since its model sets it', () => {
    assert.deepEqual(
        transcriptionContextFor('microvm', { diarizeSystemChannel: true }, transcription({ languageCode: 'en-US' })),
        { TranscriptionEngine: 'MICROVM_ASR', SpeakerSeparation: 'REMOTE' }
    );
});

const capturedEvents = (t: { mock: { method: typeof test.mock.method } }) => {
    const events: Array<Record<string, unknown>> = [];
    t.mock.method(KinesisClient.prototype, 'send', async (command: { input: { Data: Buffer } }) => {
        events.push(JSON.parse(command.input.Data.toString()));
        return {};
    });
    return events;
};

test('START carries the capture context it is given', async (t) => {
    const events = capturedEvents(t);
    await writeCallStartEvent(callMetaData, fakeServer, {
        CaptureSource: 'DESKTOP_APP',
        TranscriptionEngine: 'TRANSCRIBE',
        TranscriptionLanguage: 'en-US',
        SpeakerSeparation: 'BOTH',
    });
    assert.equal(events.length, 1);
    assert.equal(events[0]['EventType'], 'START');
    assert.equal(events[0]['CaptureSource'], 'DESKTOP_APP');
    assert.equal(events[0]['TranscriptionEngine'], 'TRANSCRIBE');
    assert.equal(events[0]['TranscriptionLanguage'], 'en-US');
    assert.equal(events[0]['SpeakerSeparation'], 'BOTH');
});

test('START without a capture context is the event it always was', async (t) => {
    const events = capturedEvents(t);
    await writeCallStartEvent(callMetaData, fakeServer);
    assert.deepEqual(
        Object.keys(events[0]).filter((key) => /Capture|Transcription|Speaker/.test(key)),
        []
    );
});

test('UPDATE_CAPTURE_CONTEXT carries the transcription fields for the meeting', async (t) => {
    const events = capturedEvents(t);
    await writeCaptureContextEvent(
        callMetaData,
        transcriptionContextFor('transcribe', { diarizeMicChannel: true }, transcription({ languageCode: 'en-GB' })),
        fakeServer
    );
    assert.deepEqual(events, [
        {
            EventType: 'UPDATE_CAPTURE_CONTEXT',
            CallId: callMetaData.callId,
            TranscriptionEngine: 'TRANSCRIBE',
            TranscriptionLanguage: 'en-GB',
            SpeakerSeparation: 'LOCAL',
            AccessToken: 'access',
        },
    ]);
});
