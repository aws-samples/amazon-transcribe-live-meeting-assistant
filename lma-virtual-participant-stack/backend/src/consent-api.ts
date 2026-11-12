/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The meeting consent API as the Virtual Participant calls it: the AppSync
 * operations of docs/meeting-consent.md, signed with the task's role, which the
 * meeting consent resolver treats as a backend role.
 *
 * Every mutation answers with the whole record, which is what the flow
 * decides from; `ConsentRecord` is the part of it the flow reads.
 */
import { createSignedFetcher } from 'aws-sigv4-fetch';
import { createLogger } from './logger.js';

const log = createLogger('consent-api');

export interface ConsentEvent {
    EventId?: string | null;
    ParticipantName?: string | null;
    Decision?: string | null;
    Source?: string | null;
    Text?: string | null;
}

export interface ConsentEvidenceEntry {
    EvidenceId?: string | null;
    S3Key?: string | null;
}

export interface ConsentRecord {
    Mode?: string | null;
    Rule?: string | null;
    Status?: string | null;
    CaptureAllowed?: boolean | null;
    NoticeText?: string | null;
    NoticePostedAt?: string | null;
    Participants?: string[] | null;
    Events?: ConsentEvent[] | null;
    Attestation?: { AttestedBy?: string | null } | null;
    Evidence?: ConsentEvidenceEntry[] | null;
    StatusHistory?: Array<{ Status?: string | null; Reason?: string | null }> | null;
    Version?: number | null;
}

export interface ConsentSettingsView {
    Mode?: string | null;
    Rule?: string | null;
    ConsentPhrases?: string[] | null;
    ObjectionPhrases?: string[] | null;
    AllowPerMeetingOverride?: boolean | null;
    KeepEvidenceScreenshots?: boolean | null;
}

export interface ConsentState {
    record: ConsentRecord | null;
    captureAllowed: boolean;
    settings: ConsentSettingsView | null;
}

export interface StartConsentInput {
    Mode?: string;
    Rule?: string;
    LmaUser?: string;
    MeetingName?: string;
}

/** Where a reply was read: typed in the meeting chat, or said aloud and heard. */
export type ReplySource = 'chat' | 'voice';

export interface RecordEventInput {
    ParticipantName: string;
    Decision: 'consent' | 'objection';
    Text?: string;
    OccurredAt?: string;
    /** `chat` when omitted. */
    Source?: ReplySource;
}

export interface AddEvidenceInput {
    S3Key: string;
    CapturedAt: string;
    Note?: string;
}

/** What the flow needs from the API; a test supplies its own. */
export interface ConsentApi {
    start(input: StartConsentInput): Promise<ConsentRecord | null>;
    get(): Promise<ConsentState>;
    recordNotice(text: string): Promise<ConsentRecord | null>;
    setParticipants(names: string[]): Promise<ConsentRecord | null>;
    recordEvent(input: RecordEventInput): Promise<ConsentRecord | null>;
    addEvidence(input: AddEvidenceInput): Promise<ConsentRecord | null>;
    /** Asks for the vision-model check of one image; see docs/meeting-consent.md. */
    verifyEvidence(evidenceId: string): Promise<void>;
}

/** A GraphQL error the resolver answered with: the request was understood and refused. */
export class ConsentApiError extends Error {
    constructor(
        message: string,
        readonly errorType: string,
    ) {
        super(message);
        this.name = 'ConsentApiError';
    }

    /**
     * The resolver refused the mode or rule asked for: the deployment does not
     * allow it, or the meeting's consent is already under way in another mode.
     * Matched on the refusals' own words, since an answer to retry ("try
     * again") must never be taken for one.
     */
    get choiceRefused(): boolean {
        return (
            !this.meetingNotCreatedYet &&
            !/try again/i.test(this.message) &&
            /does not allow a meeting|already under way|must be one of/i.test(this.message)
        );
    }

    /** The meeting row is not there yet: its START event is still being processed. */
    get meetingNotCreatedYet(): boolean {
        // The resolver's answer to a backend role for a CallId with no row.
        return /^no meeting\b/i.test(this.message.trim());
    }
}

