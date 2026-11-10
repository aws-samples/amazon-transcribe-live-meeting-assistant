/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * One meeting's consent, from its START to its end.
 *
 * The client's consent step arrives with START, before the meeting exists: the
 * meeting's row is written by the call event processor a moment after this task
 * puts the START event on the stream. So the session waits for the row, then
 * records the step through the meeting consent API -- start, the notice, and in
 * an opt-in meeting the owner's attestation -- and acts on the `CaptureAllowed`
 * each write answers with. Objections and evidence the client sends later are
 * written in order after that.
 *
 * Capture is gated here and nowhere else in the task: `gateOpen` says whether
 * audio may be transcribed and recorded now. A notify meeting is open from the
 * start and closes on an objection; an opt-in meeting opens only when the record
 * says it may be captured. Closing is final for the session: the meeting is
 * ended, and capture is not resumed on that connection.
 */
import { randomBytes } from 'node:crypto';

import { ConsentApi, ConsentApiError, ConsentRecordView } from './appsync';
import {
    ClientConsent,
    ConsentRule,
    MAX_CLIENT_EVIDENCE,
    MAX_NAME_LENGTH,
    MAX_TEXT_LENGTH,
    MEETING_NOT_OWNED_MESSAGE,
    MODE_OPT_IN,
    MODE_NOTIFY,
    decodeEvidenceImage,
    defaultStatement,
    evidenceKey,
    noticeChannelFor,
} from './rules';

export interface ConsentLog {
    info(message: string): void;
    warn(message: string): void;
    error(message: string): void;
}

export interface ConsentSessionDeps {
    /** Undefined when this task has no GraphQL endpoint to record consent with. */
    readonly api: ConsentApi | undefined;
    /**
     * The Cognito username of the verified caller who started the meeting on
     * this connection. The step is recorded only on a meeting that user owns.
     */
    readonly callerUsername: string | undefined;
    /** Stores an evidence image in the recordings bucket. */
    readonly putEvidence: (key: string, bytes: Buffer, contentType: string) => Promise<void>;
    /** The recordings bucket's evidence prefix, ending in "/". */
    readonly evidencePrefix: string;
    /** Sends a JSON frame to the client; a closed socket is ignored. */
    readonly send: (frame: Record<string, unknown>) => void;
    /**
     * Capture may start (open) or must stop (closed). Called once to open and at
     * most once to close. A close ends the meeting, except when `discard` is set:
     * the meeting is not the caller's, so only this connection's capture of it is
     * dropped and nothing is written to it.
     */
    readonly onGate: (open: boolean, reason: string, discard?: boolean) => void;
    readonly log: ConsentLog;
    readonly now?: () => Date;
    readonly sleep?: (ms: number) => Promise<void>;
    /** How long to wait for the meeting's row before giving up. */
    readonly meetingWaitMs?: number;
}

export interface ObjectionInput {
    readonly participantName?: string;
    readonly source?: string;
    readonly text?: string;
}

export interface EvidenceInput {
    readonly image?: unknown;
    readonly capturedAt?: unknown;
    readonly note?: unknown;
}

const DEFAULT_MEETING_WAIT_MS = 30_000;
const RETRY_DELAYS_MS = [250, 500, 1000, 1500, 2000];

const clip = (value: unknown, limit: number): string | undefined => {
    if (typeof value !== 'string') {
        return undefined;
    }
    const trimmed = value.trim();
    return trimmed ? trimmed.slice(0, limit) : undefined;
};

const isoOrUndefined = (value: unknown): string | undefined => {
    if (typeof value !== 'string') {
        return undefined;
    }
    const parsed = new Date(value);
    return Number.isNaN(parsed.getTime()) ? undefined : parsed.toISOString();
};

export class ConsentSession {
    readonly callId: string;

    readonly mode: typeof MODE_NOTIFY | typeof MODE_OPT_IN;

    private readonly rule: ConsentRule | null;

    /** The record decides whether capture may happen, whatever the mode; see StartDecision. */
    private readonly requireRecord: boolean;

    private readonly consent: ClientConsent;

    private readonly deps: ConsentSessionDeps;

    private open: boolean;

    private stopped = false;

    private started = false;

    private evidenceSent = 0;

    /** Set once the meeting turned out not to be the caller's: nothing more is written. */
    private notOwned = false;

    private chain: Promise<void> = Promise.resolve();

