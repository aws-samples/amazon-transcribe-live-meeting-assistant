/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The meeting consent flow of the Virtual Participant (docs/meeting-consent.md,
 * "Virtual Participant").
 *
 * The meeting's consent record, its rules and its API are the AI stack's: the
 * meeting consent resolver decides the record's status and whether the meeting
 * may be captured (`CaptureAllowed`), and this module acts on that answer. It
 * is a capture client that follows the steps that page lists:
 *
 * 1. Before joining, it works out the meeting's mode: the per-meeting choice
 *    stored on the VP's row when it was created, if the deployment allows it,
 *    or else the deployment's own (the Meeting Consent section of the
 *    configuration). With mode Off nothing below happens and the VP behaves
 *    exactly as it always has. An opt-in meeting, or one whose mode cannot be
 *    read, starts with capture withheld: the transcript, the audio recording,
 *    the voice assistant and the screen recording do not start.
 * 2. Once admitted (`attach`, called by each platform handler after its
 *    introduction), it creates the meeting record if transcription has not,
 *    starts the meeting's consent, posts the notice in the chat and records
 *    that it was posted.
 * 3. It keeps the participant list current (consent-roster.ts), reads each
 *    chat reply and each spoken reply that is consent or an objection
 *    (consent-matcher.ts) and records it, and every few seconds reads the
 *    record again, which is how an attestation or an objection entered on the
 *    meeting page reaches it. Spoken replies come from the transcription
 *    service while the meeting is captured, and from the consent listener
 *    (consent-listener.ts), which the flow runs while it withholds capture.
 * 4. It gates capture on `CaptureAllowed` in every answer, with the same switch
 *    the "LMA pause" and "LMA start" chat commands use (`details.start`), so
 *    starting later is the same path as resuming from a pause.
 * 5. When consent is granted, and on every objection, it keeps screenshots of
 *    the chat and the participant list as evidence and asks for their check.
 *
 * An objection read in the chat or heard stops capture at once, before
 * anything is written; a reply is otherwise only acted on through the record's answer, so
 * the VP and the record never disagree about whether the meeting is captured
 * for longer than one round trip.
 */
import { details, matchesChatCommand } from './details.js';
import {
    ConsentDecision,
    ConsentMode,
    ConsentRule,
    atLeastAsStrict,
    classifyChatReply,
    classifySpokenReply,
    isConsentMode,
    isConsentRule,
    noticeChatMessages,
    normalizeParticipantName,
    normalizeReplyText,
    renderNotice,
    splitPhrases,
    strictness,
} from './consent-matcher.js';
import { ConsentRoster, UNIDENTIFIED_PARTICIPANT, onParticipantSeen } from './consent-roster.js';
import type { ConsentApi, ConsentRecord, RecordEventInput, ReplySource } from './consent-api.js';
import { ConsentApiError } from './consent-api.js';
import { consentEvidenceKey, EvidenceShot } from './consent-evidence.js';
import { CONSENT_HELP_LINE, setConsentHelpActive } from './consent-help.js';
import { onSpokenUtterance } from './spoken-replies.js';
import { createLogger } from './logger.js';

const log = createLogger('meeting-consent');

// ─── Settings ────────────────────────────────────────────────────────────────

export interface ConsentSettings {
    mode: ConsentMode;
    rule: ConsentRule;
    notifyNoticeText: string;
    optInNoticeText: string;
    consentPhrases: string[];
    objectionPhrases: string[];
    allowPerMeetingOverride: boolean;
    leaveOnObjection: boolean;
}

/**
 * The same values as lma_consent.DEFAULT_SETTINGS and the configuration
 * seeder's MeetingConsent.json, which consent-wiring.test.ts holds them to.
 */
export const DEFAULT_CONSENT_SETTINGS: ConsentSettings = {
    mode: 'off',
    rule: 'each_participant',
    notifyNoticeText:
        '{LMA_USER} is using Live Meeting Assistant to record and transcribe this meeting ({meeting}). ' +
        'If you do not agree, reply "{objection_phrase}" in the chat or say so, and recording and transcription will stop.',
    optInNoticeText:
        '{LMA_USER} would like to record and transcribe this meeting ({meeting}) with Live Meeting Assistant. ' +
        'Nothing is recorded or transcribed until everyone has agreed. ' +
        'Reply "{consent_phrase}" to agree, or "{objection_phrase}" to decline.',
    consentPhrases: ['I consent', 'I agree', 'I accept'],
    objectionPhrases: [
        'I object',
        'I do not consent',
        "I don't consent",
        'I do not agree',
        "I don't agree",
        'do not record',
        "don't record",
        'stop recording',
    ],
    allowPerMeetingOverride: true,
    leaveOnObjection: false,
};

/**
 * The section's values over the defaults, as lma_consent.effective_settings
 * merges them: a value of the wrong type, or a mode or rule not in the lists,
 * is the default rather than trusted.
 */
export function effectiveConsentSettings(values: Record<string, unknown> | null | undefined): ConsentSettings {
    const v = values || {};
    const text = (key: string, fallback: string): string =>
        typeof v[key] === 'string' && (v[key] as string).trim() ? (v[key] as string) : fallback;
    const phrases = (key: string, fallback: string[]): string[] => {
        const list = splitPhrases(v[key]);
        return typeof v[key] === 'string' && list.length > 0 ? list : fallback;
    };
    return {
        mode: isConsentMode(v.mode) ? v.mode : DEFAULT_CONSENT_SETTINGS.mode,
        rule: isConsentRule(v.consentRule) ? v.consentRule : DEFAULT_CONSENT_SETTINGS.rule,
        notifyNoticeText: text('notifyNoticeText', DEFAULT_CONSENT_SETTINGS.notifyNoticeText),
        optInNoticeText: text('optInNoticeText', DEFAULT_CONSENT_SETTINGS.optInNoticeText),
        consentPhrases: phrases('consentPhrases', DEFAULT_CONSENT_SETTINGS.consentPhrases),
        objectionPhrases: phrases('objectionPhrases', DEFAULT_CONSENT_SETTINGS.objectionPhrases),
        allowPerMeetingOverride:
            typeof v.allowPerMeetingOverride === 'boolean' ? v.allowPerMeetingOverride : DEFAULT_CONSENT_SETTINGS.allowPerMeetingOverride,
        leaveOnObjection: typeof v.leaveOnObjection === 'boolean' ? v.leaveOnObjection : DEFAULT_CONSENT_SETTINGS.leaveOnObjection,
    };
}

/** The choice stored on the VP's row when it was created; empty means the deployment's. */
export interface VpConsentChoice {
    mode?: string | null;
    rule?: string | null;
}

/** `unknown`: the deployment's settings could not be read, so the mode is not known until the API answers. */
export type PlannedMode = ConsentMode | 'unknown';

