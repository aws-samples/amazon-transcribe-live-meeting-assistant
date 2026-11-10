/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The meeting chat, added to the transcript.
 *
 * Every platform handler reads the meeting's chat for the LMA chat commands.
 * After a message has been through the commands, the Zoom, Teams and Google
 * Meet handlers offer it here,
 * and a message participants sent to everyone becomes one transcript segment
 * on the MEETING_CHAT channel: the sender as the speaker, the text as the
 * transcript, and as its time the point on the transcript timeline at which
 * the VP read it. No platform gives a message time that can be placed on that
 * timeline, which is the audio's clock, so the time is the reading.
 *
 * What is NOT added, in the order it is decided:
 *
 * - a message already offered (by its platform id, or for a platform without
 *   one, the same sender and text within `ID_LESS_DUPLICATE_MS`);
 * - the VP's own messages: a sender that is the VP, or a text it posted in the
 *   last `OWN_MESSAGE_TTL_MS` (every handler's `sendMessages` records what it
 *   posts with `noteSentChat`);
 * - any LMA chat command, whatever state the meeting is in (`isLmaChatCommand`):
 *   "LMA start" typed while the meeting is running is still a command. An
 *   `LMA ask` or `@LMA` question IS added: it is something a participant asked
 *   in front of everyone;
 * - a private message, where the platform shows a message is private, and a
 *   message whose recipient the handler could not read: only a message the
 *   handler shows was sent to everyone is added (Teams and Google Meet, whose
 *   meeting chats have no private messages, say so for every message);
 * - the chat that was already in the panel when the VP first read it;
 * - everything while the setting is off (`virtual_participant.captureMeetingChat`)
 *   or content redaction is turned on (in any language), and when the saved
 *   redaction choice is not known because the configuration was not applied
 *   at start (`contentRedactionRequested`), while the transcript is paused or
 *   consent is withheld (`details.start`), and before the meeting's START event
 *   has been sent (there is no meeting record to add it to yet);
 * - more than `MAX_PER_MINUTE` messages in a minute, or `MAX_PER_MEETING` in
 *   the meeting.
 *
 * The VP's own answer to a question asked in the chat (chat-ask.ts) is added
 * as well, under the VP's display name, but only through `offerAnswer`, and
 * only when the question itself was added: an answer to a private question, or
 * to one asked while the capture was off, stays out like the question did. The
 * answer read back from the chat panel is the VP's own message and is left
 * out as such, so it is added once.
 *
 * Webex and Amazon Chime are not covered. Webex's chat panel does not show
 * which messages are private, so its handler only calls `observe`, which
 * writes the diagnostic line and adds nothing; the Amazon Chime handler offers
 * nothing.
 *
 * A message that is not added is not kept for later either: one read while the
 * transcript was paused stays out after it resumes, because it is marked seen
 * like any other.
 *
 * The VP logs no message text and no sender name here. Each message offered
 * gets one debug line with what the platform gave for it (a sender, an id, a
 * time, a recipient, how many links), so a live run can tell what each
 * platform's chat actually exposes.
 *
 * Pure apart from the logger: the transcript writer, the clock and the meeting
 * state are passed in, and meeting-chat-runtime.ts wires them to this VP.
 */
import { createHash } from 'node:crypto';
import { details, matchesChatCommand, matchesEndCommand } from './details.js';
import { parseAssistantChatCommand } from './assistant-toggles.js';
import { ChatPlatform, isOwnChatSender, parseChatQuestion } from './chat-ask.js';
import { classifyChatReply, normalizeReplyText } from './consent-matcher.js';
import { MEETING_CHAT_CHANNEL } from './channels.js';
import { withoutVpAddress } from './vp-handle.js';
import type { TranscriptSegment } from './kinesis-stream.js';
import { createLogger } from './logger.js';

const log = createLogger('meeting-chat');

/** The environment variable stack-settings.ts sets from `virtual_participant.captureMeetingChat`. */
export const CAPTURE_MEETING_CHAT_VARIABLE = 'CAPTURE_MEETING_CHAT';
/** The setting's shipped default, used when the configuration could not be read. */
export const CAPTURE_MEETING_CHAT_DEFAULT = true;

