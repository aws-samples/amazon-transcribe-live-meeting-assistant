/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * A spoken consent reply linked to the transcript line it was read from
 * (docs/meeting-consent.md): which line an utterance is linked by, and that the
 * reply is still recorded by an API that takes no link.
 */
import { strict as assert } from 'node:assert';
import { test } from 'node:test';
import { ConsentApiError, createConsentApi } from './consent-api.js';
import { singleSpeakerLine } from './spoken-replies.js';

const line = (segmentId: string, speaker: string, channel = 'REMOTE') => ({ segmentId, channel, speaker });

test('an utterance written as one speaker\'s lines is linked by the first of them', () => {
    assert.deepEqual(singleSpeakerLine([line('r1-r0', 'Bo Chen (spk_0)'), line('r1-r1', 'Bo Chen (spk_0) ')]), line('r1-r0', 'Bo Chen (spk_0)'));
    assert.deepEqual(singleSpeakerLine([line('s2-vp-r1-g0-s4', 'Bo Chen')]), line('s2-vp-r1-g0-s4', 'Bo Chen'));
});

test('an utterance split between speakers, or written as no line, is linked by none', () => {
    assert.equal(singleSpeakerLine([line('r1-r0', 'Bo Chen (spk_0)'), line('r1-r1', 'Ana Silva (spk_1)')]), undefined);
    assert.equal(singleSpeakerLine([line('r1-r0', 'Bo Chen'), line('r1-r1', 'Bo Chen', 'LOCAL')]), undefined);
    assert.equal(singleSpeakerLine([]), undefined);
    assert.equal(singleSpeakerLine([line('', 'Bo Chen')]), undefined);
    assert.equal(singleSpeakerLine([line('r1-r0', '  ')]), undefined);
});

const LINK = { SegmentId: 'r1-r0', Channel: 'REMOTE', Speaker: 'Bo Chen (spk_0)' };

test('a reply is sent with its link, which the VP never reads back', async () => {
    const sent: Array<{ query: string; variables: any }> = [];
    const api = createConsentApi('m-1', async (query, variables: any) => {
        sent.push({ query, variables });
        return { recordMeetingConsentEvent: { Consent: { Status: 'objected' } } };
    });
    await api.recordEvent({ ParticipantName: 'Bo Chen', Decision: 'objection', Source: 'voice', SpeakerLink: LINK });
    assert.equal(sent.length, 1);
    assert.deepEqual(sent[0].variables.input.SpeakerLink, LINK);
    // An answer that selected the link would fail against an API without one.
    assert.doesNotMatch(sent[0].query, /SpeakerLink/);
});

test('an API that takes no link still records the reply, without it', async () => {
    const sent: any[] = [];
    const api = createConsentApi('m-1', async (_query, variables: any) => {
        sent.push(variables.input);
        if (variables.input.SpeakerLink) {
            throw new ConsentApiError(
                "Validation error of type WrongType: argument 'input' contains a field not in 'RecordMeetingConsentEventInput': 'SpeakerLink'",
                'ValidationError',
            );
        }
        return { recordMeetingConsentEvent: { Consent: { Status: 'objected' } } };
    });
    const record = await api.recordEvent({ ParticipantName: 'Bo Chen', Decision: 'objection', Source: 'voice', SpeakerLink: LINK });
    assert.equal(record?.Status, 'objected');
    assert.equal(sent.length, 2);
    assert.equal(sent[1].SpeakerLink, undefined);
    assert.equal(sent[1].ParticipantName, 'Bo Chen');
});

test('any other refusal of a reply is not retried', async () => {
    let calls = 0;
    const api = createConsentApi('m-1', async () => {
        calls += 1;
        throw new ConsentApiError('the consent record changed; try again', 'Lambda:Unhandled');
    });
    await assert.rejects(api.recordEvent({ ParticipantName: 'Bo Chen', Decision: 'objection', Source: 'voice', SpeakerLink: LINK }));
    assert.equal(calls, 1);
});
