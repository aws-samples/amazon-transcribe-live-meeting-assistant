/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * A chat consent reply linked to its transcript speaker by the platform's
 * participant id (docs/meeting-consent.md, basis `platform_identity`): the id a
 * row of speech carries, which speaker an id was last written under, and that
 * the reply is still recorded by an API that takes no such link.
 */
import { strict as assert } from 'node:assert';
import { afterEach, beforeEach, describe, it } from 'node:test';

process.env.LOCAL_TEST = 'true';
delete process.env.CALL_DATA_STREAM_NAME;

// eslint-disable-next-line import/first
import { RosterTimeline } from './roster-timeline.js';
// eslint-disable-next-line import/first
import { notePlatformSpeaker, platformId, platformSpeakerFor, resetPlatformSpeakers } from './platform-speakers.js';
// eslint-disable-next-line import/first
import { platformIdentityLinkFor } from './meeting-consent.js';
// eslint-disable-next-line import/first
import { ConsentApiError, createConsentApi } from './consent-api.js';
// eslint-disable-next-line import/first
import { kinesisStreamManager } from './kinesis-stream.js';

describe('the id a row of speech carries', () => {
    const timeline = () => {
        const t = new RosterTimeline(['LMA']);
        t.record(0, 'Bo Chen', '8:acs:bo');
        t.record(10, 'Ana Silva', '8:acs:ana');
        t.record(20, 'Bo Chen', '8:acs:bo-2');
        t.record(30, 'Cy Lee');
        return t;
    };

    it('is the id of the participant shown for most of the row, when the row is named after them', () => {
        assert.equal(timeline().platformIdFor(2, 9, 'Bo Chen'), '8:acs:bo');
        assert.equal(timeline().platformIdFor(8, 18, 'Ana Silva'), '8:acs:ana');
        // The speech engine's voice label is not part of the name.
        assert.equal(timeline().platformIdFor(2, 9, 'Bo Chen (spk_0)'), '8:acs:bo');
    });

    it('is none when the row is named after somebody other than who was shown', () => {
        assert.equal(timeline().platformIdFor(2, 9, 'Ana Silva'), undefined);
        assert.equal(timeline().platformIdFor(8, 18, 'Bo Chen'), undefined);
    });

    it('tells apart two participants with one display name', () => {
        assert.equal(timeline().platformIdFor(21, 29, 'Bo Chen'), '8:acs:bo-2');
    });

    it('is none for a participant the platform gave no id for, or before anyone was shown', () => {
        assert.equal(timeline().platformIdFor(31, 35, 'Cy Lee'), undefined);
        const late = new RosterTimeline([]);
        late.record(10, 'Bo Chen', '8:acs:bo');
        assert.equal(late.platformIdFor(1, 2, 'Bo Chen'), undefined);
        assert.equal(new RosterTimeline([]).platformIdFor(1, 2, 'Bo Chen'), undefined);
    });

    it('applies the platform lag as speakerFor does', () => {
        // Ana's change reported at 10 is moved back to 8 with a 2 s lag.
        assert.equal(timeline().platformIdFor(8, 9.5, 'Ana Silva', 2), '8:acs:ana');
    });
});

describe('which speaker an id was last written under', () => {
    beforeEach(() => resetPlatformSpeakers());
    afterEach(() => resetPlatformSpeakers());

    it('is the most recent speaker label written with the id', () => {
        notePlatformSpeaker('16778240', 'REMOTE', 'Bo Chen');
        notePlatformSpeaker('16778240', 'REMOTE', 'Bo Chen (spk_1)');
        assert.deepEqual(platformSpeakerFor('16778240'), { channel: 'REMOTE', speaker: 'Bo Chen (spk_1)' });
        assert.equal(platformSpeakerFor('16778241'), undefined);
        assert.equal(platformSpeakerFor(undefined), undefined);
    });

    it('is nobody when that speaker label was also written with another id', () => {
        notePlatformSpeaker('8:acs:bo', 'REMOTE', 'Guest (spk_0)');
        notePlatformSpeaker('8:acs:ana', 'REMOTE', 'Guest (spk_0)');
        assert.equal(platformSpeakerFor('8:acs:bo'), undefined);
        assert.equal(platformSpeakerFor('8:acs:ana'), undefined);
        assert.equal(platformIdentityLinkFor('8:acs:bo'), undefined);
    });

    it('gives a chat reply its platform_identity link', () => {
        notePlatformSpeaker('8:acs:bo', 'REMOTE', 'Bo Chen');
        assert.deepEqual(platformIdentityLinkFor('8:acs:bo'), {
            Basis: 'platform_identity',
            PlatformParticipantId: '8:acs:bo',
            Channel: 'REMOTE',
            Speaker: 'Bo Chen',
        });
        assert.equal(platformIdentityLinkFor(undefined), undefined);
    });

    it('takes an id as a bounded string, a number included', () => {
        assert.equal(platformId(16778240), '16778240');
        assert.equal(platformId('  8:acs:bo '), '8:acs:bo');
        assert.equal(platformId(''), undefined);
        assert.equal(platformId('x'.repeat(257)), undefined);
        assert.equal(platformId(null), undefined);
    });
});

