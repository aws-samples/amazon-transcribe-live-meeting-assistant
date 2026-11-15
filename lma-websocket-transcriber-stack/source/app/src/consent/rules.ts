/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Meeting consent for the meetings this transcriber receives, with no AWS in it.
 *
 * The rules a meeting's consent record is judged by live in the Python
 * configuration layer's `lma_consent`, which the meeting consent resolver
 * applies on every write; this transcriber never evaluates a record itself, it
 * acts on the `CaptureAllowed` each write answers with. What is here is what it
 * decides before there is a record: which settings are in force, whether a
 * client's consent step satisfies them, and how a client's notice is filled
 * in. The setting names, defaults and placeholders are the Python module's;
 * consent/rules.test.ts holds the defaults to the seeder's file.
 *
 * Stream Audio and the Desktop Capture App have no participant list and no
 * view of the meeting chat, so the only opt-in rule they can satisfy is
 * "the meeting owner attests that everyone agreed". See docs/meeting-consent.md.
 */
import { posixifyFilename } from '../utils/common';

export const MODE_OFF = 'off';
export const MODE_NOTIFY = 'notify';
export const MODE_OPT_IN = 'opt_in';
export type ConsentMode = typeof MODE_OFF | typeof MODE_NOTIFY | typeof MODE_OPT_IN;
export const MODES: readonly ConsentMode[] = [MODE_OFF, MODE_NOTIFY, MODE_OPT_IN];

export const RULE_EACH_PARTICIPANT = 'each_participant';
export const RULE_OWNER_ATTESTS = 'owner_attests';
export type ConsentRule = typeof RULE_EACH_PARTICIPANT | typeof RULE_OWNER_ATTESTS;
export const RULES: readonly ConsentRule[] = [RULE_OWNER_ATTESTS, RULE_EACH_PARTICIPANT];

export type AttestationMethod = 'verbal' | 'chat' | 'invite_terms';
export const ATTESTATION_METHODS: readonly AttestationMethod[] = ['verbal', 'chat', 'invite_terms'];

/** What the transcriber does with a client that sends no consent step when one is required. */
export const CLIENTS_ACCEPT_WITH_WARNING = 'accept_with_warning';
export const CLIENTS_REJECT = 'reject';
export type ClientsWithoutConsent = typeof CLIENTS_ACCEPT_WITH_WARNING | typeof CLIENTS_REJECT;

export const MAX_NAME_LENGTH = 100;
export const MAX_TEXT_LENGTH = 500;
export const MAX_NOTICE_LENGTH = 2000;

export interface ConsentSettings {
    readonly mode: ConsentMode;
    readonly consentRule: ConsentRule;
    readonly notifyNoticeText: string;
    readonly optInNoticeText: string;
    readonly consentPhrases: readonly string[];
    readonly objectionPhrases: readonly string[];
    readonly allowPerMeetingOverride: boolean;
    readonly clientsWithoutConsent: ClientsWithoutConsent;
}

/**
 * The settings when the Meeting Consent section holds nothing: lma_consent's
 * DEFAULT_SETTINGS, which the seeder's MeetingConsent.json also carries.
 */
export const DEFAULT_SETTINGS: Readonly<Record<string, string | boolean>> = {
    mode: MODE_OFF,
    consentRule: RULE_EACH_PARTICIPANT,
    notifyNoticeText:
        '{LMA_USER} is using Live Meeting Assistant to record and transcribe this meeting ' +
        '({meeting}). If you do not agree, say so, and recording and transcription will stop.',
    optInNoticeText:
        '{LMA_USER} would like to record and transcribe this meeting ({meeting}) with Live ' +
        'Meeting Assistant. Nothing is recorded or transcribed until everyone has agreed.',
    consentPhrases: 'I consent, I agree, I accept',
    objectionPhrases:
        'I object, I do not consent, I don\'t consent, I do not agree, I don\'t agree, ' +
        'do not record, don\'t record, stop recording',
    allowPerMeetingOverride: true,
    clientsWithoutConsent: CLIENTS_ACCEPT_WITH_WARNING,
};

/** Comma-separated phrases as a list, trimmed, empty ones dropped. */
export const splitPhrases = (value: unknown): string[] => {
    const parts = Array.isArray(value)
        ? value.map((part) => String(part))
        : typeof value === 'string'
            ? value.split(',')
            : [];
    return parts.map((part) => part.trim()).filter((part) => part.length > 0);
};

/**
 * The deployment's settings: the section's values over the defaults. A value of
 * the wrong type, or a mode, rule or policy not in its list, is replaced by the
 * default rather than trusted, as lma_consent.effective_settings does.
 */