/** The speaker of a message whose sender the platform did not show. */
export const UNKNOWN_CHAT_SENDER = 'Unknown sender';
/** Longest message text kept; the rest is cut with an ellipsis. */
export const MAX_CHAT_TEXT_LENGTH = 4000;
/** Longest sender name kept. */
export const MAX_SENDER_LENGTH = 200;
/** At most this many link addresses are added to one message. */
export const MAX_LINKS_PER_MESSAGE = 10;
/** Longest link address added. */
export const MAX_LINK_LENGTH = 2048;
/** Messages added in any one minute; more are left out until the minute has passed. */
export const MAX_PER_MINUTE = 60;
/** Messages added in one meeting. */
export const MAX_PER_MEETING = 3000;
/** How long a message the VP posted is recognised when it is read back. */
export const OWN_MESSAGE_TTL_MS = 10 * 60_000;
/** How long a captured question waits for its answer to be added; see `offerAnswer`. */
export const ANSWER_WAIT_MS = 5 * 60_000;
/** Platform message ids remembered, oldest forgotten first. */
export const MAX_SEEN_IDS = 5000;
/**
 * For a platform that gives no message id: the same sender and text read again
 * within this long is the same message delivered twice (a re-rendered row).
 */
export const ID_LESS_DUPLICATE_MS = 5000;

/** Who a message was sent to, where the platform shows it. */
export type ChatAudience = 'everyone' | 'direct';

/** One message, as a platform handler read it. */
export interface IncomingChat {
    platform: ChatPlatform;
    /** The message body, with any sender or recipient prefix removed. */
    text: string;
    sender?: string | null;
    /** The platform's own id for the message, when it gives one. */
    id?: string | null;
    /**
     * The time the platform shows for the message, when it shows one. Only
     * whether it is there is used (in the diagnostics): it is not on the
     * transcript's clock.
     */
    platformTime?: string | number | null;
    /** Undefined when the platform does not say who the message was sent to. */
    audience?: ChatAudience;
    /** The addresses of the links in the message. */
    links?: readonly string[];
    /** Read from what the chat panel already held when the VP first read it. */
    history?: boolean;
    /**
     * The text to read for an LMA command, when it differs from `text`: the
     * message with its address reduced to "LMA" (chat-addressing.ts), so a
     * command for another LMA VP stays out like one for this VP.
     */
    commandText?: string;
}

/** What happened to an offered message. */
export type ChatOutcome =
    | 'captured'
    | 'empty'
    | 'duplicate'
    | 'own'
    | 'command'
    | 'direct'
    /** The handler did not say who the message was sent to. */
    | 'recipient-unknown'
    | 'history'
    | 'disabled'
    /** The configuration was not applied at start, so the saved redaction choice is not known. */
    | 'redaction-unknown'
    | 'paused'
    | 'not-started'
    | 'flood'
    /** A platform the capture does not cover; read for the diagnostics only. */
    | 'not-covered';

/**
 * Whether a chat message is one of the LMA chat commands, in any state of the
 * meeting: the dismissal, pause, resume and help commands, the assistant-mode
 * commands, and the consent replies addressed to LMA ("LMA consent", "LMA no
 * consent", "LMA object"). A question for the meeting assistant ("LMA ask
 * ...", "@LMA ...") is not a command here, so it is added to the transcript;
 * "LMA ask" with no question is.
 *
 * The handlers' own chains decide what a command does, and some only act in
 * one state ("LMA start" only resumes a paused meeting). This decides what
 * stays out of the transcript, and that does not depend on the state.
 */
