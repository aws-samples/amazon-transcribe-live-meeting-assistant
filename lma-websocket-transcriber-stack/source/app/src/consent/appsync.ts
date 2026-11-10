/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The meeting consent mutations, called on the AppSync GraphQL API with this
 * task's role (SigV4). The meeting consent resolver treats a request signed by
 * one of LMA's own roles as a backend role: it may start a meeting's consent,
 * record its notice, relay the owner's attestation, record a reply read from
 * the meeting, and add evidence it stored under the meeting's folder. The task
 * role is granted these mutations and nothing else on the API.
 */
import { createHash, createHmac } from 'node:crypto';

import { defaultProvider } from '@aws-sdk/credential-provider-node';
import { SignatureV4 } from '@smithy/signature-v4';

export interface GraphqlError {
    readonly message?: string;
    readonly errorType?: string;
}

export interface GraphqlResponse {
    readonly data?: Record<string, unknown> | null;
    readonly errors?: readonly GraphqlError[];
}

/** Sends one GraphQL operation. Injected in tests. */
export type GraphqlCaller = (query: string, variables: Record<string, unknown>) => Promise<GraphqlResponse>;

type SourceData = string | ArrayBuffer | ArrayBufferView;

const bytesOf = (data: SourceData): string | Buffer => {
    if (typeof data === 'string') {
        return data;
    }
    if (ArrayBuffer.isView(data)) {
        return Buffer.from(data.buffer, data.byteOffset, data.byteLength);
    }
    return Buffer.from(data);
};

/** SHA-256 for the signer, from node:crypto (keyed for HMAC when given a secret). */
class NodeSha256 {
    private hash: ReturnType<typeof createHash> | ReturnType<typeof createHmac>;

    private readonly secret: string | Buffer | undefined;

    constructor(secret?: SourceData) {
        this.secret = secret === undefined ? undefined : bytesOf(secret);
        this.hash = this.secret !== undefined ? createHmac('sha256', this.secret) : createHash('sha256');
    }

    update(data: SourceData): void {
        this.hash.update(bytesOf(data));
    }

    async digest(): Promise<Uint8Array> {
        return new Uint8Array(this.hash.digest());
    }

    reset(): void {
        this.hash = this.secret !== undefined ? createHmac('sha256', this.secret) : createHash('sha256');
    }
}

/** Long enough for verifyMeetingConsentEvidence, which waits for a vision model's answer. */
const REQUEST_TIMEOUT_MS = 35_000;

/** A GraphqlCaller that signs each request with this task's credentials. */
export const createSignedGraphqlCaller = (endpoint: string, region: string): GraphqlCaller => {
    const url = new URL(endpoint);
    const signer = new SignatureV4({
        service: 'appsync',
        region,
        credentials: defaultProvider(),
        sha256: NodeSha256,
    });
    return async (query, variables) => {
        const body = JSON.stringify({ query, variables });
        const signed = await signer.sign({
            method: 'POST',
            protocol: url.protocol,
            hostname: url.hostname,
            path: url.pathname,
            query: {},
            headers: { host: url.hostname, 'content-type': 'application/json' },
            body,
        });
        const headers: Record<string, string> = {};
        for (const [name, value] of Object.entries(signed.headers)) {
            // fetch sets Host itself, to the same value that was signed.
            if (name.toLowerCase() !== 'host') {
                headers[name] = String(value);
            }
        }
        const response = await fetch(endpoint, {
            method: 'POST',
            headers,
            body,
            signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS),
        });
        const text = await response.text();
        try {
            return JSON.parse(text) as GraphqlResponse;
        } catch {
            return { errors: [{ message: `HTTP ${response.status} from the GraphQL API` }] };
        }
    };
};

/**
 * The whole record, as docs/meeting-consent.md asks a writer to select: a
 * subscriber to onUpdateMeetingConsent receives the fields the writer selected.
 */
const RECORD_FIELDS = `
  Mode Rule Status CaptureAllowed NoticeText NoticePostedAt
  Notices { Channel Text PostedAt PostedBy }
  Participants ParticipantsSeen
  Events { EventId ParticipantName Decision Source Text OccurredAt RecordedAt RecordedBy }
  Attestation { AttestedBy Method Statement AttestedAt RecordedBy }
  Evidence { EvidenceId Kind S3Key CapturedAt AddedAt Note SignedUrl }
  StatusHistory { Status CaptureAllowed At Reason }
  StartedAt StartedBy UpdatedAt Version
`;

const VERIFICATION_FIELDS = `
  Evidence { EvidenceId Kind S3Key CapturedAt AddedAt Note SignedUrl
    Verification { Verdict MissingParticipants Readable NoticeVisible NoticeMatches
      Replies { Name Text Decision } AnyObjection Confidence Summary ModelId VerifiedAt VerifiedBy
      InputTokens OutputTokens }
    ImageDeletedAt }
`;

const mutation = (name: string, inputType: string, fields: string = RECORD_FIELDS): string => `
mutation ($input: ${inputType}!) {
  ${name}(input: $input) { CallId Owner SharedWith Consent { ${fields} } }
}`;

/** The meeting's owner, read before anything is written to its consent record. */
export const MEETING_OWNER = `
query ($CallId: ID!) {
  getCall(CallId: $CallId) { CallId Owner }
}`;

export const START = mutation('startMeetingConsent', 'StartMeetingConsentInput');
export const RECORD_NOTICE = mutation('recordMeetingConsentNotice', 'RecordMeetingConsentNoticeInput');
export const RECORD_EVENT = mutation('recordMeetingConsentEvent', 'RecordMeetingConsentEventInput');
export const ATTEST = mutation('attestMeetingConsent', 'AttestMeetingConsentInput');
export const ADD_EVIDENCE = mutation('addMeetingConsentEvidence', 'AddMeetingConsentEvidenceInput');
export const VERIFY_EVIDENCE = mutation(
    'verifyMeetingConsentEvidence',
    'VerifyMeetingConsentEvidenceInput',
    RECORD_FIELDS.replace(/\n\s*Evidence \{[^}]*\}/, VERIFICATION_FIELDS)
);