describe('the transcript writer', () => {
    type Row = Record<string, any>;
    const sent: Row[] = [];
    const manager = kinesisStreamManager as unknown as { sendRecord: (record: Row) => Promise<void> };
    const original = manager.sendRecord;
    beforeEach(() => {
        sent.length = 0;
        resetPlatformSpeakers();
        manager.sendRecord = async (record: Row) => {
            sent.push(record);
        };
        kinesisStreamManager.setPlatformIdLookup((start, _end, speaker) =>
            speaker.startsWith('Bo Chen') && start < 100 ? '8:acs:bo' : undefined,
        );
    });
    afterEach(() => {
        manager.sendRecord = original;
        kinesisStreamManager.setPlatformIdLookup(null);
        resetPlatformSpeakers();
    });

    const row = (overrides: Row = {}) => ({
        segmentId: 'vp-remote-r0-g0-s0',
        startTime: 1,
        endTime: 2,
        transcript: 'hello',
        isPartial: false,
        channel: 'REMOTE',
        speaker: 'Bo Chen',
        ...overrides,
    });

    it('puts the id on a row of speech named after its participant, and notes a final one', async () => {
        await kinesisStreamManager.sendAsrTranscriptSegment(row({ isPartial: true }));
        assert.equal(sent[0].PlatformParticipantId, '8:acs:bo');
        assert.equal(platformSpeakerFor('8:acs:bo'), undefined, 'a partial is not a written line');
        await kinesisStreamManager.sendAsrTranscriptSegment(row());
        assert.equal(sent[1].PlatformParticipantId, '8:acs:bo');
        assert.deepEqual(platformSpeakerFor('8:acs:bo'), { channel: 'REMOTE', speaker: 'Bo Chen' });
    });

    it('puts no id on a row the lookup gives none for, or on a chat row', async () => {
        await kinesisStreamManager.sendAsrTranscriptSegment(row({ speaker: 'Ana Silva' }));
        await kinesisStreamManager.sendChatSegment(row({ segmentId: 'chat-1' }));
        assert.equal('PlatformParticipantId' in sent[0], false);
        assert.equal('PlatformParticipantId' in sent[1], false);
        assert.equal(platformSpeakerFor('8:acs:bo'), undefined);
    });

    it('puts the id on an Amazon Transcribe row as it is prepared and sent', async () => {
        const prepared = kinesisStreamManager.prepareTranscriptSegments('Bo Chen', {
            ResultId: 'result-1',
            IsPartial: false,
            StartTime: 2,
            EndTime: 3,
            Alternatives: [{ Transcript: 'I object', Items: [{ Type: 'pronunciation', Content: 'object', StartTime: 2, EndTime: 3 }] }],
        });
        assert.equal(prepared.records[0].PlatformParticipantId, '8:acs:bo');
        await kinesisStreamManager.sendPreparedTranscriptSegments(prepared);
        assert.equal(sent[0].PlatformParticipantId, '8:acs:bo');
        assert.equal(platformSpeakerFor('8:acs:bo')?.speaker, sent[0].Speaker);
    });
});

describe('the consent API', () => {
    const LINK = { Basis: 'platform_identity' as const, PlatformParticipantId: '8:acs:bo', Channel: 'REMOTE', Speaker: 'Bo Chen' };

    it('records a chat reply without its link when the API takes no platform id', async () => {
        const sent: any[] = [];
        const api = createConsentApi('m-1', async (_query, variables: any) => {
            sent.push(variables.input);
            if (variables.input.SpeakerLink?.PlatformParticipantId) {
                throw new ConsentApiError(
                    "Validation error of type WrongType: argument 'input.SpeakerLink' contains a field not in 'MeetingConsentSpeakerLinkInput': 'PlatformParticipantId'",
                    'ValidationError',
                );
            }
            return { recordMeetingConsentEvent: { Consent: { Status: 'objected' } } };
        });
        const record = await api.recordEvent({ ParticipantName: 'Bo Chen', Decision: 'objection', Source: 'chat', SpeakerLink: LINK });
        assert.equal(record?.Status, 'objected');
        assert.equal(sent.length, 2);
        assert.deepEqual(sent[0].SpeakerLink, LINK);
        assert.equal(sent[1].SpeakerLink, undefined);
    });
});