export function isLmaChatCommand(message: string): boolean {
    if (!message || !message.trim()) return false;
    // A command for another LMA VP ("LMA (Alice) leave") is a command too.
    const text = withoutVpAddress(message);
    if (matchesEndCommand(text)) return true;
    if (parseAssistantChatCommand(text) !== null) return true;
    for (const command of [details.helpCommand, details.pauseCommand, details.startCommand]) {
        if (matchesChatCommand(text, command)) return true;
    }
    // Only the built-in phrases addressed to LMA: the deployment's own consent
    // phrases are replies only while a consent flow runs, and the flow consumes
    // those before the message is offered here.
    if (classifyChatReply(text, [], []) !== null) return true;
    const question = parseChatQuestion(text, true);
    return question !== null && !question.question;
}

/** Text as the own-message ledger compares it. */
function ledgerText(text: string): string {
    return normalizeReplyText(text || '');
}

/** Below this length an own message is matched only whole, never by containment. */
const CONTAINMENT_MIN_LENGTH = 24;

/**
 * The messages this VP posted recently, so that reading one back is recognised
 * on every platform, including those that show no sender (the Teams browser
 * join) or label the VP's messages in a way that cannot be matched to its name.
 */
export class OwnChatLedger {
    private readonly entries: Array<{ text: string; at: number }> = [];

    constructor(
        private readonly now: () => number = Date.now,
        private readonly ttlMs: number = OWN_MESSAGE_TTL_MS,
        private readonly limit: number = 500,
    ) {}

    record(messages: readonly string[]): void {
        const at = this.now();
        for (const message of messages) {
            const text = ledgerText(message);
            if (text) this.entries.push({ text, at });
        }
        this.prune(at);
    }

    /**
     * Whether `text` is one of the posted messages: the same words, or, for
     * longer messages, one containing the other, since some chat panels add to
     * a message (an "edited" mark) or show only the start of a long one.
     */
    matches(text: string): boolean {
        const candidate = ledgerText(text);
        if (!candidate) return false;
        this.prune(this.now());
        return this.entries.some(
            (entry) =>
                entry.text === candidate ||
                (candidate.length >= CONTAINMENT_MIN_LENGTH &&
                    entry.text.length >= CONTAINMENT_MIN_LENGTH &&
                    (entry.text.includes(candidate) || candidate.includes(entry.text))),
        );
    }

    private prune(at: number): void {
        while (this.entries.length && (at - this.entries[0].at >= this.ttlMs || this.entries.length > this.limit)) {
            this.entries.shift();
        }
    }
}

/** The ledger every handler records into; see `noteSentChat`. */
export const ownChatLedger = new OwnChatLedger();

/** Records messages this VP is posting in the meeting chat. Every handler's `sendMessages` calls it. */
export function noteSentChat(messages: readonly string[]): void {
    ownChatLedger.record(messages);
}

/** Whether the setting asks for the chat to be added, read from the environment stack-settings.ts sets. */
export function captureMeetingChatSetting(env: Record<string, string | undefined> = process.env): boolean {
    const raw = (env[CAPTURE_MEETING_CHAT_VARIABLE] ?? '').trim().toLowerCase();
    if (raw === 'true') return true;
    if (raw === 'false') return false;
    return CAPTURE_MEETING_CHAT_DEFAULT;
}

/**
 * Whether content redaction is turned on, as saved, whatever the language, or
 * undefined when that is not known.
 *
 * The Virtual Participant redacts only en-US speech (ENABLE_CONTENT_REDACTION
 * is the flag combined with the language), but chat text is not redacted in any
 * language, so the chat stays out of the transcript whenever the flag is on.
 * stack-settings.ts sets CONTENT_REDACTION_REQUESTED from the saved flag. It is
 * unset when the configuration was not applied at start (the table could not
 * be read, or held no value for the flag); the saved choice is then not known,
 * and the capture adds nothing. The task's combined variable can still say
 * that redaction is on, but its "false" may only mean the language is not
 * en-US, so it never says that redaction is off.
 */
export function contentRedactionRequested(env: Record<string, string | undefined> = process.env): boolean | undefined {
    const requested = (env['CONTENT_REDACTION_REQUESTED'] ?? '').trim().toLowerCase();
    if (requested === 'true') return true;
    if (requested === 'false') return false;
    if ((env['ENABLE_CONTENT_REDACTION'] ?? '').trim().toLowerCase() === 'true') return true;
    return undefined;
}

