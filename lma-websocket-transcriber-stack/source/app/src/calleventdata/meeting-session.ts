/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Which meeting a START may capture into, and the capture session it captures.
 *
 * A START that names a meeting which already exists adds to that meeting, so it
 * is accepted from the meeting's owner or an Admin. A START with a new CallId
 * creates a meeting.
 *
 * A meeting is captured in sessions (docs/virtual-participant.md#rejoin). A
 * streaming client continues an existing meeting by calling the reopenMeeting
 * mutation, which allows the next session on the meeting record, and then
 * naming that session in its START (`sessionNumber`). The transcriber reads the
 * session's number and its offset on the transcript timeline from the meeting
 * record, never from the client: it names the session in the meeting's START
 * and END events, adds the offset to the start and end time of every segment
 * and key frame, and prefixes the segment ids with the session (`s2-`, ...),
 * as the Virtual Participant does. The call event processor then reopens the
 * meeting through the same session path as a rejoin (startCallSession).
 */
import type { GraphqlCaller } from '../consent/appsync';
import { MEETING_NOT_OWNED_MESSAGE } from '../consent/rules';

/** The parts of the meeting record a START is checked against. */
export interface MeetingRecordView {
    readonly owner: string;
    readonly status?: string;
    /** The latest session reopenMeeting or a rejoin allowed (absent: the first). */
    readonly sessionNumber?: number;
    /** Where that session starts on the transcript timeline, in seconds. */
    readonly sessionOffsetSeconds?: number;
    readonly transcriptDeletedAt?: string;
}

/** The verified caller: the access token's username and Cognito groups. */
export interface MeetingCaller {
    readonly username?: string;
    readonly groups: readonly string[];
}

/** What a START may do, and the session it captures. */
export type MeetingAccess =
    /** A new meeting: its first session, named on its START and END. */
    | { readonly kind: 'new'; readonly sessionNumber: 1; readonly offsetSeconds: 0 }
    /**
     * An existing meeting of the caller's (or any, for an Admin). With a session
     * the client asked for and the record allows, that session and its offset;
     * without one, no session is named and nothing is offset, as before.
     */
    | { readonly kind: 'append'; readonly sessionNumber?: number; readonly offsetSeconds: number }
    | { readonly kind: 'refuse'; readonly code: MeetingRefusalCode; readonly message: string };

export type MeetingRefusalCode =
    | 'MEETING_NOT_OWNED'
    | 'MEETING_NOT_FOUND'
    | 'SESSION_NOT_ALLOWED'
    | 'TRANSCRIPT_DELETED'
    | 'MEETING_NOT_CHECKED';

export const MEETING_REFUSALS: Readonly<Record<MeetingRefusalCode, string>> = {
    MEETING_NOT_OWNED: MEETING_NOT_OWNED_MESSAGE,
    MEETING_NOT_FOUND: 'There is no meeting with this ID to continue. Check the meeting ID and try again.',
    SESSION_NOT_ALLOWED:
        'This meeting was not reopened for this capture. Choose to continue the meeting again, then start.',
    TRANSCRIPT_DELETED: 'The transcript of this meeting has been deleted, so the meeting cannot be continued.',
    MEETING_NOT_CHECKED: 'LMA could not check this meeting just now, so it was not captured. Try again.',
};

/** A refusal of the START, with the message the client is sent. */
export const meetingRefusal = (code: MeetingRefusalCode): MeetingAccess => ({
    kind: 'refuse',
    code,
    message: MEETING_REFUSALS[code],
});

const refuse = meetingRefusal;

/**
 * The session a START asks for: undefined when it names none, null when what
 * it names is not a session a reopen allows (an integer from 2).
 */
export const requestedSessionNumber = (value: unknown): number | undefined | null => {
    if (value === undefined || value === null) {
        return undefined;
    }
    return typeof value === 'number' && Number.isInteger(value) && value >= 2 ? value : null;
};

/** Whether the caller may add to the meeting: its owner, or an Admin. */
export const callerOwnsMeeting = (record: MeetingRecordView, caller: MeetingCaller | undefined): boolean =>
    Boolean(caller) &&
    ((caller?.groups ?? []).includes('Admin') || (Boolean(caller?.username) && record.owner === caller?.username));

/**
 * What a START may do. `record` is the meeting record (null when there is no
 * meeting with the CallId); `requested` the START's `sessionNumber`.
 */
export const decideMeetingAccess = (
    record: MeetingRecordView | null,
    caller: MeetingCaller | undefined,
    requested: unknown
): MeetingAccess => {
    const session = requestedSessionNumber(requested);
    if (session === null) {
        return refuse('SESSION_NOT_ALLOWED');
    }
    if (record === null) {
        return session === undefined ? { kind: 'new', sessionNumber: 1, offsetSeconds: 0 } : refuse('MEETING_NOT_FOUND');
    }
    if (!callerOwnsMeeting(record, caller)) {
        return refuse('MEETING_NOT_OWNED');
    }
    if (session === undefined) {
        return { kind: 'append', offsetSeconds: 0 };
    }
    if (record.transcriptDeletedAt) {
        return refuse('TRANSCRIPT_DELETED');
    }
    if (record.sessionNumber !== session) {
        return refuse('SESSION_NOT_ALLOWED');
    }
    const offset = record.sessionOffsetSeconds;
    return {
        kind: 'append',
        sessionNumber: session,
        offsetSeconds: typeof offset === 'number' && Number.isFinite(offset) && offset > 0 ? offset : 0,
    };
};

export const MEETING_RECORD = `
query ($CallId: ID!) {
  getCall(CallId: $CallId) { CallId Owner Status SessionNumber SessionOffsetSeconds TranscriptDeletedAt }
}`;

const asRecord = (value: unknown): Record<string, unknown> | undefined =>
    value !== null && typeof value === 'object' && !Array.isArray(value)
        ? (value as Record<string, unknown>)
        : undefined;

const finiteNumber = (value: unknown): number | undefined =>
    typeof value === 'number' && Number.isFinite(value) ? value : undefined;

/**
 * The meeting record, or null when there is no meeting with the CallId. getCall
 * answers this task's role (a backend role) with the row as stored. Throws
 * when the record could not be read.
 */
export const readMeetingRecord = async (call: GraphqlCaller, callId: string): Promise<MeetingRecordView | null> => {
    const response = await call(MEETING_RECORD, { CallId: callId });
    const errors = response.errors ?? [];
    if (errors.length > 0) {
        throw new Error(`getCall: ${errors.map((error) => error.message ?? error.errorType ?? 'error').join('; ')}`);
    }
    const record = asRecord(asRecord(response.data)?.['getCall']);
    if (!record) {
        return null;
    }
    return {
        owner: typeof record['Owner'] === 'string' ? record['Owner'] : '',
        status: typeof record['Status'] === 'string' ? record['Status'] : undefined,
        sessionNumber: finiteNumber(record['SessionNumber']),
        sessionOffsetSeconds: finiteNumber(record['SessionOffsetSeconds']),
        transcriptDeletedAt:
            typeof record['TranscriptDeletedAt'] === 'string' && record['TranscriptDeletedAt']
                ? record['TranscriptDeletedAt']
                : undefined,
    };
};

/** Delays between the attempts to read the meeting record. */
export const MEETING_READ_RETRY_DELAYS_MS: readonly number[] = [250, 1000];

/**
 * readMeetingRecord, tried again after each of `delays` when it throws. Throws
 * the last error when every attempt failed.
 */
export const readMeetingRecordWithRetries = async (
    call: GraphqlCaller,
    callId: string,
    delays: readonly number[] = MEETING_READ_RETRY_DELAYS_MS,
    sleep: (ms: number) => Promise<void> = (ms) => new Promise((resolve) => setTimeout(resolve, ms))
): Promise<MeetingRecordView | null> => {
    for (let attempt = 0; ; attempt += 1) {
        try {
            return await readMeetingRecord(call, callId);
        } catch (error) {
            const delay = delays[attempt];
            if (delay === undefined) {
                throw error;
            }
            await sleep(delay);
        }
    }
};

/** The session fields the transcriber keeps on a meeting's metadata. */
export interface SessionFields {
    /** Named on the START and END events when set. */
    readonly sessionNumber?: number;
    /** Added to every segment's and key frame's time. */
    readonly sessionOffsetSeconds?: number;
}

/** The segment as sent for its session: times offset, and the id prefixed for a later session. */
export const sessionSegment = <T extends { SegmentId: string; StartTime: number; EndTime: number }>(
    segment: T,
    session: SessionFields
): T => {
    const offset = session.sessionOffsetSeconds ?? 0;
    const number = session.sessionNumber ?? 1;
    if (offset === 0 && number < 2) {
        return segment;
    }
    return {
        ...segment,
        SegmentId: number >= 2 ? `s${number}-${segment.SegmentId}` : segment.SegmentId,
        StartTime: segment.StartTime + offset,
        EndTime: segment.EndTime + offset,
    };
};

/** `{ SessionNumber }` for a START or END event, or nothing when no session is named. */
export const sessionEventFields = (session: SessionFields): { SessionNumber?: number } =>
    session.sessionNumber !== undefined ? { SessionNumber: session.sessionNumber } : {};
