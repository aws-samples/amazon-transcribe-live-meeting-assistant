/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The capture context a VP meeting's record is given: its START names the
 * Virtual Participant as the capture source with the engine, language and
 * speaker separation it is expected to use, and UPDATE_CAPTURE_CONTEXT names
 * Amazon Transcribe when it takes over from the MicroVM. Drives the real
 * writer with only `sendRecord` stubbed.
 */
import { strict as assert } from 'node:assert';
import { afterEach, beforeEach, describe, it } from 'node:test';

process.env.LOCAL_TEST = 'true';
delete process.env.CALL_DATA_STREAM_NAME;

// eslint-disable-next-line import/first
import { kinesisStreamManager, vpTranscriptionContext } from './kinesis-stream.js';

type Record_ = Record<string, unknown>;

const sent: Record_[] = [];

const manager = kinesisStreamManager as unknown as {
    sendRecord: (record: Record_) => Promise<void>;
    sendStartMeeting: () => Promise<void>;
    sendTranscriptionContext: (context: ReturnType<typeof vpTranscriptionContext>) => Promise<void>;
    setTranscriptionContext: (context: ReturnType<typeof vpTranscriptionContext> | null) => void;
};

beforeEach(() => {
    sent.length = 0;
    manager.sendRecord = async (record: Record_) => {
        sent.push(record);
    };
});

afterEach(() => {
    manager.setTranscriptionContext(null);
});

describe('vpTranscriptionContext', () => {
    it('names Amazon Transcribe with the language it is started in', () => {
        assert.deepEqual(vpTranscriptionContext('transcribe', false, 'en-US'), {
            TranscriptionEngine: 'TRANSCRIBE',
            TranscriptionLanguage: 'en-US',
            SpeakerSeparation: 'NONE',
        });
    });

    it('names the MicroVM without a language, and separation on the meeting audio only', () => {
        assert.deepEqual(vpTranscriptionContext('microvm', true, 'en-US'), {
            TranscriptionEngine: 'MICROVM_ASR',
            SpeakerSeparation: 'REMOTE',
        });
    });
});

describe('the meeting record', () => {
    it('is created as a Virtual Participant meeting with its transcription context', async () => {
        manager.setTranscriptionContext(vpTranscriptionContext('microvm', true, 'en-US'));
        await manager.sendStartMeeting();
        const start = sent.find((record) => record.EventType === 'START');
        assert.ok(start);
        assert.equal(start.CaptureSource, 'VIRTUAL_PARTICIPANT');
        assert.equal(start.TranscriptionEngine, 'MICROVM_ASR');
        assert.equal(start.SpeakerSeparation, 'REMOTE');
        assert.equal('TranscriptionLanguage' in start, false);
    });

    it('names its capture source even when the engine is not known', async () => {
        await manager.sendStartMeeting();
        const start = sent.find((record) => record.EventType === 'START');
        assert.equal(start?.CaptureSource, 'VIRTUAL_PARTICIPANT');
        assert.equal(start && 'TranscriptionEngine' in start, false);
    });

    it('is told when Amazon Transcribe takes over', async () => {
        await manager.sendTranscriptionContext(vpTranscriptionContext('transcribe', true, 'de-DE'));
        assert.equal(sent.length, 1);
        assert.equal(sent[0]?.EventType, 'UPDATE_CAPTURE_CONTEXT');
        assert.equal(sent[0]?.CallId, kinesisStreamManager.currentCallId);
        assert.equal(sent[0]?.TranscriptionEngine, 'TRANSCRIBE');
        assert.equal(sent[0]?.TranscriptionLanguage, 'de-DE');
        assert.equal(sent[0]?.SpeakerSeparation, 'REMOTE');
    });
});
