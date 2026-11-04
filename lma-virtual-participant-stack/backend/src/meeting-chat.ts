/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The meeting chat, added to the transcript.
 *
 * Every platform handler reads the meeting's chat for the LMA chat commands.
 * After a message has been through the commands, the handler offers it here,
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
 * - a private message, where the platform shows a message is private;
 * - the chat that was already in the panel when the VP first read it;
 * - everything while the setting is off (`virtual_participant.captureMeetingChat`)
 *   or the VP redacts its transcript, while the transcript is paused or
 *   consent is withheld (`details.start`), and before the meeting's START event
 *   has been sent (there is no meeting record to add it to yet);
 * - more than `MAX_PER_MINUTE` messages in a minute, or `MAX_PER_MEETING` in
 *   the meeting.
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
}

/** What happened to an offered message. */
export type ChatOutcome =
    | 'captured'
    | 'empty'
    | 'duplicate'
    | 'own'
    | 'command'
    | 'direct'
    | 'history'
    | 'disabled'
    | 'paused'
    | 'not-started'
    | 'flood';

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
export function isLmaChatCommand(text: string): boolean {
    if (!text || !text.trim()) return false;
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
    /** Whether the VP redacts its transcript; chat is not added then. */
    redacting: () => boolean;
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

    private decide(chat: IncomingChat): ChatOutcome {
        const text = (chat.text ?? '').trim();
        if (!text) return 'empty';
        if (this.alreadySeen(chat, text)) return 'duplicate';
        if (this.isOwn(chat.sender, text)) return 'own';
        if (isLmaChatCommand(text)) return 'command';
        if (chat.audience === 'direct') return 'direct';
        if (chat.history) return 'history';
        if (!this.deps.settingOn() || this.deps.redacting()) {
            if (!this.offNoticeLogged) {
                this.offNoticeLogged = true;
                log.info(
                    `[meeting-chat] the meeting chat is not added to the transcript ` +
                        `(setting ${this.deps.settingOn() ? 'on' : 'off'}, redaction ${this.deps.redacting() ? 'on' : 'off'})`,
                );
            }
            return 'disabled';
        }
        if (!this.deps.capturing()) return 'paused';
        if (!this.deps.meetingStarted()) return 'not-started';
        if (this.flooded()) return 'flood';

        const speaker = chatSpeaker(chat.sender);
        const occurrenceKey = `${speaker}|${text}`;
        const occurrence = (this.occurrences.get(occurrenceKey) ?? 0) + 1;
        this.occurrences.set(occurrenceKey, occurrence);
        const at = Math.max(0, this.deps.timelineSeconds());
        const segment: TranscriptSegment = {
            channel: MEETING_CHAT_CHANNEL,
            segmentId: chatSegmentId(chat.platform, chat.id, speaker, text, occurrence),
            startTime: at,
            endTime: at,
            transcript: chatTranscript(text, chat.links),
            isPartial: false,
            speaker,
        };
        this.added += 1;
        this.addedAt.push(this.now());
        if (!this.firstCaptureLogged) {
            this.firstCaptureLogged = true;
            log.info(`[meeting-chat] adding the meeting chat to the transcript (platform=${chat.platform})`);
        }
        void this.deps.send(segment).catch((error) => log.warn('[meeting-chat] could not write a chat segment:', error));
        return 'captured';
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