export interface ConsentPlan {
    mode: PlannedMode;
    rule: ConsentRule | null;
    /** What to ask startMeetingConsent for; empty means the deployment's choice. */
    requested: { Mode?: ConsentMode; Rule?: ConsentRule };
}

/**
 * The mode this meeting will run in, worked out before joining.
 *
 * The resolver has the last word (it refuses a choice the deployment does not
 * allow, and the VP then starts with the deployment's), and this applies the
 * same rule ahead of it so the VP knows before joining whether to withhold
 * capture. Settings that could not be read leave the mode unknown unless the
 * meeting asked for opt-in, which nothing can make less strict.
 */
export function planConsent(choice: VpConsentChoice | null, settings: ConsentSettings | null): ConsentPlan {
    const askedMode = isConsentMode(choice?.mode) ? (choice?.mode as ConsentMode) : null;
    const askedRule = isConsentRule(choice?.rule) ? (choice?.rule as ConsentRule) : null;
    if (!settings) {
        if (askedMode === 'opt_in') {
            return { mode: 'opt_in', rule: askedRule, requested: { Mode: 'opt_in', ...(askedRule ? { Rule: askedRule } : {}) } };
        }
        return { mode: 'unknown', rule: null, requested: askedMode ? { Mode: askedMode } : {} };
    }
    if (!askedMode && !askedRule) {
        return { mode: settings.mode, rule: settings.mode === 'opt_in' ? settings.rule : null, requested: {} };
    }
    const mode = askedMode ?? settings.mode;
    const rule = mode === 'opt_in' ? askedRule ?? settings.rule : null;
    const permitted =
        settings.allowPerMeetingOverride ||
        atLeastAsStrict(strictness(mode, rule), strictness(settings.mode, settings.mode === 'opt_in' ? settings.rule : null));
    if (!permitted) {
        log.warn(
            `[consent] this meeting asked for ${mode}${rule ? ` (${rule})` : ''}, which the deployment does not allow; ` +
                `using the deployment's ${settings.mode}`,
        );
        return { mode: settings.mode, rule: settings.mode === 'opt_in' ? settings.rule : null, requested: {} };
    }
    return {
        mode,
        rule,
        requested: { Mode: mode, ...(mode === 'opt_in' && rule ? { Rule: rule } : {}) },
    };
}

// ─── What the flow drives ────────────────────────────────────────────────────

/** The capture switches: the transcript and recordings, and the screen recording. */
export interface CaptureControl {
    /** Whether the meeting is being captured now (`details.start`). */
    isOn(): boolean;
    /** Stops the transcript, the audio recording and the voice assistant's input. */
    hold(): void;
    /** Starts them again, or for the first time. */
    release(): void;
    startVideo(): Promise<void>;
    /** Stops the screen recording for the rest of the meeting, keeping what it recorded. */
    stopVideo(): Promise<void>;
    /**
     * Starts (true) or stops (false) listening for spoken replies while capture
     * is withheld (consent-listener.ts). Stopped before capture is released.
     */
    listen?(on: boolean): void;
}

/** What a platform handler gives the flow once it is in the meeting. */
export interface ConsentMeetingIo {
    /** Posts messages in the meeting chat, as the introduction is posted. */
    sendMessages(messages: string[]): Promise<void>;
    /** Screenshots of the chat and the participant list; see consent-evidence.ts. */
    screenshots?(): Promise<EvidenceShot[]>;
    /** The platform's participant list, VP left out, or null where it has none. */
    readRoster?(): Promise<string[] | null>;
    /** Leaves the meeting after an objection, when the deployment says to. */
    leave(objector: string): void;
    /**
     * The platform shows no chat sender (the Teams browser join), so a consent
     * typed in the chat cannot be attributed; one said aloud can.
     */
    sendersHidden?: boolean;
}

export interface ConsentFlowDeps {
    capture: CaptureControl;
    /** The API for this meeting, or null where there is none (local test). */
    api: () => ConsentApi | null;
    /** Writes the VP's status detail line; null clears it. */
    setStatusDetail: (detail: string | null) => Promise<void>;
    /** Sends the meeting's START event, which creates the meeting record. */
    createMeeting: () => Promise<void>;
    /** Reports the VP as JOINED while it waits for consent in the meeting. */
    markJoined?: () => Promise<void>;
    upload: (key: string, body: Buffer) => Promise<void>;
    callId: () => string;
    lmaUser: string;
    meetingName: string;
    ownIdentities: string[];
    startMessages: () => string[];
    exitMessages: () => string[];
    clock?: () => number;
    sleep?: (ms: number) => Promise<void>;
    /** Runs `fn` every `ms`; answers a function that stops it. Off in tests. */
    every?: (fn: () => void, ms: number) => () => void;
    pollMs?: number;
    startAttempts?: number;
    startRetryMs?: number;
    noticeRepostMs?: number;
}

/** Posted after every notice: where replies are read. */
export const REPLY_CHANNELS_LINE = 'Replies may be typed in this chat or said aloud.';

/** Images kept per meeting by this flow; the record keeps 30. */
export const MAX_EVIDENCE_IMAGES = 24;

/** Reads of the VP row's consent choice before joining; the flow keeps trying after. */
const CHOICE_READ_ATTEMPTS = 3;

/** How long after joining a platform may take to report its first attendee count. */
const COUNT_GRACE_MS = 60_000;

/** Writes of one consent reply before it is given up on. */
const MAX_CONSENT_RECORD_ATTEMPTS = 20;

/** A reply repeated by the same sender within this long is one reply. */
const DUPLICATE_REPLY_MS = 5000;

/**
 * An objection that arrives by the other route (said aloud and typed in the
 * chat) within this long of one already acted on, while capture is still
 * stopped by it, is the same objection.
 */
const CROSS_SOURCE_OBJECTION_MS = 30_000;

/** How a reply's sender is recorded when the platform did not say. */
export const UNKNOWN_SENDER = 'Unidentified participant';

type VideoState = 'deferred' | 'live' | 'stopped';

export class MeetingConsentFlow {
    private readonly deps: ConsentFlowDeps;

    private readonly clock: () => number;

    private readonly sleep: (ms: number) => Promise<void>;

    readonly roster: ConsentRoster;

    plan: ConsentPlan = { mode: 'off', rule: null, requested: {} };

    settings: ConsentSettings = DEFAULT_CONSENT_SETTINGS;

    /** Whether the flow is withholding capture now. */
    held = false;

    private userPaused = false;

    private video: VideoState = 'live';

    private io: ConsentMeetingIo | null = null;

    private api: ConsentApi | null = null;

    private record: ConsentRecord | null = null;

    private started = false;

    private starting: Promise<boolean> | null = null;

    private noticePosted = false;