/** The record's fields, selected in full by every write (docs/meeting-consent.md). */
export const CONSENT_RECORD_FIELDS = `
    Mode Rule Status CaptureAllowed NoticeText NoticePostedAt
    Notices { Channel Text PostedAt PostedBy }
    Participants ParticipantsSeen
    Events { EventId ParticipantName Decision Source Text OccurredAt RecordedAt RecordedBy }
    Attestation { AttestedBy Method Statement AttestedAt RecordedBy }
    Evidence { EvidenceId Kind S3Key CapturedAt AddedAt Note }
    StatusHistory { Status CaptureAllowed At Reason }
    StartedAt StartedBy UpdatedAt Version`;

const UPDATE_SELECTION = `{ CallId Owner SharedWith Consent { ${CONSENT_RECORD_FIELDS} } }`;

type GraphqlSender = (query: string, variables: Record<string, unknown>) => Promise<Record<string, unknown>>;

/**
 * How long one request may take. The flow sends its writes and reads one after
 * another, so a request that hangs would hold up every later one (the release
 * after an attestation among them); a timeout turns it into a failure the flow
 * retries. Above the 30 seconds AppSync allows a resolver, plus the network.
 */
export const CONSENT_API_TIMEOUT_MS = 35_000;

/** Sends one signed GraphQL request; throws ConsentApiError for an answered error. */
export function signedGraphqlSender(endpoint: string, region: string, timeoutMs = CONSENT_API_TIMEOUT_MS): GraphqlSender {
    const signedFetch = createSignedFetcher({ service: 'appsync', region });
    return async (query, variables) => {
        const response = await signedFetch(endpoint, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ query, variables }),
            signal: AbortSignal.timeout(timeoutMs),
        });
        if (!response.ok) {
            throw new Error(`the consent API answered HTTP ${response.status}`);
        }
        const body = (await response.json()) as { data?: Record<string, unknown>; errors?: Array<{ message?: string; errorType?: string }> };
        if (body.errors && body.errors.length > 0) {
            const first = body.errors[0];
            throw new ConsentApiError(first.message || 'the consent API refused the request', first.errorType || '');
        }
        return body.data || {};
    };
}

export function createConsentApi(callId: string, send: GraphqlSender): ConsentApi {
    const mutate = async (field: string, inputType: string, input: Record<string, unknown>): Promise<ConsentRecord | null> => {
        const query = `mutation Consent($input: ${inputType}!) { ${field}(input: $input) ${UPDATE_SELECTION} }`;
        const data = await send(query, { input: { CallId: callId, ...input } });
        const update = data[field] as { Consent?: ConsentRecord | null } | null | undefined;
        return update?.Consent ?? null;
    };
    return {
        start: (input) => mutate('startMeetingConsent', 'StartMeetingConsentInput', { ...input }),
        get: async () => {
            const query = `query Consent($CallId: ID!) { getMeetingConsent(CallId: $CallId) {
                CallId CaptureAllowed Consent { ${CONSENT_RECORD_FIELDS} }
                Settings { Mode Rule ConsentPhrases ObjectionPhrases AllowPerMeetingOverride KeepEvidenceScreenshots } } }`;
            const data = await send(query, { CallId: callId });
            const state = (data.getMeetingConsent || {}) as {
                Consent?: ConsentRecord | null;
                CaptureAllowed?: boolean;
                Settings?: ConsentSettingsView | null;
            };
            return { record: state.Consent ?? null, captureAllowed: state.CaptureAllowed === true, settings: state.Settings ?? null };
        },
        recordNotice: (text) => mutate('recordMeetingConsentNotice', 'RecordMeetingConsentNoticeInput', { Channel: 'chat', Text: text }),
        setParticipants: (names) =>
            mutate('setMeetingConsentParticipants', 'SetMeetingConsentParticipantsInput', { Participants: names }),
        recordEvent: (input) =>
            mutate('recordMeetingConsentEvent', 'RecordMeetingConsentEventInput', { ...input, Source: input.Source ?? 'chat' }),
        addEvidence: (input) =>
            mutate('addMeetingConsentEvidence', 'AddMeetingConsentEvidenceInput', { ...input, Kind: 'screenshot' }),
        verifyEvidence: async (evidenceId) => {
            const query = `mutation Verify($input: VerifyMeetingConsentEvidenceInput!) {
                verifyMeetingConsentEvidence(input: $input) { CallId } }`;
            try {
                await send(query, { input: { CallId: callId, EvidenceId: evidenceId } });
            } catch (error) {
                // A check is supporting evidence; the meeting never waits on it.
                log.warn(`[consent] the evidence check of ${evidenceId} did not run: ${String(error)}`);
            }
        },
    };
}
