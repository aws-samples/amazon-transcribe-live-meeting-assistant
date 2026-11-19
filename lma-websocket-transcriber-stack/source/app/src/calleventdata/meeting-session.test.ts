/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Which meeting a START may capture into, and its capture session
 * (meeting-session.ts): the decision on the meeting record, reading the record,
 * and how a later session's segments are placed on the meeting's timeline.
 */
import assert from 'node:assert/strict';
import test from 'node:test';

import type { GraphqlCaller } from '../consent/appsync';
import {
    MEETING_REFUSALS,
    MeetingRecordView,
    decideMeetingAccess,
    readMeetingRecord,
    readMeetingRecordWithRetries,
    requestedSessionNumber,
    sessionEventFields,
    sessionSegment,
} from './meeting-session';

const OWNER = { username: 'ana', groups: [] };
const ADMIN = { username: 'root', groups: ['Admin'] };
const OTHER = { username: 'bob', groups: [] };

const ended = (fields: Partial<MeetingRecordView> = {}): MeetingRecordView => ({
    owner: 'ana',
    status: 'ENDED',
    ...fields,
});

test('a START with a new CallId creates the meeting as its first session', () => {
    assert.deepEqual(decideMeetingAccess(null, OWNER, undefined), { kind: 'new', sessionNumber: 1, offsetSeconds: 0 });
});

test('a START that adds to an existing meeting requires its owner or an Admin', () => {
    assert.deepEqual(decideMeetingAccess(ended(), OWNER, undefined), { kind: 'append', offsetSeconds: 0 });
    assert.deepEqual(decideMeetingAccess(ended(), ADMIN, undefined), { kind: 'append', offsetSeconds: 0 });
    const refused = decideMeetingAccess(ended(), OTHER, undefined);
    assert.equal(refused.kind, 'refuse');
    assert.equal(refused.kind === 'refuse' && refused.code, 'MEETING_NOT_OWNED');
});

test('a caller with no verified identity may not add to an existing meeting', () => {
    const refused = decideMeetingAccess(ended(), undefined, undefined);
    assert.equal(refused.kind === 'refuse' && refused.code, 'MEETING_NOT_OWNED');
    const nameless = decideMeetingAccess(ended({ owner: '' }), { groups: [] }, undefined);
    assert.equal(nameless.kind === 'refuse' && nameless.code, 'MEETING_NOT_OWNED');
});

test('a START for the session the meeting record allows captures it at the recorded offset', () => {
    const access = decideMeetingAccess(ended({ sessionNumber: 3, sessionOffsetSeconds: 1807 }), OWNER, 3);
    assert.deepEqual(access, { kind: 'append', sessionNumber: 3, offsetSeconds: 1807 });
});

test('the offset is taken from the meeting record, never from the client', () => {
    const access = decideMeetingAccess(ended({ sessionNumber: 2 }), ADMIN, 2);
    assert.deepEqual(access, { kind: 'append', sessionNumber: 2, offsetSeconds: 0 });
});

test('a START for a session the meeting record does not allow is refused', () => {
    for (const record of [ended(), ended({ sessionNumber: 2 }), ended({ sessionNumber: 4 })]) {
        const access = decideMeetingAccess(record, OWNER, 3);
        assert.equal(access.kind === 'refuse' && access.code, 'SESSION_NOT_ALLOWED');
    }
});

test('a session number that is not one a reopen gives is refused', () => {
    for (const value of [1, 0, -2, 2.5, '2', true, {}]) {
        assert.equal(requestedSessionNumber(value), null, String(value));
        const access = decideMeetingAccess(ended({ sessionNumber: 2 }), OWNER, value);
        assert.equal(access.kind === 'refuse' && access.code, 'SESSION_NOT_ALLOWED', String(value));
    }
    assert.equal(requestedSessionNumber(undefined), undefined);
    assert.equal(requestedSessionNumber(null), undefined);
    assert.equal(requestedSessionNumber(2), 2);
});

test('continuing a meeting that does not exist is refused', () => {
    const access = decideMeetingAccess(null, OWNER, 2);
    assert.equal(access.kind === 'refuse' && access.code, 'MEETING_NOT_FOUND');
});