    constructor(
        callId: string,
        decision: {
            mode: typeof MODE_NOTIFY | typeof MODE_OPT_IN;
            rule: ConsentRule | null;
            gateOpen: boolean;
            requireRecord?: boolean;
        },
        consent: ClientConsent,
        deps: ConsentSessionDeps
    ) {
        this.callId = callId;
        this.mode = decision.mode;
        this.rule = decision.rule;
        this.consent = consent;
        this.deps = deps;
        this.open = decision.gateOpen;
        this.requireRecord = decision.requireRecord === true;
    }

    /** Whether audio may be transcribed and recorded now. */
    get gateOpen(): boolean {
        return this.open && !this.stopped;
    }

    /** Whether capture was stopped for good on this connection. */
    get isStopped(): boolean {
        return this.stopped;
    }

    /** Every write queued so far, finished; for tests and for orderly shutdown. */
    settled(): Promise<void> {
        return this.chain;
    }

    /** Record the client's consent step. Runs once; later calls do nothing. */
    begin(): Promise<void> {
        if (this.started) {
            return this.chain;
        }
        this.started = true;
        return this.enqueue(() => this.recordStep());
    }

    /**
     * A participant objected, as the client's user reports. Capture stops at once,
     * before the objection is written, and the meeting ends.
     */
    objection(input: ObjectionInput): Promise<void> {
        const name = clip(input.participantName, MAX_NAME_LENGTH) ?? 'A participant';
        const source = input.source === 'chat' ? 'chat' : 'voice';
        const text = clip(input.text, MAX_TEXT_LENGTH);
        this.stop('a participant objected');
        // Before the connection closes, which it does as the meeting ends.
        this.deps.send(
            this.frame({
                captureAllowed: false,
                code: 'OBJECTION_RECEIVED',
                message: 'Recording and transcription stopped. The objection is being recorded on the meeting.',
            })
        );
        return this.enqueue(async () => {
            const view = await this.write('recordEvent', {
                CallId: this.callId,
                ParticipantName: name,
                Decision: 'objection',
                Source: source,
                ...(text ? { Text: text } : {}),
                OccurredAt: this.now().toISOString(),
            });
            if (view) {
                this.report(view);
            }
        });
    }

    /** An evidence image from the client: stored, added to the record, and checked. */
    evidence(input: EvidenceInput): Promise<void> {
        if (this.evidenceSent >= MAX_CLIENT_EVIDENCE) {
            this.deps.send(this.frame({ code: 'EVIDENCE_REFUSED', message: `at most ${MAX_CLIENT_EVIDENCE} evidence images can be sent for a meeting` }));
            return this.chain;
        }
        const image = decodeEvidenceImage(input.image);
        if ('error' in image) {
            this.deps.send(this.frame({ code: 'EVIDENCE_REFUSED', message: image.error }));
            return this.chain;
        }
        // Counted once accepted, so a refused image does not use up the allowance.
        this.evidenceSent += 1;
        const capturedAt = isoOrUndefined(input.capturedAt) ?? this.now().toISOString();
        const note = clip(input.note, MAX_TEXT_LENGTH);
        return this.enqueue(async () => {
            const key = evidenceKey(
                this.deps.evidencePrefix,
                this.callId,
                this.now(),
                randomBytes(4).toString('hex'),
                image.extension
            );
            if (!key || !this.deps.api || this.notOwned) {
                this.evidenceSent -= 1;
                this.deps.send(this.frame({ code: 'EVIDENCE_NOT_STORED', message: 'evidence cannot be stored for this meeting' }));
                return;
            }
            try {
                await this.deps.putEvidence(key, image.bytes, image.contentType);
            } catch (error) {
                this.evidenceSent -= 1;
                this.deps.log.error(`[CONSENT]: [${this.callId}] - could not store an evidence image: ${String(error)}`);
                this.deps.send(this.frame({ code: 'EVIDENCE_NOT_STORED', message: 'the evidence image could not be stored' }));
                return;
            }
            const added = await this.write('addEvidence', {
                CallId: this.callId,
                S3Key: key,
                Kind: 'screenshot',
                CapturedAt: capturedAt,
                ...(note ? { Note: note } : {}),
            });
            if (!added) {
                this.deps.send(this.frame({ code: 'EVIDENCE_NOT_STORED', message: 'the evidence image could not be added to the consent record' }));
                return;
            }
            const entry = [...added.evidence].reverse().find((item) => item.s3Key === key);
            this.report(added, { evidence: { evidenceId: entry?.evidenceId, stored: true } });
            if (!entry?.evidenceId) {
                return;
            }
            // The vision-model check is supporting evidence and never changes the
            // record's status. A check that fails (the model not enabled, for
            // example) leaves the image stored and unchecked.
            const verified = await this.write('verifyEvidence', { CallId: this.callId, EvidenceId: entry.evidenceId }, 'warn');
            const checked = verified?.evidence.find((item) => item.evidenceId === entry.evidenceId);
            if (verified) {
                this.report(verified, { evidence: { evidenceId: entry.evidenceId, stored: true, verdict: checked?.verdict } });
            }
        });
    }