export const effectiveSettings = (values: Record<string, unknown> | undefined): ConsentSettings => {
    const merged: Record<string, unknown> = { ...DEFAULT_SETTINGS };
    for (const [key, fallback] of Object.entries(DEFAULT_SETTINGS)) {
        const value = values?.[key];
        if (value !== undefined && value !== null && typeof value === typeof fallback) {
            merged[key] = value;
        }
    }
    const mode = MODES.includes(merged['mode'] as ConsentMode) ? (merged['mode'] as ConsentMode) : MODE_OFF;
    const rule = RULES.includes(merged['consentRule'] as ConsentRule)
        ? (merged['consentRule'] as ConsentRule)
        : RULE_EACH_PARTICIPANT;
    const policy =
        merged['clientsWithoutConsent'] === CLIENTS_REJECT ? CLIENTS_REJECT : CLIENTS_ACCEPT_WITH_WARNING;
    return {
        mode,
        consentRule: rule,
        notifyNoticeText: String(merged['notifyNoticeText']),
        optInNoticeText: String(merged['optInNoticeText']),
        consentPhrases: splitPhrases(merged['consentPhrases']),
        objectionPhrases: splitPhrases(merged['objectionPhrases']),
        allowPerMeetingOverride: merged['allowPerMeetingOverride'] === true,
        clientsWithoutConsent: policy,
    };
};

/**
 * The notice with its placeholders filled, as lma_consent.render_notice fills
 * them: {LMA_USER}, {meeting}, {consent_phrase} and {objection_phrase}; any other
 * text in braces is left as written.
 */
export const renderNotice = (
    template: string,
    values: { lmaUser?: string; meeting?: string; consentPhrase?: string; objectionPhrase?: string }
): string => {
    const replacements: Record<string, string> = {
        '{LMA_USER}': values.lmaUser || 'The meeting organizer',
        '{meeting}': values.meeting || 'this meeting',
        '{consent_phrase}': values.consentPhrase ?? '',
        '{objection_phrase}': values.objectionPhrase ?? '',
    };
    return (template || '')
        .replace(/\{LMA_USER\}|\{meeting\}|\{consent_phrase\}|\{objection_phrase\}/g, (key) => replacements[key] ?? key)
        .slice(0, MAX_NOTICE_LENGTH);
};

/**
 * The rule a meeting from this transcriber's clients runs under, or why it
 * cannot run at all. Only opt-in has a rule; these clients meet it by the
 * owner's attestation, which a deployment requiring each participant's own
 * consent accepts only when it allows a meeting its own choice.
 */
export const ruleForCaptureClients = (
    settings: ConsentSettings
): { supported: boolean; rule: ConsentRule | null; reason?: string } => {
    if (settings.mode !== MODE_OPT_IN) {
        return { supported: true, rule: null };
    }
    if (settings.consentRule === RULE_OWNER_ATTESTS || settings.allowPerMeetingOverride) {
        return { supported: true, rule: RULE_OWNER_ATTESTS };
    }
    return {
        supported: false,
        rule: null,
        reason:
            'This deployment needs every participant\'s own consent before a meeting is recorded, ' +
            'which Stream Audio and the Desktop Capture App cannot collect. Use a Virtual Participant ' +
            'for this meeting, or ask an administrator to allow a different choice per meeting.',
    };
};

/** What the Meeting Consent settings say to a client's CONSENT_SETTINGS request. */
export const settingsFrame = (settings: ConsentSettings): Record<string, unknown> => {
    const rule = ruleForCaptureClients(settings);
    return {
        event: 'CONSENT_SETTINGS',
        available: true,
        mode: settings.mode,
        rule: rule.rule,
        supported: rule.supported,
        ...(rule.reason ? { reason: rule.reason } : {}),
        noticeTemplate:
            settings.mode === MODE_OPT_IN
                ? settings.optInNoticeText
                : settings.mode === MODE_NOTIFY
                    ? settings.notifyNoticeText
                    : '',
        consentPhrase: settings.consentPhrases[0] ?? '',
        objectionPhrase: settings.objectionPhrases[0] ?? '',
        clientsWithoutConsent: settings.clientsWithoutConsent,
    };
};

/** A client's consent step, as sent in the `consent` field of START. */
export interface ClientConsent {
    /** The mode the client's consent step was shown for. */
    readonly mode: ConsentMode;
    /** The user ticked the confirmation. */
    readonly confirmed: boolean;
    /** How everyone was told, or agreed: out loud, in the chat, or in the invitation's terms. */
    readonly method?: AttestationMethod;
    /** The notice as the client showed it, placeholders filled. */
    readonly noticeText?: string;
    readonly lmaUser?: string;
    readonly meetingName?: string;
    /** The user's own words about how everyone agreed. */
    readonly statement?: string;
    /** Which client sent it, for the attestation's statement: e.g. "Stream Audio". */
    readonly client?: string;
}

