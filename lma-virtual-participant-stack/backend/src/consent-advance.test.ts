/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Consent given in advance, as the consent API client sends what it needs
 * (docs/meeting-consent.md#consent-given-in-advance): the Virtual
 * Participant's own id with the participants it lists, so a consent given
 * through its consent link counts for its meeting, and the participants still
 * listed by an API that does not take the id.
 */
import { strict as assert } from 'node:assert';
import { test } from 'node:test';
import { ConsentApiError, createConsentApi } from './consent-api.js';

test('the participants are listed with the Virtual Participant id', async () => {
    const sent: any[] = [];
    const api = createConsentApi(
        'm-1',
        async (_query, variables: any) => {
            sent.push(variables.input);
            return { setMeetingConsentParticipants: { Consent: { Status: 'pending' } } };
        },
        { virtualParticipantId: 'vp-1' },
    );
    await api.setParticipants(['Bo Chen']);
    assert.deepEqual(sent, [{ CallId: 'm-1', Participants: ['Bo Chen'], VirtualParticipantId: 'vp-1' }]);
});

test('without an id, none is sent', async () => {
    const sent: any[] = [];
    const api = createConsentApi('m-1', async (_query, variables: any) => {
        sent.push(variables.input);
        return { setMeetingConsentParticipants: { Consent: { Status: 'pending' } } };
    });
    await api.setParticipants(['Bo Chen']);
    assert.equal(sent[0].VirtualParticipantId, undefined);
});

test('an API that does not take the id still gets the participants', async () => {
    const sent: any[] = [];
    const api = createConsentApi(
        'm-1',
        async (_query, variables: any) => {
            sent.push(variables.input);
            if (variables.input.VirtualParticipantId) {
                throw new ConsentApiError(
                    "Validation error of type WrongType: argument 'input' contains a field not in 'SetMeetingConsentParticipantsInput': 'VirtualParticipantId'",
                    'ValidationError',
                );
            }
            return { setMeetingConsentParticipants: { Consent: { Status: 'pending' } } };
        },
        { virtualParticipantId: 'vp-1' },
    );
    const record = await api.setParticipants(['Bo Chen']);
    assert.equal(record?.Status, 'pending');
    assert.equal(sent.length, 2);
    assert.equal(sent[1].VirtualParticipantId, undefined);
});

test('any other refusal is not retried', async () => {
    let calls = 0;
    const api = createConsentApi(
        'm-1',
        async () => {
            calls += 1;
            throw new ConsentApiError('the consent record changed; try again', 'Lambda:Unhandled');
        },
        { virtualParticipantId: 'vp-1' },
    );
    await assert.rejects(api.setParticipants(['Bo Chen']));
    assert.equal(calls, 1);
});
