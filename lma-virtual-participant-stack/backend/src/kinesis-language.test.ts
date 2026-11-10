/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The identified language of an Amazon Transcribe result, on the
 * ADD_TRANSCRIPT_SEGMENT records the Virtual Participant writes for it.
 *
 * Drives the real writer with only `sendRecord` stubbed, as
 * kinesis-diarized-segments.test.ts does.
 */
import { strict as assert } from 'node:assert';
import { beforeEach, describe, it } from 'node:test';

process.env.LOCAL_TEST = 'true';
delete process.env.CALL_DATA_STREAM_NAME;

// eslint-disable-next-line import/first
import { kinesisStreamManager } from './kinesis-stream.js';
// eslint-disable-next-line import/first
import { assistantSegmentFromResult } from './transcribe-channels.js';
// eslint-disable-next-line import/first
import { resultLanguageCode } from './transcribe-language.js';

type SentRecord = { EventType: string; Channel?: string; LanguageCode?: string; Transcript?: string };

const sent: SentRecord[] = [];

const manager = kinesisStreamManager as unknown as {
    sendRecord: (record: SentRecord) => Promise<void>;
    setDiarization: (on: boolean) => void;
    setRosterLookup: (lookup: null) => void;
    sendTranscriptSegment: (speaker: string, result: unknown, offset?: number) => Promise<number>;
    sendAsrTranscriptSegment: (segment: unknown) => Promise<void>;
};

const result = (languageCode?: string, isPartial = false) => ({
    ResultId: 'lang-1',
    IsPartial: isPartial,
    StartTime: 1,
    EndTime: 3,
    ...(languageCode === undefined ? {} : { LanguageCode: languageCode }),
    Alternatives: [
        {
            Transcript: 'bonjour tout le monde',
            Items: [
                { Content: 'bonjour', Type: 'pronunciation', StartTime: 1, EndTime: 1.5 },
                { Content: 'tout', Type: 'pronunciation', StartTime: 1.5, EndTime: 2 },
                { Content: 'le', Type: 'pronunciation', StartTime: 2, EndTime: 2.2 },
                { Content: 'monde', Type: 'pronunciation', StartTime: 2.2, EndTime: 3 },
            ],
        },
    ],
});

const segments = () => sent.filter((record) => record.EventType === 'ADD_TRANSCRIPT_SEGMENT');

beforeEach(() => {
    sent.length = 0;
    manager.sendRecord = async (record: SentRecord) => {
        sent.push(record);
    };
    manager.setDiarization(false);
    manager.setRosterLookup(null);
});

describe('the participants channel', () => {
    it('sends the result language on the segment', async () => {
        await manager.sendTranscriptSegment('Alex', result('fr-FR'));
        assert.equal(segments().length, 1);
        assert.equal(segments()[0]?.LanguageCode, 'fr-FR');
    });

    it('sends it on a partial too, so the live row carries it from the start', async () => {
        await manager.sendTranscriptSegment('Alex', result('fr-FR', true));
        assert.equal(segments()[0]?.LanguageCode, 'fr-FR');
    });

    it('leaves the field out when Transcribe names no language', async () => {
        await manager.sendTranscriptSegment('Alex', result(undefined));
        assert.equal(segments().length, 1);
        assert.equal('LanguageCode' in (segments()[0] ?? {}), false);
    });
});

describe('the assistant channel and the MicroVM rows', () => {
    it('carries the result language onto the assistant row', async () => {
        const row = assistantSegmentFromResult(result('en-GB'), 0, 'LMA');
        assert.equal(row?.languageCode, 'en-GB');
        await manager.sendAsrTranscriptSegment(row);
        assert.equal(segments()[0]?.LanguageCode, 'en-GB');
        assert.equal(segments()[0]?.Channel, 'LOCAL');
    });

    it('sends a MicroVM row, which has no language, without the field', async () => {
        await manager.sendAsrTranscriptSegment({
            channel: 'REMOTE',
            segmentId: 'asr-1',
            startTime: 0,
            endTime: 1,
            transcript: 'hello',
            isPartial: false,
            speaker: 'Alex',
        });
        assert.equal('LanguageCode' in (segments()[0] ?? {}), false);
    });
});

describe('resultLanguageCode', () => {
    it('reads a code and treats a blank or missing one as none', () => {
        assert.equal(resultLanguageCode({ LanguageCode: 'hi-IN' }), 'hi-IN');
        assert.equal(resultLanguageCode({ LanguageCode: ' ' }), undefined);
        assert.equal(resultLanguageCode({}), undefined);
        assert.equal(resultLanguageCode(null), undefined);
    });
});