    private async recordStep(): Promise<void> {
        if (!this.deps.api) {
            this.fail('this deployment has no meeting consent API configured for the transcriber');
            return;
        }
        const started = await this.startWhenMeetingExists();
        if (!started) {
            return;
        }
        if (this.stopped) {
            // An objection arrived while the meeting was being created: its
            // record is started, and nothing is confirmed after the objection.
            return;
        }
        let view = started;
        if (view.status === 'objected') {
            // A client reconnecting to a meeting somebody objected to: its
            // confirmation was given before the objection and is not recorded again.
            this.report(view);
            return;
        }
        const consent = this.consent;
        let noticeFailed = false;
        if (consent.confirmed && !view.noticePosted) {
            const noticed = await this.write('recordNotice', {
                CallId: this.callId,
                Channel: noticeChannelFor(consent.method),
                ...(consent.noticeText ? { Text: consent.noticeText } : {}),
            });
            noticeFailed = noticed === undefined;
            view = noticed ?? view;
        }
        if (this.mode === MODE_OPT_IN && consent.method && !view.attested) {
            const attested = await this.write('attest', {
                CallId: this.callId,
                Method: consent.method,
                Statement: consent.statement ?? defaultStatement(consent.method, consent.client),
            });
            if (!attested) {
                this.fail('the owner\'s attestation could not be recorded');
                return;
            }
            view = attested;
        }
        this.report(
            view,
            noticeFailed
                ? { code: 'NOTICE_NOT_RECORDED', message: 'The notice was not recorded as posted on this meeting.' }
                : {}
        );
    }

    /**
     * The meeting's owner, once its row exists: retried until it does or the
     * wait runs out. undefined when it could not be read (already reported).
     */
    private async ownerWhenMeetingExists(deadline: number): Promise<string | undefined> {
        const api = this.deps.api as ConsentApi;
        for (let attempt = 0; ; attempt += 1) {
            let owner: string | null;
            try {
                owner = await api.meetingOwner(this.callId);
            } catch (error) {
                this.fail(`its owner could not be read: ${String((error as Error).message ?? error)}`);
                return undefined;
            }
            if (owner !== null) {
                return owner;
            }
            if (Date.now() >= deadline) {
                this.fail('the meeting was not created in time to record its consent');
                return undefined;
            }
            await this.sleep(RETRY_DELAYS_MS[Math.min(attempt, RETRY_DELAYS_MS.length - 1)] ?? 2000);
        }
    }

    /**
     * startMeetingConsent, once the meeting exists and is the caller's. A backend
     * role may write any meeting's consent, so the transcriber records a caller's
     * step only on a meeting that caller owns -- read before anything is written,
     * as START_VIDEO admits a video stream only for the caller's own session.
     */
    private async startWhenMeetingExists(): Promise<ConsentRecordView | undefined> {
        const api = this.deps.api as ConsentApi;
        const deadline = Date.now() + (this.deps.meetingWaitMs ?? DEFAULT_MEETING_WAIT_MS);
        const owner = await this.ownerWhenMeetingExists(deadline);
        if (owner === undefined) {
            return undefined;
        }
        if (!this.deps.callerUsername || owner !== this.deps.callerUsername) {
            this.notOwned = true;
            this.deps.log.warn(`[CONSENT]: [${this.callId}] - the meeting is not owned by the caller; not recording consent`);
            this.deps.send(this.frame({ captureAllowed: false, code: 'MEETING_NOT_OWNED', message: MEETING_NOT_OWNED_MESSAGE }));
            this.stop('the meeting belongs to another user', true);
            return undefined;
        }
        const input = {
            CallId: this.callId,
            Mode: this.mode,
            ...(this.rule ? { Rule: this.rule } : {}),
            ...(this.consent.lmaUser ? { LmaUser: this.consent.lmaUser } : {}),
            ...(this.consent.meetingName ? { MeetingName: this.consent.meetingName } : {}),
        };
        for (let attempt = 0; ; attempt += 1) {
            try {
                return await api.start(input);
            } catch (error) {
                const missing = error instanceof ConsentApiError && error.meetingMissing;
                if (!missing || Date.now() >= deadline) {
                    this.fail(
                        missing
                            ? 'the meeting was not created in time to record its consent'
                            : `its consent could not be started: ${String((error as Error).message ?? error)}`
                    );
                    return undefined;
                }
                await this.sleep(RETRY_DELAYS_MS[Math.min(attempt, RETRY_DELAYS_MS.length - 1)] ?? 2000);
            }
        }
    }