    /** What the last notice posted said, all its messages joined. */
    private postedNoticeText: string | null = null;

    private stopped = false;

    private leaving = false;

    private readonly posted = new Set<string>();

    private readonly pendingEvents: RecordEventInput[] = [];

    private readonly eventAttempts = new WeakMap<RecordEventInput, number>();

    private readonly objectionsInFlight = new Set<RecordEventInput>();

    private readonly handledObjections = new Set<string>();

    private readonly localDecisions = new Map<string, ConsentDecision>();

    private readonly recentReplies = new Map<string, number>();

    /** The objections acted on lately, by participant and route; see CROSS_SOURCE_OBJECTION_MS. */
    private recentObjections: Array<{ key: string; source: ReplySource; at: number }> = [];

    /** Whether the flow has asked for spoken replies to be listened for. */
    private listening = false;

    private evidenceUsed = 0;

    private lastParticipants: string | null = null;

    private lastDetail: string | null | undefined;

    private lastNoticeAt = 0;

    private attachedAt = 0;

    private readChoice: (() => Promise<VpConsentChoice | null>) | null = null;

    /** The meeting's own choice has not been read yet; consent is not started until it is. */
    private choiceUnread = false;

    private settingsRead: ConsentSettings | null = null;

    private noticeRepostTimer: ReturnType<typeof setTimeout> | null = null;

    private rosterSyncTimer: ReturnType<typeof setTimeout> | null = null;

    private stopTimers: Array<() => void> = [];

    private apiChain: Promise<unknown> = Promise.resolve();

    private evidenceChain: Promise<unknown> = Promise.resolve();

    private videoChain: Promise<void> = Promise.resolve();

    private polling = false;

    /** Settled once the flow has started the meeting's consent (or given up for now); for tests. */
    begun: Promise<void> = Promise.resolve();

    constructor(deps: ConsentFlowDeps) {
        this.deps = deps;
        this.clock = deps.clock ?? Date.now;
        this.sleep = deps.sleep ?? ((ms) => new Promise((resolve) => setTimeout(resolve, ms)));
        this.roster = new ConsentRoster(deps.ownIdentities, this.clock);
    }

    /** Whether this meeting runs a consent flow at all. */
    get active(): boolean {
        return (this.plan.mode !== 'off' || this.offChoicePending) && !this.stopped;
    }

    /**
     * The meeting chose Off in a deployment whose mode is not Off: capture runs
     * as usual, and the choice is put on the meeting's record (a consent record
     * in mode off), where the resolver checks it again and the meeting page
     * shows it. Nothing else of the flow runs while this is pending.
     */
    private offChoicePending = false;

    private get eachParticipant(): boolean {
        return this.plan.mode === 'opt_in' && this.plan.rule === 'each_participant';
    }

    /**
     * Before joining: decides the mode and, for an opt-in meeting or one whose
     * mode is unknown, withholds capture. Never throws.
     */
    async prepare(
        readChoice: () => Promise<VpConsentChoice | null>,
        readSection: () => Promise<Record<string, unknown> | null>,
    ): Promise<{ holdCapture: boolean; deferVideo: boolean }> {
        this.readChoice = readChoice;
        let choice: VpConsentChoice | null = null;
        this.choiceUnread = true;
        for (let attempt = 1; attempt <= CHOICE_READ_ATTEMPTS && this.choiceUnread; attempt += 1) {
            try {
                choice = await readChoice();
                this.choiceUnread = false;
            } catch (error) {
                log.warn(`[consent] could not read this meeting's consent choice (attempt ${attempt}): ${String(error)}`);
                if (attempt < CHOICE_READ_ATTEMPTS) await this.sleep(1000);
            }
        }
        let settings: ConsentSettings | null = null;
        try {
            settings = effectiveConsentSettings(await readSection());
            this.settings = settings;
        } catch (error) {
            log.warn(`[consent] could not read the Meeting Consent settings: ${String(error)}`);
        }
        this.settingsRead = settings;
        // A choice that could not be read may have been opt-in, which nothing
        // else can tell: the mode is unknown, and capture waits, until it is read.
        this.plan = this.choiceUnread ? { mode: 'unknown', rule: null, requested: {} } : planConsent(choice, settings);
        log.always(
            `[consent] mode=${this.plan.mode}${this.plan.rule ? ` rule=${this.plan.rule}` : ''}` +
                (Object.keys(this.plan.requested).length ? ' (chosen for this meeting)' : ''),
        );
        this.offChoicePending =
            this.plan.mode === 'off' && this.plan.requested.Mode === 'off' && !!settings && settings.mode !== 'off';
        if (!this.active) return { holdCapture: false, deferVideo: false };
        const hold = this.plan.mode === 'opt_in' || this.plan.mode === 'unknown';
        if (hold) {
            this.held = true;
            this.video = 'deferred';
            this.deps.capture.hold();
        }
        this.stopTimers.push(onParticipantSeen((name) => this.noteSpeaker(name)));
        this.stopTimers.push(onSpokenUtterance((heard) => void this.handleSpeech(heard.speaker, heard.text)));
        return { holdCapture: hold, deferVideo: hold };
    }

    /** In the meeting: the handler's chat and screenshots. Starts the flow in the background. */
    attach(io: ConsentMeetingIo): void {
        if (!this.active || this.io) return;
        this.io = io;
        if (!this.offChoicePending) setConsentHelpActive(true);
        this.begun = this.begin().catch((error) => log.error('[consent] the consent flow failed to start:', error));
        if (this.deps.every) {
            this.stopTimers.push(this.deps.every(() => void this.pollOnce(), this.deps.pollMs ?? 10_000));
        }
    }

    /** The meeting is over. */
    stop(): void {
        this.stopped = true;
        this.syncListening();
        setConsentHelpActive(false);
        for (const stopTimer of this.stopTimers) stopTimer();
        this.stopTimers = [];
        if (this.noticeRepostTimer) clearTimeout(this.noticeRepostTimer);
        if (this.rosterSyncTimer) clearTimeout(this.rosterSyncTimer);
    }

    private async begin(): Promise<void> {
        this.attachedAt = this.clock();
        if (this.held) {
            // In the meeting, though not capturing: JOINED rather than JOINING
            // on the platforms whose handler does not say so itself.
            try {
                await this.deps.markJoined?.();
            } catch (error) {
                log.warn(`[consent] could not report JOINED: ${String(error)}`);
            }
            await this.writeDetail('Waiting for consent…');
        }
        // In the meeting now, so a reply said aloud can be heard.
        this.syncListening();
        if (this.held) {
            // The meeting record has to exist before its consent can start, and
            // a withheld meeting has not started transcription, which is what
            // would otherwise create it.
            try {
                await this.deps.createMeeting();
            } catch (error) {
                log.warn(`[consent] could not send the meeting's START event: ${String(error)}`);
            }
        }
        const ok = await this.ensureStarted();
        if (!ok && this.plan.mode !== 'unknown' && !this.noticePosted) {
            // Participants are told even when the record cannot be written yet;
            // it is recorded once the API answers (pollOnce retries).
            await this.postNotice(this.localNotice(), false);
        }
    }