test('continuing a meeting whose transcript was deleted is refused', () => {
    const access = decideMeetingAccess(
        ended({ sessionNumber: 2, transcriptDeletedAt: '2026-11-18T11:00:00Z' }),
        OWNER,
        2
    );
    assert.equal(access.kind === 'refuse' && access.code, 'TRANSCRIPT_DELETED');
});

test('the owner check comes before any session check', () => {
    const access = decideMeetingAccess(ended({ sessionNumber: 2 }), OTHER, 2);
    assert.equal(access.kind === 'refuse' && access.code, 'MEETING_NOT_OWNED');
});

test('every refusal carries a message for the client', () => {
    for (const [code, message] of Object.entries(MEETING_REFUSALS)) {
        assert.ok(message.length > 20, code);
    }
});

test('a later session\'s segments follow the earlier ones and have ids of their own', () => {
    const segment = { SegmentId: 'abc', StartTime: 1.5, EndTime: 3.25, Transcript: 'hi' };
    assert.deepEqual(sessionSegment(segment, { sessionNumber: 2, sessionOffsetSeconds: 100 }), {
        SegmentId: 's2-abc',
        StartTime: 101.5,
        EndTime: 103.25,
        Transcript: 'hi',
    });
});

test('a first session\'s segments, or a meeting with no session named, are sent as they are', () => {
    const segment = { SegmentId: 'abc', StartTime: 1.5, EndTime: 3.25 };
    assert.equal(sessionSegment(segment, {}), segment);
    assert.equal(sessionSegment(segment, { sessionNumber: 1, sessionOffsetSeconds: 0 }), segment);
});

test('the START and END name the session only when there is one', () => {
    assert.deepEqual(sessionEventFields({ sessionNumber: 2 }), { SessionNumber: 2 });
    assert.deepEqual(sessionEventFields({ sessionNumber: 1 }), { SessionNumber: 1 });
    assert.deepEqual(sessionEventFields({}), {});
});

const answering = (...answers: Array<Awaited<ReturnType<GraphqlCaller>> | Error>): GraphqlCaller & { calls: number } => {
    const counter = { calls: 0 };
    const call: GraphqlCaller = async () => {
        counter.calls += 1;
        const answer = answers.shift();
        if (answer instanceof Error || answer === undefined) {
            throw answer ?? new Error('no answer');
        }
        return answer;
    };
    return Object.defineProperty(call, 'calls', { get: () => counter.calls }) as GraphqlCaller & { calls: number };
};

test('the meeting record is read with its owner, status, session and deletion', async () => {
    const call = answering({
        data: {
            getCall: {
                CallId: 'm',
                Owner: 'ana',
                Status: 'ENDED',
                SessionNumber: 2,
                SessionOffsetSeconds: 1807,
                TranscriptDeletedAt: null,
            },
        },
    });
    assert.deepEqual(await readMeetingRecord(call, 'm'), {
        owner: 'ana',
        status: 'ENDED',
        sessionNumber: 2,
        sessionOffsetSeconds: 1807,
        transcriptDeletedAt: undefined,
    });
});

test('no record is read as no meeting, and an error is raised', async () => {
    assert.equal(await readMeetingRecord(answering({ data: { getCall: null } }), 'm'), null);
    await assert.rejects(readMeetingRecord(answering({ errors: [{ message: 'denied' }] }), 'm'), /denied/);
});

test('a record that could not be read is read again, and the last failure is raised', async () => {
    const slept: number[] = [];
    const sleep = async (ms: number) => {
        slept.push(ms);
    };
    const recovers = answering(new Error('timeout'), { data: { getCall: { Owner: 'ana' } } });
    assert.equal((await readMeetingRecordWithRetries(recovers, 'm', [5, 10], sleep))?.owner, 'ana');
    assert.deepEqual(slept, [5]);

    const fails = answering(new Error('a'), new Error('b'), new Error('c'));
    await assert.rejects(readMeetingRecordWithRetries(fails, 'm', [5, 10], sleep), /c/);
    assert.equal(fails.calls, 3);
});