const clip = (value: unknown, limit: number): string | undefined => {
    if (typeof value !== 'string') {
        return undefined;
    }
    const trimmed = value.trim();
    return trimmed ? trimmed.slice(0, limit) : undefined;
};

/**
 * The `consent` field of a START frame, or undefined when there is none or it
 * is not an object. Unknown values of `mode` and `method` are dropped, so a
 * malformed step is judged as one that confirmed nothing.
 */
export const parseClientConsent = (raw: unknown): ClientConsent | undefined => {
    if (raw === null || typeof raw !== 'object' || Array.isArray(raw)) {
        return undefined;
    }
    const value = raw as Record<string, unknown>;
    const mode = MODES.includes(value['mode'] as ConsentMode) ? (value['mode'] as ConsentMode) : MODE_OFF;
    const method = ATTESTATION_METHODS.includes(value['method'] as AttestationMethod)
        ? (value['method'] as AttestationMethod)
        : undefined;
    return {
        mode,
        confirmed: value['confirmed'] === true,
        method,
        noticeText: clip(value['noticeText'], MAX_NOTICE_LENGTH),
        lmaUser: clip(value['lmaUser'], MAX_NAME_LENGTH),
        meetingName: clip(value['meetingName'], MAX_NAME_LENGTH),
        statement: clip(value['statement'], MAX_TEXT_LENGTH),
        client: clip(value['client'], 40),
    };
};

export type StartDecision =
    /** Capture as LMA always has, with no consent record. */
    | { readonly kind: 'capture'; readonly warning?: string }
    /** Refuse the meeting: nothing is recorded or transcribed, and no meeting is created. */
    | { readonly kind: 'reject'; readonly code: string; readonly message: string }
    /** Record the client's consent step on the meeting, and gate capture on the record. */
    | {
        readonly kind: 'consent';
        readonly mode: typeof MODE_NOTIFY | typeof MODE_OPT_IN;
        readonly rule: ConsentRule | null;
        /** Whether capture may start before the record answers: notify yes, opt-in no. */
        readonly gateOpen: boolean;
        /**
         * Decided without the settings, from the mode the client showed: the
         * record decides, so capture waits for it and stops if it cannot be written.
         */
        readonly requireRecord?: boolean;
    };

/**
 * What to do with a meeting whose START just arrived.
 *
 * `settings` is undefined when they could not be read. The client then read
 * them a moment ago for its consent step, so its step is recorded in the mode
 * it was shown for, and the resolver -- which reads the settings itself --
 * decides: capture waits for the record whatever the mode, and does not start
 * if it cannot be written. A client with no step is captured as before,
 * because nothing says a step is needed.
 */
export const decideAtStart = (
    settings: ConsentSettings | undefined,
    consent: ClientConsent | undefined
): StartDecision => {
    if (!settings) {
        if (consent && consent.mode !== MODE_OFF) {
            const decided = decideWith(consent.mode, RULE_OWNER_ATTESTS, consent);
            return decided.kind === 'consent' ? { ...decided, gateOpen: false, requireRecord: true } : decided;
        }
        return {
            kind: 'capture',
            warning: 'the meeting consent settings could not be read; capturing without a consent record',
        };
    }
    if (settings.mode === MODE_OFF) {
        return { kind: 'capture' };
    }
    if (!consent) {
        if (settings.clientsWithoutConsent === CLIENTS_REJECT) {
            return {
                kind: 'reject',
                code: 'CONSENT_REQUIRED',
                message:
                    'This deployment requires a consent step before a meeting is recorded or transcribed, ' +
                    'and this client did not send one. Update the client and try again.',
            };
        }
        return {
            kind: 'capture',
            warning:
                `the deployment's consent mode is ${settings.mode} and the client sent no consent step; ` +
                'capturing without a consent record (clientsWithoutConsent=accept_with_warning)',
        };
    }
    const rule = ruleForCaptureClients(settings);
    if (!rule.supported) {
        return { kind: 'reject', code: 'CONSENT_RULE_UNSUPPORTED', message: rule.reason ?? '' };
    }
    return decideWith(settings.mode, rule.rule, consent);
};