    /** Starts the meeting's consent, once; true when it has started. */
    private async ensureStarted(): Promise<boolean> {
        if (this.started) return true;
        if (!this.starting) {
            this.starting = this.startConsent().finally(() => {
                this.starting = null;
            });
        }
        return this.starting;
    }

    private async startConsent(): Promise<boolean> {
        this.api = this.api ?? this.deps.api();
        if (!this.api) return false;
        const attempts = this.deps.startAttempts ?? 24;
        let requested: Record<string, string> = { ...this.plan.requested };
        for (let attempt = 1; attempt <= attempts && !this.stopped; attempt += 1) {
            if (this.choiceUnread) {
                if (!(await this.rereadChoice())) {
                    await this.sleep(this.deps.startRetryMs ?? 5000);
                    continue;
                }
                this.offChoicePending =
                    this.plan.mode === 'off' &&
                    this.plan.requested.Mode === 'off' &&
                    !!this.settingsRead &&
                    this.settingsRead.mode !== 'off';
                if (this.plan.mode === 'off' && !this.offChoicePending) {
                    await this.finishWithoutConsent();
                    return true;
                }
                requested = { ...this.plan.requested };
            }
            try {
                const record = await this.api.start({ ...requested, LmaUser: this.deps.lmaUser, MeetingName: this.deps.meetingName });
                this.started = true;
                await this.afterStart(record);
                return true;
            } catch (error) {
                if (error instanceof ConsentApiError && error.choiceRefused && Object.keys(requested).length > 0) {
                    // The meeting's own choice was refused (the deployment does
                    // not allow it, or consent is already under way in another
                    // mode); the deployment's or the meeting's own applies.
                    log.warn(`[consent] startMeetingConsent refused this meeting's choice (${error.message}); starting with the deployment's`);
                    requested = {};
                    continue;
                }
                log.info(`[consent] consent could not be started yet (attempt ${attempt}): ${String(error)}`);
                if (this.plan.mode === 'notify' && !this.noticePosted && !this.stopped) {
                    // Capture is already on in a notify meeting, so participants
                    // are told now; the posting is recorded once consent starts.
                    await this.postNotice(this.localNotice(), false);
                }
                await this.sleep(this.deps.startRetryMs ?? 5000);
            }
        }
        return false;
    }

    /** One more read of the meeting's own choice; true once it has been read. */
    private async rereadChoice(): Promise<boolean> {
        if (!this.readChoice) return false;
        try {
            const choice = await this.readChoice();
            this.choiceUnread = false;
            this.plan = planConsent(choice, this.settingsRead);
            log.always(`[consent] this meeting's consent choice was read: mode=${this.plan.mode}${this.plan.rule ? ` rule=${this.plan.rule}` : ''}`);
            return true;
        } catch (error) {
            log.warn(`[consent] this meeting's consent choice still cannot be read; capture stays withheld: ${String(error)}`);
            return false;
        }
    }

    private async afterStart(started: ConsentRecord | null): Promise<void> {
        if (this.offChoicePending) {
            this.offChoicePending = false;
            if (started?.Mode === 'off') {
                log.always("[consent] this meeting's choice of mode off is on its record; nothing else to do");
                // Releases capture if it was withheld while the choice was unread.
                await this.finishWithoutConsent();
                return;
            }
            // Refused, or the record was already in another mode: run that.
            setConsentHelpActive(true);
        }
        let record = started;
        try {
            const state = await this.api!.get();
            record = state.record ?? record;
            const phrases = state.settings;
            if (phrases?.ConsentPhrases?.length) this.settings = { ...this.settings, consentPhrases: phrases.ConsentPhrases };
            if (phrases?.ObjectionPhrases?.length) this.settings = { ...this.settings, objectionPhrases: phrases.ObjectionPhrases };
        } catch (error) {
            log.warn(`[consent] could not read the consent settings back; using the configuration's phrases: ${String(error)}`);
        }
        // Objections already on the record (a VP that restarted) have been
        // acknowledged before; only new ones are acted on.
        for (const event of record?.Events ?? []) {
            if (event.Decision === 'objection' && event.EventId) this.handledObjections.add(event.EventId);
        }
        // Replies read before consent started go on the record first, so the
        // record the gate is applied from already holds them.
        await this.flushPendingEvents();
        await this.applyRecord(record);
        if (record?.Mode === 'off') {
            log.always('[consent] the meeting runs in mode off; nothing to do');
            await this.finishWithoutConsent();
            return;
        }
        if (!this.noticePosted) {
            await this.postNotice(record?.NoticeText || this.localNotice(), true);
        } else {
            // Posted before the record could be written: record now exactly
            // what was posted, as the record is evidence of it.
            const posted = this.postedNoticeText || record?.NoticeText || this.localNotice();
            await this.callApi((api) => api.recordNotice(posted));
        }
        await this.syncRoster();
    }

    /** The meeting turned out to run in mode off: capture as the VP always has. */
    private async finishWithoutConsent(): Promise<void> {
        const wasHeld = this.held;
        this.plan = { mode: 'off', rule: null, requested: {} };
        this.held = false;
        this.syncListening();
        if (wasHeld && !this.userPaused) {
            this.deps.capture.release();
            await this.say(this.deps.startMessages());
        }
        if (wasHeld) await this.writeDetail(null);
        await this.startDeferredVideo();
        this.stop();
    }

    /** The notice rendered here, for when the API cannot be reached. */
    private localNotice(): string {
        const template = this.plan.mode === 'opt_in' ? this.settings.optInNoticeText : this.settings.notifyNoticeText;
        return renderNotice(template, {
            lmaUser: this.deps.lmaUser,
            meeting: this.deps.meetingName,
            consentPhrase: this.settings.consentPhrases[0],
            objectionPhrase: this.settings.objectionPhrases[0],
        });
    }

    /** Posts the notice (with a line for the owner-attests rule) and, when `record`, records it. */
    private async postNotice(notice: string, record: boolean): Promise<void> {
        const messages = noticeChatMessages(notice);
        if (this.plan.mode === 'opt_in' && this.plan.rule === 'owner_attests') {
            messages.push(`Recording starts when ${this.deps.lmaUser} confirms in LMA that everyone has agreed.`);
        }
        // The notice texts are shared with every capture method, which read
        // replies in different places; this VP reads the chat and listens.
        messages.push(REPLY_CHANNELS_LINE);
        if (this.held && this.noticePosted && this.eachParticipant) {
            messages.push('Recording and transcription are paused until everyone present has consented.');
        }
        await this.say(messages);
        this.noticePosted = true;
        this.postedNoticeText = messages.join(' ');
        this.lastNoticeAt = this.clock();
        log.info(`[consent] notice posted in the chat (${messages.length} message${messages.length === 1 ? '' : 's'})`);
        if (record) await this.callApi((api) => api.recordNotice(messages.join(' ')));
    }