/** The parts of a consent record this transcriber acts on. */
export interface ConsentRecordView {
    /** The meeting's Owner, as the mutation answered it. */
    readonly owner?: string;
    readonly mode?: string;
    readonly status?: string;
    readonly captureAllowed: boolean;
    readonly noticePosted: boolean;
    readonly attested: boolean;
    readonly reason?: string;
    readonly evidence: ReadonlyArray<{ readonly evidenceId: string; readonly s3Key: string; readonly verdict?: string }>;
}

/** A refusal or failure from the API, with whether trying again may succeed. */
export class ConsentApiError extends Error {
    readonly meetingMissing: boolean;

    constructor(message: string, meetingMissing: boolean) {
        super(message);
        this.name = 'ConsentApiError';
        this.meetingMissing = meetingMissing;
    }
}

const asRecord = (value: unknown): Record<string, unknown> | undefined =>
    value !== null && typeof value === 'object' && !Array.isArray(value)
        ? (value as Record<string, unknown>)
        : undefined;

/** The view of the `Consent` a mutation answered with, and the meeting's Owner. */
export const viewOf = (consent: unknown, owner?: unknown): ConsentRecordView => {
    const record = asRecord(consent) ?? {};
    const history = Array.isArray(record['StatusHistory']) ? record['StatusHistory'] : [];
    const latest = asRecord(history[history.length - 1]);
    const evidence = (Array.isArray(record['Evidence']) ? record['Evidence'] : [])
        .map(asRecord)
        .filter((item): item is Record<string, unknown> => item !== undefined)
        .map((item) => ({
            evidenceId: String(item['EvidenceId'] ?? ''),
            s3Key: String(item['S3Key'] ?? ''),
            verdict:
                typeof asRecord(item['Verification'])?.['Verdict'] === 'string'
                    ? String(asRecord(item['Verification'])?.['Verdict'])
                    : undefined,
        }));
    return {
        owner: typeof owner === 'string' ? owner : undefined,
        mode: typeof record['Mode'] === 'string' ? record['Mode'] : undefined,
        status: typeof record['Status'] === 'string' ? record['Status'] : undefined,
        captureAllowed: record['CaptureAllowed'] === true,
        noticePosted: Boolean(record['NoticePostedAt']),
        attested: asRecord(record['Attestation']) !== undefined,
        reason: typeof latest?.['Reason'] === 'string' ? latest['Reason'] : undefined,
        evidence,
    };
};

/** The consent mutations, each answering with the record as written. */
export class ConsentApi {
    private readonly call: GraphqlCaller;

    constructor(call: GraphqlCaller) {
        this.call = call;
    }

    private async run(document: string, field: string, input: Record<string, unknown>): Promise<ConsentRecordView> {
        let response: GraphqlResponse;
        try {
            response = await this.call(document, { input });
        } catch (error) {
            throw new ConsentApiError(`${field} could not be sent: ${String(error)}`, false);
        }
        const errors = response.errors ?? [];
        if (errors.length > 0) {
            const message = errors.map((error) => error.message ?? error.errorType ?? 'error').join('; ');
            // The resolver tells a backend role "no meeting <CallId>" until the
            // meeting's row exists, which the call event processor writes a moment
            // after the START event this task put on the stream.
            throw new ConsentApiError(`${field}: ${message}`, /\bno meeting\b/.test(message));
        }
        const answer = asRecord(asRecord(response.data)?.[field]);
        return viewOf(answer?.['Consent'], answer?.['Owner']);
    }

    /**
     * The meeting's Owner, or null while its row does not exist yet. getCall
     * answers a backend role with the row as stored.
     */
    async meetingOwner(callId: string): Promise<string | null> {
        let response: GraphqlResponse;
        try {
            response = await this.call(MEETING_OWNER, { CallId: callId });
        } catch (error) {
            throw new ConsentApiError(`getCall could not be sent: ${String(error)}`, false);
        }
        const errors = response.errors ?? [];
        if (errors.length > 0) {
            throw new ConsentApiError(
                `getCall: ${errors.map((error) => error.message ?? error.errorType ?? 'error').join('; ')}`,
                false
            );
        }
        const call = asRecord(asRecord(response.data)?.['getCall']);
        if (!call) {
            return null;
        }
        return typeof call['Owner'] === 'string' ? call['Owner'] : '';
    }

    start(input: Record<string, unknown>): Promise<ConsentRecordView> {
        return this.run(START, 'startMeetingConsent', input);
    }

    recordNotice(input: Record<string, unknown>): Promise<ConsentRecordView> {
        return this.run(RECORD_NOTICE, 'recordMeetingConsentNotice', input);
    }

    recordEvent(input: Record<string, unknown>): Promise<ConsentRecordView> {
        return this.run(RECORD_EVENT, 'recordMeetingConsentEvent', input);
    }

    attest(input: Record<string, unknown>): Promise<ConsentRecordView> {
        return this.run(ATTEST, 'attestMeetingConsent', input);
    }

    addEvidence(input: Record<string, unknown>): Promise<ConsentRecordView> {
        return this.run(ADD_EVIDENCE, 'addMeetingConsentEvidence', input);
    }

    verifyEvidence(input: Record<string, unknown>): Promise<ConsentRecordView> {
        return this.run(VERIFY_EVIDENCE, 'verifyMeetingConsentEvidence', input);
    }
}