const decideWith = (
    mode: typeof MODE_NOTIFY | typeof MODE_OPT_IN,
    rule: ConsentRule | null,
    consent: ClientConsent
): StartDecision => {
    if (mode === MODE_NOTIFY) {
        return { kind: 'consent', mode, rule: null, gateOpen: true };
    }
    // An opt-in meeting needs the owner's confirmation that everyone agreed, given
    // for an opt-in request: a notify notice says nobody has to agree.
    if (consent.mode !== MODE_OPT_IN || !consent.confirmed || !consent.method) {
        return {
            kind: 'reject',
            code: 'CONSENT_NOT_CONFIRMED',
            message:
                'This meeting needs everyone\'s agreement before it is recorded or transcribed. Confirm that ' +
                'everyone agreed, and how, before starting.' +
                (consent.mode !== MODE_OPT_IN ? ' The consent settings changed; reload and try again.' : ''),
        };
    }
    return { kind: 'consent', mode, rule: rule ?? RULE_OWNER_ATTESTS, gateOpen: false };
};

/** What a client is told when the meeting its START names belongs to another user. */
export const MEETING_NOT_OWNED_MESSAGE =
    'This meeting was not recorded or transcribed because a meeting with this name belongs to another user. ' +
    'Start it again with a different meeting name.';

/** The notice channel a confirmation method implies. */
export const noticeChannelFor = (method: AttestationMethod | undefined): 'voice' | 'chat' | 'invite' => {
    if (method === 'verbal') {
        return 'voice';
    }
    if (method === 'invite_terms') {
        return 'invite';
    }
    return 'chat';
};

/** The attestation's statement when the user wrote none. */
export const defaultStatement = (method: AttestationMethod, client: string | undefined): string => {
    const where = client ? ` in ${client}` : '';
    if (method === 'verbal') {
        return `Confirmed${where} that everyone in the meeting agreed out loud after the request was read.`;
    }
    if (method === 'chat') {
        return `Confirmed${where} that everyone in the meeting agreed in the meeting chat after the request was posted.`;
    }
    return `Confirmed${where} that everyone agreed to recording and transcription in the meeting invitation's terms.`;
};

// ── evidence ────────────────────────────────────────────────────────────────

/** The consent evidence checker reads at most this much; a larger image is refused here. */
export const MAX_EVIDENCE_BYTES = 3_932_160;
/** Base64 text of at most MAX_EVIDENCE_BYTES, with room for padding. */
export const MAX_EVIDENCE_BASE64_LENGTH = Math.ceil(MAX_EVIDENCE_BYTES / 3) * 4;
/** Evidence images one meeting may send from its client; the record keeps at most 30 in all. */
export const MAX_CLIENT_EVIDENCE = 5;

export type EvidenceImage = { readonly bytes: Buffer; readonly contentType: string; readonly extension: string };

/**
 * A JPEG or PNG image from the `image` field of a CONSENT_EVIDENCE frame, or
 * why it cannot be kept. The type is read from the bytes, not from anything
 * the client says.
 */
export const decodeEvidenceImage = (value: unknown): EvidenceImage | { readonly error: string } => {
    if (typeof value !== 'string' || !value) {
        return { error: 'the evidence frame carries no image' };
    }
    const base64 = value.replace(/^data:image\/[a-z]+;base64,/, '');
    if (base64.length > MAX_EVIDENCE_BASE64_LENGTH) {
        return { error: `the evidence image is larger than ${MAX_EVIDENCE_BYTES} bytes` };
    }
    if (!/^[A-Za-z0-9+/]+={0,2}$/.test(base64)) {
        return { error: 'the evidence image is not base64' };
    }
    const bytes = Buffer.from(base64, 'base64');
    if (bytes.length === 0 || bytes.length > MAX_EVIDENCE_BYTES) {
        return { error: `the evidence image must be between 1 and ${MAX_EVIDENCE_BYTES} bytes` };
    }
    if (bytes.length >= 3 && bytes[0] === 0xff && bytes[1] === 0xd8 && bytes[2] === 0xff) {
        return { bytes, contentType: 'image/jpeg', extension: 'jpg' };
    }
    if (bytes.length >= 8 && bytes.subarray(0, 8).equals(Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]))) {
        return { bytes, contentType: 'image/png', extension: 'png' };
    }
    return { error: 'the evidence image must be a JPEG or PNG' };
};

/**
 * Where a meeting's evidence image is stored: under the meeting's own folder,
 * named as the meeting consent resolver names it (the CallId with every
 * character other than letters, digits, "_" and "." replaced by "_", leading
 * and trailing "_" removed), which is the only folder it accepts evidence from.
 * Undefined for a CallId with no such characters at all.
 */
export const evidenceKey = (
    prefix: string,
    callId: string,
    at: Date,
    unique: string,
    extension: string
): string | undefined => {
    const folder = posixifyFilename(callId);
    if (!folder) {
        return undefined;
    }
    const stamp = at.toISOString().replace(/[-:]/g, '').replace(/\.\d+Z$/, 'Z');
    return `${prefix}${folder}/screenshot-${stamp}-${unique}.${extension}`;
};