    private async say(messages: string[]): Promise<void> {
        const wanted = messages.filter((message) => message && message.trim());
        if (!this.io || wanted.length === 0) return;
        for (const message of wanted) this.posted.add(normalizeReplyText(message));
        try {
            await this.io.sendMessages(wanted);
        } catch (error) {
            log.warn(`[consent] could not post in the chat: ${String(error)}`);
        }
    }

    /** Whether `text` is something this flow posted (for handlers that see their own messages). */
    isOwnMessage(text: string): boolean {
        const normalized = normalizeReplyText(text || '');
        return this.posted.has(normalized) || normalized === normalizeReplyText(CONSENT_HELP_LINE);
    }

    /** Runs one API call after the ones before it, and applies the record it answers with. */
    private callApi(call: (api: ConsentApi) => Promise<ConsentRecord | null>, onRecord?: (record: ConsentRecord) => void): Promise<ConsentRecord | null> {
        const run = async (): Promise<ConsentRecord | null> => {
            if (!this.api || !this.started) return null;
            try {
                const record = await call(this.api);
                if (record) {
                    onRecord?.(record);
                    await this.applyRecord(record);
                }
                return record;
            } catch (error) {
                log.warn(`[consent] a consent API call failed: ${String(error)}`);
                return null;
            }
        };
        const next = this.apiChain.then(run, run);
        this.apiChain = next;
        return next;
    }

    /** Acts on what the record now says. */
    private async applyRecord(record: ConsentRecord | null): Promise<void> {
        if (!record) return;
        const previous = this.record;
        if (previous?.Version != null && record.Version != null && record.Version < previous.Version) return;
        this.record = record;
        if (isConsentMode(record.Mode) && record.Mode !== this.plan.mode) {
            log.always(`[consent] the meeting's consent runs in mode ${record.Mode}${record.Rule ? ` (${record.Rule})` : ''}`);
        }
        if (isConsentMode(record.Mode)) {
            this.plan = { ...this.plan, mode: record.Mode, rule: isConsentRule(record.Rule) ? record.Rule : null };
        }
        if (record.Mode === 'off') return;

        const fresh = (record.Events ?? []).filter(
            (event) => event.Decision === 'objection' && event.EventId && !this.handledObjections.has(event.EventId),
        );
        for (const event of fresh) {
            this.handledObjections.add(event.EventId as string);
            // The participant's latest reply is now this objection, so a
            // consent they reply in the chat afterwards is new, not a repeat.
            this.localDecisions.set(normalizeParticipantName(event.ParticipantName), 'objection');
        }

        await this.setGate(record.CaptureAllowed === true, record);

        for (const event of fresh) {
            // Recorded somewhere else: by a signed-in user, in the meeting
            // page's Consent section or through the API.
            const name = event.ParticipantName || 'A participant';
            log.always(`[consent] objection recorded for ${JSON.stringify(name)} (source ${event.Source})`);
            await this.say([`An objection for ${name} was recorded in LMA. Live Meeting Assistant has stopped recording and transcribing.`]);
            this.queueEvidence(`after an objection for ${name} was recorded in LMA`);
        }
        if (record.Status === 'granted' && previous?.Status !== 'granted' && this.noticePosted) {
            this.queueEvidence(this.plan.mode === 'notify' ? 'after the notice was posted' : 'when consent was granted');
        }
    }

    /** Withholds or allows capture as the record says. */
    private async setGate(recordAllows: boolean, record: ConsentRecord): Promise<void> {
        const allowed = recordAllows && !this.objectionAwaitingRecord;
        if (!allowed) {
            if (!this.held) {
                this.held = true;
                if (this.deps.capture.isOn()) this.deps.capture.hold();
                log.always(`[consent] capture withheld: ${latestReason(record)}`);
            }
            this.syncListening();
            await this.stopVideo();
            await this.writeDetail(
                this.objectionAwaitingRecord ? 'Not recording: a participant objected to recording.' : this.detailFor(record),
            );
            return;
        }
        if (this.held) {
            this.held = false;
            // Closed before capture starts, so nothing heard while consent was
            // awaited reaches the transcript.
            this.syncListening();
            log.always(`[consent] capture allowed: ${latestReason(record)}`);
            const lines = [this.grantLine(record)];
            if (this.userPaused) {
                lines.push(`Transcription stays paused, as asked; type "LMA ${details.startCommand.toLowerCase()}" to resume it.`);
                await this.say(lines);
            } else {
                await this.say(lines);
                this.deps.capture.release();
                await this.say(this.deps.startMessages());
            }
            await this.writeDetail(null);
        }
        await this.startDeferredVideo();
    }

    private grantLine(record: ConsentRecord): string {
        if (this.plan.mode === 'notify') {
            // Said only after an objection; a meeting that was only waiting for
            // its mode to be known just starts.
            return this.handledObjections.size > 0 ? 'Nobody present objects to recording now.' : '';
        }
        if (this.plan.rule === 'owner_attests') return `${this.deps.lmaUser} confirmed in LMA that everyone has agreed.`;
        return record.Status === 'granted' ? 'Everyone present has consented.' : 'Consent was given.';
    }

    private detailFor(record: ConsentRecord): string {
        if (record.Status === 'objected') return 'Not recording: a participant objected to recording.';
        if (this.plan.mode === 'opt_in' && this.plan.rule === 'owner_attests') {
            return 'Waiting for consent: the meeting owner has not yet confirmed in LMA that everyone agreed.';
        }
        // Only once the handler has had time to report a count: until its
        // first reading every platform looks like one that gives none.
        if (this.roster.seats === null && !this.roster.rosterIsComplete && this.clock() - this.attachedAt >= COUNT_GRACE_MS) {
            return 'Waiting for consent: this meeting platform does not show LMA how many people are present, so each participant\'s consent cannot be confirmed.';
        }
        const waiting = waitingFor(record);
        // Where the chat shows no sender, consent can only be given aloud.
        const aloud = this.io?.sendersHidden ? ' On this meeting platform consent is given aloud, not in the chat.' : '';
        if (waiting.length === 0) return `Waiting for consent: no participants are listed yet.${aloud}`;
        return `Waiting for consent: ${waiting.length} of ${(record.Participants ?? []).length} participants present have not consented yet.${aloud}`;
    }