    private async write(
        operation: 'recordNotice' | 'recordEvent' | 'attest' | 'addEvidence' | 'verifyEvidence',
        input: Record<string, unknown>,
        level: 'error' | 'warn' = 'error'
    ): Promise<ConsentRecordView | undefined> {
        if (!this.deps.api || this.notOwned) {
            return undefined;
        }
        try {
            return await this.deps.api[operation](input);
        } catch (error) {
            this.deps.log[level](`[CONSENT]: [${this.callId}] - ${operation} failed: ${String((error as Error).message ?? error)}`);
            return undefined;
        }
    }

    /** Act on a record: open or close the gate, and tell the client. */
    private report(view: ConsentRecordView, extra: Record<string, unknown> = {}): void {
        if (view.captureAllowed && !this.open && !this.stopped) {
            this.open = true;
            this.deps.log.info(`[CONSENT]: [${this.callId}] - capture allowed (${view.status}): ${view.reason ?? ''}`);
            this.deps.onGate(true, view.reason ?? 'capture allowed');
        } else if (!view.captureAllowed) {
            this.stop(view.reason ?? `the consent status is ${view.status ?? 'unknown'}`);
        }
        this.deps.send(
            this.frame({
                status: view.status,
                captureAllowed: view.captureAllowed && !this.stopped,
                reason: view.reason,
                ...extra,
            })
        );
    }

    /**
     * Recording the step failed. An opt-in meeting is never captured without its
     * record; a notify meeting goes on, as notify allows capture until an objection.
     */
    private fail(why: string): void {
        if (this.mode === MODE_OPT_IN || this.requireRecord) {
            this.deps.log.error(`[CONSENT]: [${this.callId}] - not capturing this meeting: ${why}`);
            this.stop(why);
            this.deps.send(
                this.frame({
                    captureAllowed: false,
                    code: 'CONSENT_NOT_RECORDED',
                    message: `This meeting was not recorded or transcribed because ${why}.`,
                })
            );
            return;
        }
        this.deps.log.warn(`[CONSENT]: [${this.callId}] - capturing this notify meeting without a consent record: ${why}`);
        this.deps.send(
            this.frame({
                captureAllowed: !this.stopped,
                code: 'CONSENT_NOT_RECORDED',
                message: `The consent notice was not recorded on this meeting because ${why}.`,
            })
        );
    }

    private stop(reason: string, discard = false): void {
        if (this.stopped) {
            return;
        }
        this.stopped = true;
        this.open = false;
        this.deps.log.info(`[CONSENT]: [${this.callId}] - capture stopped: ${reason}`);
        this.deps.onGate(false, reason, discard);
    }

    private frame(fields: Record<string, unknown>): Record<string, unknown> {
        return { event: 'CONSENT_STATE', callId: this.callId, mode: this.mode, ...fields };
    }

    private enqueue(operation: () => Promise<void>): Promise<void> {
        this.chain = this.chain.then(operation).catch((error) => {
            this.deps.log.error(`[CONSENT]: [${this.callId}] - unexpected error: ${String(error)}`);
        });
        return this.chain;
    }

    private now(): Date {
        return (this.deps.now ?? (() => new Date()))();
    }

    private sleep(ms: number): Promise<void> {
        return (this.deps.sleep ?? ((delay: number) => new Promise<void>((resolve) => setTimeout(resolve, delay))))(ms);
    }
}