/** The link addresses worth adding: http and https only, not already in the text, each once. */
export function linksToAdd(text: string, links: readonly string[] | undefined): string[] {
    const out: string[] = [];
    for (const raw of links ?? []) {
        if (out.length >= MAX_LINKS_PER_MESSAGE) break;
        if (typeof raw !== 'string') continue;
        const href = raw.trim();
        if (!href || href.length > MAX_LINK_LENGTH || !/^https?:\/\//i.test(href)) continue;
        if (text.includes(href) || out.includes(href)) continue;
        out.push(href);
    }
    return out;
}

/** The transcript text of a message: the text, cut to the limit, and the links it did not show. */
export function chatTranscript(text: string, links?: readonly string[]): string {
    const single = text.trim();
    const body = single.length <= MAX_CHAT_TEXT_LENGTH ? single : `${single.slice(0, MAX_CHAT_TEXT_LENGTH - 1)}…`;
    const added = linksToAdd(body, links);
    return added.length ? `${body} ${added.map((href) => `(link: ${href})`).join(' ')}` : body;
}

/** The speaker of a message: its sender, or `UNKNOWN_CHAT_SENDER`, never empty. */
export function chatSpeaker(sender: string | null | undefined): string {
    const name = (sender ?? '').replace(/\s+/g, ' ').trim();
    if (!name) return UNKNOWN_CHAT_SENDER;
    return name.length <= MAX_SENDER_LENGTH ? name : name.slice(0, MAX_SENDER_LENGTH);
}

/** How a question is matched to its answer: by the platform's id, else by sender and text. */
function questionKey(chat: Pick<IncomingChat, 'platform' | 'text' | 'sender' | 'id'>): string {
    if (chat.id) return `${chat.platform}:${chat.id}`;
    return `${chat.platform}|${(chat.sender ?? '').trim()}|${(chat.text ?? '').trim()}`;
}

const SAFE_ID = /^[A-Za-z0-9._:-]{1,96}$/;

function shortHash(text: string): string {
    return createHash('sha256').update(text).digest('hex').slice(0, 24);
}

/**
 * The segment id of a message. From the platform's id where there is one, so
 * the same message is the same row however it arrives; otherwise from the
 * sender, the text and how many times that sender has sent that text, so two
 * identical messages are two rows. Always starts with `chat-`, which no speech
 * segment id does.
 */
export function chatSegmentId(platform: ChatPlatform, id: string | null | undefined, sender: string, text: string, occurrence: number): string {
    if (id) {
        return SAFE_ID.test(id) ? `chat-${platform}-${id}` : `chat-${platform}-${shortHash(id)}`;
    }
    return `chat-${platform}-${shortHash(`${sender}|${text}|${occurrence}`)}`;
}

export interface MeetingChatDeps {
    /** Whether the setting is on; see `captureMeetingChatSetting`. */
    settingOn: () => boolean;
    /**
     * Whether content redaction is turned on, in any language; chat is not
     * added then. Undefined when that is not known (see
     * `contentRedactionRequested`); chat is not added then either.
     */
    redacting: () => boolean | undefined;
    /** Whether the meeting is being captured now (`details.start`). */
    capturing: () => boolean;
    /** Whether the meeting's START event has been sent. */
    meetingStarted: () => boolean;
    /** The current position on the transcript timeline, in seconds. */
    timelineSeconds: () => number;
    /** Writes the segment. Errors are logged. */
    send: (segment: TranscriptSegment) => Promise<void>;
    /** Names the VP appears under in the chat. */
    ownIdentities: () => readonly string[];
    /** The VP's display name, the speaker of its own answers (`offerAnswer`). */
    ownName: () => string;
    ledger?: OwnChatLedger;
    now?: () => number;
}

/**
 * The chat capture for one meeting. `offer` decides at once and returns the
 * outcome; the segment is written in the background.
 */
export class MeetingChatCapture {
    private readonly seenIds = new Set<string>();
    private readonly recentIdLess = new Map<string, number>();
    private readonly occurrences = new Map<string, number>();
    private readonly addedAt: number[] = [];
    /** Captured questions awaiting their answer, by `questionKey`, with when they were captured. */
    private readonly openQuestions = new Map<string, number>();
    private added = 0;
    private floodNoticeAt = -Infinity;
    private offNoticeLogged = false;
    private firstCaptureLogged = false;
    private readonly ledger: OwnChatLedger;
    private readonly now: () => number;

    constructor(private readonly deps: MeetingChatDeps) {
        this.ledger = deps.ledger ?? ownChatLedger;
        this.now = deps.now ?? Date.now;
    }

    /** How many messages have been added to the transcript in this meeting. */
    get capturedCount(): number {
        return this.added;
    }

    offer(chat: IncomingChat): ChatOutcome {
        const outcome = this.decide(chat);
        this.diagnose(chat, outcome);
        return outcome;
    }

    /**
     * For a platform the capture does not cover (Webex): writes only the
     * diagnostic line, so a live run shows what that platform's chat gives
     * (in particular whether its private messages can be told apart). Never
     * adds anything.
     */
    observe(chat: IncomingChat): void {
        this.diagnose(chat, 'not-covered');
    }

    /**
     * The VP's answer to `question`, posted as `messages`: each message is
     * added to the transcript under the VP's display name when the question
     * itself was added (pass the question as it was given to `offer`), and the
     * capture is still on. Each question's answer is added once. Returns how
     * many messages were added.
     */
    offerAnswer(question: Pick<IncomingChat, 'platform' | 'text' | 'sender' | 'id'>, messages: readonly string[]): number {
        const key = questionKey(question);
        const at = this.now();
        for (const [open, capturedAt] of this.openQuestions) {
            if (at - capturedAt >= ANSWER_WAIT_MS) this.openQuestions.delete(open);
        }
        if (!this.openQuestions.delete(key)) return 0;
        // The same gates as a message, read again now: the meeting may have
        // been paused while the agent was answering.
        if (!this.deps.settingOn() || this.deps.redacting() !== false) return 0;
        if (!this.deps.capturing() || !this.deps.meetingStarted()) return 0;
        const speaker = chatSpeaker(this.deps.ownName());
        let added = 0;
        for (const message of messages) {
            const text = (message ?? '').trim();
            if (!text || this.flooded()) continue;
            this.write(question.platform, null, speaker, text);
            added += 1;
        }
        if (added) log.info(`[meeting-chat] added the chat answer to the transcript (platform=${question.platform} messages=${added})`);
        return added;
    }

    private decide(chat: IncomingChat): ChatOutcome {
        const text = (chat.text ?? '').trim();
        if (!text) return 'empty';
        if (this.alreadySeen(chat, text)) return 'duplicate';
        if (this.isOwn(chat.sender, text)) return 'own';
        if (isLmaChatCommand(text) || (chat.commandText && isLmaChatCommand(chat.commandText))) return 'command';
        if (chat.audience === 'direct') return 'direct';
        // Only a message the handler shows was sent to everyone: a Zoom label
        // in another language, or an SDK payload naming no receiver, may be a
        // private message.
        if (chat.audience !== 'everyone') return 'recipient-unknown';
        if (chat.history) return 'history';
        const settingOn = this.deps.settingOn();
        const redacting = this.deps.redacting();
        if (!settingOn || redacting !== false) {
            if (!this.offNoticeLogged) {
                this.offNoticeLogged = true;
                const redaction = redacting === undefined ? 'not known, the configuration was not applied' : redacting ? 'on' : 'off';
                log.info(
                    `[meeting-chat] the meeting chat is not added to the transcript ` +
                        `(setting ${settingOn ? 'on' : 'off'}, redaction ${redaction})`,
                );
            }
            return settingOn && redacting === undefined ? 'redaction-unknown' : 'disabled';
        }
        if (!this.deps.capturing()) return 'paused';
        if (!this.deps.meetingStarted()) return 'not-started';
        if (this.flooded()) return 'flood';

        this.write(chat.platform, chat.id, chatSpeaker(chat.sender), text, chat.links);
        // A question for the meeting assistant: its answer may now be added too.
        if (parseChatQuestion(text, true)?.question) this.openQuestions.set(questionKey(chat), this.now());
        return 'captured';
    }

    /** Writes one message as a MEETING_CHAT segment, at the current point on the timeline. */
    private write(
        platform: ChatPlatform,
        id: string | null | undefined,
        speaker: string,
        text: string,
        links?: readonly string[],
    ): void {
        const occurrenceKey = `${speaker}|${text}`;
        const occurrence = (this.occurrences.get(occurrenceKey) ?? 0) + 1;
        this.occurrences.set(occurrenceKey, occurrence);
        const at = Math.max(0, this.deps.timelineSeconds());
        const segment: TranscriptSegment = {
            channel: MEETING_CHAT_CHANNEL,
            segmentId: chatSegmentId(platform, id, speaker, text, occurrence),
            startTime: at,
            endTime: at,
            transcript: chatTranscript(text, links),
            isPartial: false,
            speaker,
        };
        this.added += 1;
        this.addedAt.push(this.now());
        if (!this.firstCaptureLogged) {
            this.firstCaptureLogged = true;
            log.info(`[meeting-chat] adding the meeting chat to the transcript (platform=${platform})`);
        }
        void this.deps.send(segment).catch((error) => log.warn('[meeting-chat] could not write a chat segment:', error));
    }

    /** Marks the message seen, and says whether it was already. */
    private alreadySeen(chat: IncomingChat, text: string): boolean {
        if (chat.id) {
            const key = `${chat.platform}:${chat.id}`;
            if (this.seenIds.has(key)) return true;
            this.seenIds.add(key);
            if (this.seenIds.size > MAX_SEEN_IDS) {
                const oldest = this.seenIds.values().next().value;
                if (oldest !== undefined) this.seenIds.delete(oldest);
            }
            return false;
        }
        const at = this.now();
        for (const [key, seenAt] of this.recentIdLess) {
            if (at - seenAt >= ID_LESS_DUPLICATE_MS) this.recentIdLess.delete(key);
        }
        const key = `${chat.platform}|${(chat.sender ?? '').trim()}|${text}`;
        const seenAt = this.recentIdLess.get(key);
        this.recentIdLess.set(key, at);
        return seenAt !== undefined;
    }

    private isOwn(sender: string | null | undefined, text: string): boolean {
        if (isOwnChatSender(sender)) return true;
        if (sender) {
            const name = normalizeReplyText(sender);
            if (this.deps.ownIdentities().some((own) => own && normalizeReplyText(own) === name)) return true;
        }
        return this.ledger.matches(text);
    }

    private flooded(): boolean {
        const at = this.now();
        while (this.addedAt.length && at - this.addedAt[0] >= 60_000) this.addedAt.shift();
        const flooded = this.added >= MAX_PER_MEETING || this.addedAt.length >= MAX_PER_MINUTE;
        if (flooded && at - this.floodNoticeAt >= 60_000) {
            this.floodNoticeAt = at;
            log.info(
                `[meeting-chat] chat limit reached: ${this.added} messages added in this meeting, ` +
                    `${this.addedAt.length} in the last minute; leaving more out`,
            );
        }
        return flooded;
    }

    /**
     * One debug line per message: what the platform gave for it, never what it
     * said or who sent it.
     */
    private diagnose(chat: IncomingChat, outcome: ChatOutcome): void {
        log.debug(
            `[meeting-chat] platform=${chat.platform} outcome=${outcome} ` +
                `sender=${chat.sender ? 'yes' : 'no'} id=${chat.id ? 'yes' : 'no'} ` +
                `time=${chat.platformTime !== undefined && chat.platformTime !== null && chat.platformTime !== '' ? 'yes' : 'no'} ` +
                `recipient=${chat.audience ?? 'unknown'} links=${chat.links?.length ?? 0} ` +
                `chars=${(chat.text ?? '').length}${chat.history ? ' history=yes' : ''}`,
        );
    }
}