    private async writeDetail(detail: string | null): Promise<void> {
        if (detail === this.lastDetail) return;
        this.lastDetail = detail;
        try {
            await this.deps.setStatusDetail(detail);
        } catch (error) {
            log.warn(`[consent] could not write the status detail: ${String(error)}`);
        }
    }

    /**
     * Starting and stopping the screen recording run on a chain of their own,
     * in the background: stopping finalises the whole recording so far, which
     * in a long meeting takes far longer than a chat round trip, and starting
     * waits for the audio device. Nothing else waits for either; the
     * decision is taken (the state below) at once.
     */
    private onVideoChain(what: string, run: () => Promise<void>): void {
        const step = async (): Promise<void> => {
            try {
                await run();
            } catch (error) {
                log.warn(`[consent] could not ${what} the screen recording: ${String(error)}`);
            }
        };
        this.videoChain = this.videoChain.then(step, step);
    }

    private async startDeferredVideo(): Promise<void> {
        if (this.video !== 'deferred' || !this.deps.capture.isOn()) return;
        this.video = 'live';
        this.onVideoChain('start', () => this.deps.capture.startVideo());
    }

    private async stopVideo(): Promise<void> {
        if (this.video !== 'live') return;
        this.video = 'stopped';
        log.always('[consent] screen recording stopped for the rest of the meeting');
        this.onVideoChain('stop', () => this.deps.capture.stopVideo());
    }

    /** Settles once every start and stop of the screen recording asked for so far has finished. */
    videoSettled(): Promise<void> {
        return this.videoChain;
    }

    /**
     * One chat message, from a platform handler. True when it was a consent
     * reply (or a refused "LMA start"), which the handler should then not treat
     * as anything else. Every handler calls it before all of its own chat
     * commands, the dismissal command included; no dismissal command is a
     * reply (classifyChatReply), so the order only matters for a message such
     * as "LMA, stop recording", which is an objection and not a dismissal.
     */
    async handleChat(sender: string | null, text: string | null): Promise<boolean> {
        if (!this.active || this.offChoicePending || !text || !text.trim()) return false;
        if (this.isOwnMessage(text) || this.roster.isOwnIdentity(sender)) return false;
        // A sender shown only as a placeholder ("Unknown") is nobody this flow
        // can name: their reply is read as one with no sender.
        if (sender && this.roster.isOwn(sender)) sender = null;
        if (sender && this.roster.noteSeen(sender)) this.scheduleRosterSync();

        if (matchesChatCommand(text, details.startCommand)) {
            if (this.held) {
                log.info('[consent] "LMA start" refused: consent is not given');
                await this.say([this.startRefusal()]);
                return true;
            }
            this.userPaused = false;
            return false;
        }
        if (matchesChatCommand(text, details.pauseCommand)) {
            this.userPaused = true;
            return false;
        }
        const decision = classifyChatReply(text, this.settings.consentPhrases, this.settings.objectionPhrases);
        if (!decision) return false;
        await this.onDecision(sender, decision, text, 'chat');
        return true;
    }

    /**
     * One final utterance heard in the meeting, from the transcription service
     * while the meeting is captured or from the consent listener while consent
     * is awaited, attributed to the participant the VP showed speaking (null
     * when it cannot name one). Read as a chat message is, the whole utterance
     * being the reply (classifySpokenReply). Answers the decision read in it,
     * or null for ordinary speech, which is dropped here: not kept, not logged.
     *
     * An objection stops capture before this returns, so the caller can rely
     * on nothing after it being captured.
     */
    handleSpeech(speaker: string | null, text: string | null): ConsentDecision | null {
        if (!this.active || this.offChoicePending || !text || !text.trim()) return null;
        // The VP's own voice (its voice assistant) is never a reply.
        if (this.roster.isOwnIdentity(speaker)) return null;
        if (speaker && this.roster.isOwn(speaker)) speaker = null;
        // A named speaker is somebody present, as one shown speaking is.
        if (speaker && this.roster.noteSeen(speaker)) this.scheduleRosterSync();
        const decision = classifySpokenReply(text, this.settings.consentPhrases, this.settings.objectionPhrases);
        if (!decision) return null;
        this.onDecision(speaker, decision, text, 'voice').catch((error) =>
            log.error('[consent] a spoken reply could not be handled:', error),
        );
        return decision;
    }

    /**
     * A reply read in the chat or heard. Nothing in it awaits before an
     * objection has stopped capture.
     */
    private async onDecision(sender: string | null, decision: ConsentDecision, text: string, source: ReplySource): Promise<void> {
        // Some chat observers report one message twice; the second is dropped.
        const duplicateKey = `${source}|${normalizeParticipantName(sender)}|${normalizeReplyText(text)}`;
        const seenAt = this.recentReplies.get(duplicateKey);
        for (const [key, at] of this.recentReplies) {
            if (this.clock() - at >= DUPLICATE_REPLY_MS) this.recentReplies.delete(key);
        }
        this.recentReplies.set(duplicateKey, this.clock());
        if (seenAt !== undefined && this.clock() - seenAt < DUPLICATE_REPLY_MS) return;
        const named = sender && sender.trim() ? sender.trim() : null;
        if (decision === 'consent' && !named) {
            // Consent counts for a person, and this platform did not say who
            // sent it, or the VP could not tell who said it. An objection
            // without a name still stops capture. Said only where a
            // participant's consent is awaited: elsewhere a bare "I agree" is
            // as likely to be conversation.
            log.info(`[consent] a consent reply with no ${source === 'voice' ? 'speaker' : 'sender'} was not recorded`);
            if (this.eachParticipant) await this.say([this.unattributedConsentLine(source)]);
            return;
        }
        if (named && isPlaceholderName(named)) {
            // A display name that reads as one of the list's placeholders is
            // not a participant this flow can tell apart from the seat it
            // stands for; it is neither counted nor recorded as consent.
            if (decision === 'consent') return;
        }
        await this.onReply(named ?? UNKNOWN_SENDER, decision, text, source);
    }

    private unattributedConsentLine(source: ReplySource): string {
        if (source === 'voice') {
            return 'Live Meeting Assistant heard consent said aloud but could not tell who said it, so it was not recorded.';
        }
        const phrase = this.settings.consentPhrases[0] || 'I consent';
        return (
            'Live Meeting Assistant could not tell who sent that reply, so it was not recorded as consent.' +
            (this.io?.sendersHidden ? ` Say "${phrase}" aloud instead.` : '')
        );
    }

    /**
     * Whether an objection is one already acted on that arrived by the other
     * route: said aloud and typed in the chat by the same participant, or with
     * either unnamed, while capture is still stopped by the first.
     */
    private isEchoedObjection(key: string, source: ReplySource): boolean {
        const now = this.clock();
        this.recentObjections = this.recentObjections.filter((o) => now - o.at < CROSS_SOURCE_OBJECTION_MS);
        if (!this.held) return false;
        const unknown = normalizeParticipantName(UNKNOWN_SENDER);
        return this.recentObjections.some((o) => o.source !== source && (o.key === key || o.key === unknown || key === unknown));
    }

    /** Asks for spoken replies to be listened for exactly while capture is withheld in the meeting. */
    private syncListening(): void {
        const want = this.held && !!this.io && !this.stopped && !this.leaving && this.plan.mode !== 'off';
        if (want === this.listening) return;
        this.listening = want;
        try {
            this.deps.capture.listen?.(want);
        } catch (error) {
            log.warn(`[consent] could not ${want ? 'start' : 'stop'} listening for spoken replies: ${String(error)}`);
        }
    }

    private startRefusal(): string {
        if (this.record?.Status === 'objected') {
            return 'Live Meeting Assistant cannot start: a participant objected to recording.';
        }
        return 'Live Meeting Assistant cannot start until consent is given.';
    }

    private async onReply(name: string, decision: ConsentDecision, text: string, source: ReplySource): Promise<void> {
        const key = normalizeParticipantName(name);
        const occurredAt = new Date(this.clock()).toISOString();
        const event: RecordEventInput = {
            ParticipantName: name.slice(0, 100),
            Decision: decision,
            Text: text.slice(0, 500),
            OccurredAt: occurredAt,
            Source: source,
        };
        if (decision === 'objection') {
            if (this.isEchoedObjection(key, source)) {
                log.info(`[consent] an objection ${source === 'voice' ? 'heard' : 'in the chat'} repeats one already acted on; not acted on again`);
                return;
            }
            this.recentObjections.push({ key, source, at: this.clock() });
            await this.onObjection(name, key, event);
            return;
        }
        // Once per participant, whichever route it came by.
        if (this.localDecisions.get(key) === 'consent') return;
        this.localDecisions.set(key, 'consent');
        log.always(`[consent] consent from ${JSON.stringify(name)} (${source})`);
        const record = await this.recordEvent(event);
        if (record && !this.held) return;
        if (record && this.eachParticipant) {
            const waiting = waitingFor(record).filter((who) => normalizeParticipantName(who) !== key);
            if (waiting.length > 0) await this.say([`Thank you, ${name}. Still waiting for ${describeWaiting(waiting)}.`]);
        }
    }

    private async onObjection(name: string, key: string, event: RecordEventInput): Promise<void> {
        this.objectionsInFlight.add(event);
        this.localDecisions.set(key, 'objection');
        log.always(`[consent] objection from ${JSON.stringify(name)} (${event.Source ?? 'chat'}): capture stops`);
        // At once: nothing waits on the API.
        if (!this.held) {
            this.held = true;
            if (this.deps.capture.isOn()) this.deps.capture.hold();
        }
        const leave = this.settings.leaveOnObjection;
        // Decided now, so a VP about to leave never starts listening for
        // replies it would not stay for.
        const leaveNow = leave && !this.leaving && !!this.io;
        if (leaveNow) this.leaving = true;
        this.syncListening();
        await this.stopVideo();
        await this.writeDetail('Not recording: a participant objected to recording.');
        const consentPhrase = this.settings.consentPhrases[0] || 'I consent';
        // A sender the platform did not show cannot withdraw by replying (a
        // consent needs a sender), so no such way is offered to them.
        const resume = name === UNKNOWN_SENDER ? '' : ` If ${name} replies "${consentPhrase}", it can continue.`;
        await this.say([
            `${name} objected. Live Meeting Assistant has stopped recording and transcribing this meeting.` +
                (leave ? ' It will now leave the meeting.' : resume),
        ]);
        await this.recordEvent(event, true);
        const evidence = this.queueEvidence(`when ${name} objected`);
        if (leaveNow && this.io) {
            await Promise.race([evidence, this.sleep(30_000)]);
            await this.say(this.deps.exitMessages());
            this.io.leave(name);
        }
    }

    /**
     * Records a reply, or keeps it to record later: before consent has
     * started, and after a write that failed. A kept objection holds capture
     * (see setGate) until it is on the record, so a record that does not show
     * it yet never releases capture.
     */
    private async recordEvent(event: RecordEventInput, objectionHandledHere = false): Promise<ConsentRecord | null> {
        if (event.Decision === 'objection') this.objectionsInFlight.add(event);
        if (!this.started) {
            this.pendingEvents.push(event);
            this.objectionsInFlight.delete(event);
            return null;
        }
        const key = normalizeParticipantName(event.ParticipantName);
        const record = await this.callApi(
            (api) => api.recordEvent(event),
            (answer) => {
                // On the record now, before the answer is applied.
                this.objectionsInFlight.delete(event);
                if (!objectionHandledHere) return;
                // Only this participant's objections: one entered on the
                // meeting page at the same moment is still acknowledged.
                for (const e of answer.Events ?? []) {
                    if (e.Decision === 'objection' && e.EventId && normalizeParticipantName(e.ParticipantName) === key) {
                        this.handledObjections.add(e.EventId);
                    }
                }
            },
        );
        if (!record) {
            const attempts = (this.eventAttempts.get(event) ?? 0) + 1;
            this.eventAttempts.set(event, attempts);
            // An objection is never given up on; a consent is, after a while,
            // and the participant can reply again.
            if (event.Decision === 'objection' || attempts < MAX_CONSENT_RECORD_ATTEMPTS) {
                this.pendingEvents.push(event);
            } else {
                log.error(`[consent] gave up recording consent from ${JSON.stringify(event.ParticipantName)}`);
                this.localDecisions.delete(normalizeParticipantName(event.ParticipantName));
            }
        }
        this.objectionsInFlight.delete(event);
        return record;
    }

    private async flushPendingEvents(): Promise<void> {
        const events = this.pendingEvents.splice(0, this.pendingEvents.length);
        // Still awaiting the record while the ones before them are written.
        for (const event of events) if (event.Decision === 'objection') this.objectionsInFlight.add(event);
        for (const event of events) await this.recordEvent(event, event.Decision === 'objection');
    }

    /**
     * Whether an objection read here is not on the record yet: kept to record
     * later, or read and not yet answered. From the moment it is read, so a
     * record answer that was already on its way, and does not hold it yet,
     * cannot release capture.
     */
    private get objectionAwaitingRecord(): boolean {
        return this.objectionsInFlight.size > 0 || this.pendingEvents.some((event) => event.Decision === 'objection');
    }

    // ─── Roster ──────────────────────────────────────────────────────────────

    /** The attendee count, VP included, from the handler's existing attendee tracking. */
    noteAttendeeCount(count: number): void {
        if (!this.active) return;
        const change = this.roster.noteCount(count, this.record?.Status === 'granted');
        if (!change) return;
        this.scheduleRosterSync();
        if (change === 'joined') this.scheduleNoticeRepost();
    }

    /** A name the platform shows as speaking (via the transcription service). */
    noteSpeaker(name: string): void {
        if (!this.active) return;
        if (this.roster.noteSeen(name)) this.scheduleRosterSync();
    }

    private scheduleRosterSync(): void {
        if (this.rosterSyncTimer || this.stopped) return;
        this.rosterSyncTimer = setTimeout(() => {
            this.rosterSyncTimer = null;
            void this.syncRoster();
        }, 2000);
    }

    /** Lists who is present on the record when that has changed. */
    async syncRoster(): Promise<void> {
        if (!this.started || !this.active) return;
        if (this.io?.readRoster) {
            try {
                const change = this.roster.noteRoster(await this.io.readRoster());
                if (change === 'joined') this.scheduleNoticeRepost();
            } catch {
                this.roster.noteRoster(null);
            }
        }
        const names = this.roster.present(this.eachParticipant).slice(0, 250);
        const listed = JSON.stringify(names);
        if (listed === this.lastParticipants) return;
        if (names.length === 0 && this.lastParticipants === null) return;
        this.lastParticipants = listed;
        log.info(`[consent] participants present: ${names.length} (${names.filter((n) => n.startsWith(UNIDENTIFIED_PARTICIPANT)).length} unidentified)`);
        await this.callApi((api) => api.setParticipants(names));
    }

    /** Somebody joined: post the notice again, at most once a minute. */
    private scheduleNoticeRepost(): void {
        if (!this.started || this.noticeRepostTimer || this.stopped) return;
        const window = this.deps.noticeRepostMs ?? 60_000;
        const wait = Math.max(0, this.lastNoticeAt + window - this.clock());
        this.noticeRepostTimer = setTimeout(() => {
            this.noticeRepostTimer = null;
            if (this.stopped || !this.record?.NoticeText) return;
            void this.postNotice(this.record.NoticeText, true);
        }, Math.max(wait, 3000));
    }

    /** One read of the record, for what changed elsewhere (an attestation, an objection on the page). */
    async pollOnce(): Promise<void> {
        // One poll at a time: when the API is slow, ticks that would queue
        // behind the one still running are skipped rather than piled up in
        // front of the chat's own writes.
        if (!this.active || this.polling) return;
        this.polling = true;
        try {
            if (!this.started) {
                await this.ensureStarted();
                return;
            }
            await this.flushPendingEvents();
            await this.syncRoster();
            await this.callApi(async (api) => (await api.get()).record);
        } finally {
            this.polling = false;
        }
    }

    // ─── Evidence ────────────────────────────────────────────────────────────

    private queueEvidence(note: string): Promise<unknown> {
        const run = (): Promise<void> => this.captureEvidence(note);
        const next = this.evidenceChain.then(run, run);
        this.evidenceChain = next;
        return next;
    }

    /** Waits for every screenshot queued so far; for tests and for leaving. */
    async evidenceSettled(): Promise<void> {
        await this.evidenceChain;
        await this.apiChain;
    }

    private async captureEvidence(note: string): Promise<void> {
        if (!this.io?.screenshots || !this.started) return;
        if (this.evidenceUsed >= MAX_EVIDENCE_IMAGES) {
            log.info(`[consent] no screenshot ${note}: this meeting already has ${MAX_EVIDENCE_IMAGES}`);
            return;
        }
        let shots: EvidenceShot[] = [];
        try {
            shots = await this.io.screenshots();
        } catch (error) {
            log.warn(`[consent] could not take screenshots ${note}: ${String(error)}`);
            return;
        }
        for (const shot of shots) {
            if (this.evidenceUsed >= MAX_EVIDENCE_IMAGES) break;
            const key = consentEvidenceKey(this.deps.callId(), Date.parse(shot.capturedAt) || this.clock(), shot.view);
            try {
                await this.deps.upload(key, shot.image);
            } catch (error) {
                log.warn(`[consent] could not store the ${shot.view} screenshot: ${String(error)}`);
                continue;
            }
            this.evidenceUsed += 1;
            const what = shot.view === 'participants' ? 'Participant list' : shot.view === 'chat' ? 'Meeting chat' : 'Meeting chat and participants';
            const record = await this.callApi((api) => api.addEvidence({ S3Key: key, CapturedAt: shot.capturedAt, Note: `${what} ${note}` }));
            const evidenceId = record?.Evidence?.find((entry) => entry.S3Key === key)?.EvidenceId;
            log.info(`[consent] evidence stored: ${key}${evidenceId ? ` (${evidenceId})` : ''}`);
            // The check reads a chat for the notice and the replies; a
            // participant list alone would always come back inconclusive.
            if (evidenceId && shot.view !== 'participants' && this.api) {
                void this.api.verifyEvidence(evidenceId);
            }
        }
    }
}

/** The record's latest status reason, for logs. */
function latestReason(record: ConsentRecord): string {
    const history = record.StatusHistory ?? [];
    return history.length ? history[history.length - 1].Reason || record.Status || '' : record.Status || '';
}

/** Each participant's latest reply on the record, attestations left out (lma_consent.latest_decisions). */
export function latestDecisions(record: ConsentRecord): Map<string, string> {
    const latest = new Map<string, string>();
    for (const event of record.Events ?? []) {
        if (event.Source === 'attestation') continue;
        const name = normalizeParticipantName(event.ParticipantName);
        if (name && (event.Decision === 'consent' || event.Decision === 'objection')) latest.set(name, event.Decision);
    }
    return latest;
}

/** Whether a display name reads as one of the participant list's placeholders, or the unknown sender. */
export function isPlaceholderName(name: string): boolean {
    return normalizeParticipantName(name).startsWith(normalizeParticipantName(UNIDENTIFIED_PARTICIPANT));
}

/** Who on the record's participant list has not consented. */
export function waitingFor(record: ConsentRecord): string[] {
    const latest = latestDecisions(record);
    return (record.Participants ?? []).filter((name) => latest.get(normalizeParticipantName(name)) !== 'consent');
}

/** "Ana Silva, Bo Chen and 2 unidentified participants". */
export function describeWaiting(names: string[]): string {
    const named = names.filter((name) => !name.startsWith(UNIDENTIFIED_PARTICIPANT));
    const unnamed = names.length - named.length;
    const parts = [...named];
    if (unnamed > 0) parts.push(`${unnamed} unidentified participant${unnamed === 1 ? '' : 's'}`);
    if (parts.length <= 1) return parts[0] || 'the participants present';
    return `${parts.slice(0, -1).join(', ')} and ${parts[parts.length - 1]}`;
}
