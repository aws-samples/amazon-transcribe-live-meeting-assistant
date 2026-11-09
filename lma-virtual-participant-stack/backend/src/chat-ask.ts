/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * `LMA ask <question>` (and `@LMA <question>`, and `LMA <question>?`): a
 * question typed in the meeting chat, answered in the meeting chat by the
 * Meeting Assistant.
 *
 * The answer comes from the same Strands meeting-assist Lambda the voice
 * assistant's lookup tool calls (`STRANDS_LAMBDA_ARN`), with the meeting's
 * transcript as context. It does not depend on a voice assistant: the Lambda
 * ARN and the permission to invoke it are set whenever the deployment's
 * meeting assistant is a Strands one, whatever the voice-assistant provider.
 *
 * Questions from the chat are asked with `toolScope: 'meeting_chat'`, and the
 * meeting assistant decides from the Meeting Assistant section's
 * `chatQuestionTools` setting what to answer with: by default only the
 * current meeting (its transcript, agenda and shared-screen pictures), with
 * no web search, document knowledge base, past meetings, MCP servers or
 * browser and VNC controls. The LMA user is sent as `userEmail` for the
 * setting's all-tools choices; the assistant does not use it otherwise.
 *
 * Every question is sent with `askedByMeetingOwner: false`
 * (`chatSenderIsMeetingOwner`). A chat sender is known only by the display
 * name the meeting shows, which any participant can choose, and no platform's
 * chat ties a message to the LMA user's sign-in, so ownership is not
 * established from the chat. The setting's "owner only" choice therefore
 * answers every chat question from the current meeting only.
 *
 * The answer is posted as plain text (markdown reduced by `toSpokenText`, which
 * also makes it a single line — the one shape every platform's send path
 * delivers intact, see chat-help.ts — and, with `keepUrls`, leaves link
 * addresses whole for the reader to follow) and split into messages no longer
 * than the platform's limit. Each sender may ask once per `PER_SENDER_INTERVAL_MS`,
 * and one question is answered at a time.
 *
 * The answer is produced in the background: the platform handlers call
 * `startChatAnswer` and return, so a question that takes the agent twenty
 * seconds does not hold up `LMA leave` behind it on the platforms whose chat
 * arrives on a poll loop.
 */
import { InvokeCommand, LambdaClient } from '@aws-sdk/client-lambda';
import { details, matchesChatCommand, matchesEndCommand } from './details.js';
import { parseAssistantChatCommand } from './assistant-toggles.js';
import { normalizeChatText } from './chat-text.js';
import { classifyChatReply } from './consent-matcher.js';
import { toSpokenText } from './spoken-text.js';
import { createLogger } from './logger.js';

const log = createLogger('chat-ask');

/** The meeting platforms, as the chat handlers name themselves. */
export type ChatPlatform = 'zoom' | 'zoom-sdk' | 'teams' | 'teams-sdk' | 'webex' | 'chime' | 'google-meet';

/**
 * The longest message LMA posts on each platform, in characters.
 *
 * Conservative values chosen by LMA rather than each platform's published
 * maximum. Chime and Webex post by typing into the composer one key at a time,
 * so their limit is lower to keep a long answer from taking many seconds to
 * type; Zoom inserts the text in one step and the two SDK platforms send it
 * through an API.
 */
export const CHAT_MESSAGE_LIMITS: Readonly<Record<ChatPlatform, number>> = {
    zoom: 1000,
    'zoom-sdk': 1000,
    teams: 1000,
    'teams-sdk': 1000,
    webex: 500,
    chime: 500,
    // Meet's own limit on one chat message is believed to be 500 characters
    // (not confirmed in a live meeting), so LMA stays at it.
    'google-meet': 500,
};

/** At most this many messages per answer; the rest is cut with an ellipsis. */
export const MAX_ANSWER_MESSAGES = 3;

/** How often one sender may ask. */
export const PER_SENDER_INTERVAL_MS = 20_000;

/**
 * How long to wait for an answer before saying none came. The agent call is
 * left to finish on its own; this only frees the one-at-a-time slot.
 */
export const ANSWER_TIMEOUT_MS = 90_000;

/** Questions longer than this are not sent to the agent. */
export const MAX_QUESTION_LENGTH = 500;

/** Every message this module posts starts with this, and no question can. */
export const ANSWER_PREFIX = 'Live Meeting Assistant';

export interface ChatQuestion {
    question: string;
    /** `LMA ask ...` rather than `@LMA ...` or `LMA <question>?`. */
    explicit: boolean;
}

const EXPLICIT_ASK = /^\s*@?lma\b[\s,:;.!-]*ask\b[\s,:;.!?-]*(.*)$/is;
const MENTION = /^\s*@lma\b[\s,:;.!-]*(.*)$/is;
const ADDRESSED = /^\s*lma\b[\s,:;.!-]*(.*)$/is;

/**
 * The words a bare `LMA <text>` may start with to be read as a question
 * without a question mark: the question words, and the requests participants
 * type to an assistant ("LMA summarize the last ten minutes").
 */
export const QUESTION_STARTS: ReadonlySet<string> = new Set([
    'what', 'whats', 'who', 'whos', 'whom', 'whose', 'when', 'where', 'why', 'how', 'which',
    'summarize', 'summarise', 'recap', 'explain', 'list', 'tell',
]);

/**
 * Whether the text after a bare "LMA" reads as a question: it ends with a
 * question mark, or starts with one of `QUESTION_STARTS`. "LMA is great" and
 * "LMA asked me" are someone talking about LMA, not to it.
 */
export function readsAsQuestion(text: string): boolean {
    const trimmed = text.trim();
    if (!/[a-z0-9]/i.test(trimmed)) return false;
    if (/\?[\s"')\]]*$/.test(trimmed)) return true;
    const first = normalizeChatText(trimmed.replace(/['’]/g, '')).split(' ')[0];
    return QUESTION_STARTS.has(first);
}

/** Whether `message` is one of the messages the VP itself posts, as configured. */
function isOwnStatusMessage(message: string): boolean {
    const text = message.trim();
    return [...details.introMessages, ...details.startMessages, ...details.pauseMessages, ...details.exitMessages]
        .some((own) => own.trim() === text);
}

/**
 * Whether `message` is one of the other chat commands, which win over a
 * question: the dismissal, assistant-mode, help, pause and resume commands, and
 * the built-in consent replies ("LMA consent", "LMA object"), which are
 * commands even while no consent flow is running to act on them.
 */
function isOtherChatCommand(message: string): boolean {
    return (
        matchesEndCommand(message)
        || parseAssistantChatCommand(message) !== null
        || matchesChatCommand(message, details.helpCommand)
        || matchesChatCommand(message, details.pauseCommand)
        || matchesChatCommand(message, details.startCommand)
        // Only the built-in phrases, by design: the deployment's own consent
        // phrases are replies only while a consent flow runs, and the flow
        // (meeting-consent.ts) reads every message before this parser does,
        // so a configured phrase never gets here while it is a reply.
        // isLmaChatCommand (meeting-chat.ts) draws the same line.
        || classifyChatReply(message, [], []) !== null
    );
}

/**
 * The question in `message`, if it is one.
 *
 * `LMA ask <question>` (also `@LMA ask`, `LMA, ask:`) always is. `@LMA
 * <question>` is one only when the text is not another chat command, so
 * `@LMA help` is still help and `@LMA translate` still changes mode. A bare
 * "LMA <words>" (LMA first) is one when it is not another command and reads as
 * a question (`readsAsQuestion`): "LMA what was just discussed?" is, "LMA is
 * great" is not, since without the `@`, the word "ask" or the shape of a
 * question it is as likely to be someone talking about LMA as to it.
 *
 * Neither form can match a message this module posts, because those all start
 * with `ANSWER_PREFIX`. With no Strands meeting assistant only the explicit
 * form is recognised (and answered with a note saying so).
 */
export function parseChatQuestion(message: string, available: boolean = chatAskAvailable()): ChatQuestion | null {
    if (!message) return null;
    // The VP's own configurable messages, which a platform with no sender
    // (the Teams web client) reads back, are never questions.
    if (isOwnStatusMessage(message)) return null;
    const explicit = EXPLICIT_ASK.exec(message);
    if (explicit) {
        return { question: explicit[1].trim(), explicit: true };
    }
    // Without a meeting assistant an `@LMA` aside is only meeting chat.
    if (!available) return null;
    const mention = MENTION.exec(message);
    if (mention && mention[1].trim() && !isOtherChatCommand(message)) {
        return { question: mention[1].trim(), explicit: false };
    }
    const addressed = ADDRESSED.exec(message);
    if (addressed && readsAsQuestion(addressed[1]) && !isOtherChatCommand(message)) {
        return { question: addressed[1].trim(), explicit: false };
    }
    return null;
}

/** Whether chat questions can be answered in this deployment. */
export function chatAskAvailable(env: Record<string, string | undefined> = process.env): boolean {
    return Boolean(env.STRANDS_LAMBDA_ARN);
}

/** Whether a chat sender is this Virtual Participant itself. */
export function isOwnChatSender(sender: string | null | undefined): boolean {
    if (!sender) return false;
    const name = normalizeChatText(sender);
    return [details.scribeIdentity, details.scribeName, 'you']
        .filter(Boolean)
        .some((own) => normalizeChatText(own) === name);
}

/**
 * Split a one-line answer into chat messages no longer than `limit`, each
 * carrying the prefix, breaking at a sentence end where one is close enough and
 * at a space otherwise. More than `maxMessages` are cut, the last ending in an
 * ellipsis.
 */
export function splitForChat(text: string, limit: number, maxMessages: number = MAX_ANSWER_MESSAGES): string[] {
    const single = text.replace(/\s+/g, ' ').trim();
    const onlyPrefix = `${ANSWER_PREFIX}: `;
    if (onlyPrefix.length + single.length <= limit || maxMessages <= 1) {
        const room = limit - onlyPrefix.length;
        return [`${onlyPrefix}${single.length <= room ? single : `${cutAtWord(single, room - 1)}…`}`];
    }

    // Numbered prefixes; the longest is the one with the largest numbers.
    const prefixFor = (i: number, total: number) => `${ANSWER_PREFIX} (${i + 1}/${total}): `;
    const room = limit - prefixFor(maxMessages - 1, maxMessages).length;

    const chunks: string[] = [];
    let rest = single;
    while (rest.length > 0 && chunks.length < maxMessages) {
        if (rest.length <= room) {
            chunks.push(rest);
            rest = '';
            break;
        }
        const window = rest.slice(0, room);
        // A sentence end in the second half of the window, else the last space.
        const sentenceEnd = Math.max(window.lastIndexOf('. '), window.lastIndexOf('? '), window.lastIndexOf('! '));
        let cut = sentenceEnd >= room / 2 ? sentenceEnd + 1 : window.lastIndexOf(' ');
        if (cut <= 0) cut = room;
        chunks.push(rest.slice(0, cut).trim());
        rest = rest.slice(cut).trim();
    }
    if (rest.length > 0) {
        const last = chunks.length - 1;
        chunks[last] = `${cutAtWord(chunks[last], room - 1)}…`;
    }
    return chunks.map((chunk, i) => `${prefixFor(i, chunks.length)}${chunk}`);
}

/** `text` cut to at most `max` characters, at a space where there is one. */
function cutAtWord(text: string, max: number): string {
    if (text.length <= max) return text;
    const window = text.slice(0, max);
    const space = window.lastIndexOf(' ');
    return (space > max / 2 ? window.slice(0, space) : window).trimEnd();
}

/** Who asked a chat question, as the platform handler read it. */
export interface ChatAsker {
    sender: string | null | undefined;
    platform: ChatPlatform;
}

/** Calls the meeting-assist agent with a question and returns its answer text. */
export type AskMeetingAssistant = (question: string, asker: ChatAsker) => Promise<string>;

/**
 * Whether a chat question is the meeting owner's (the LMA user who started
 * this Virtual Participant). Always false: the chat shows a sender only by the
 * display name the meeting shows, which any participant can set to the
 * owner's name, and no platform's chat ties a message to the LMA user's
 * sign-in. The meeting assistant offers the owner-only tools only when this is
 * true.
 */
export function chatSenderIsMeetingOwner(_asker: ChatAsker): boolean {
    return false;
}

/** The meeting-assist Lambda's request for a question typed in the meeting chat. */
export function chatQuestionPayload(
    question: string,
    asker: ChatAsker,
    env: Record<string, string | undefined> = process.env,
) {
    const callId = env.VP_CALL_ID || env.MEETING_NAME || 'vp-chat';
    return {
        text: question,
        call_id: callId,
        conversation_history: [],
        // Used only when the chatQuestionTools setting offers all tools.
        userEmail: env.LMA_USER || '',
        toolScope: 'meeting_chat',
        askedByMeetingOwner: chatSenderIsMeetingOwner(asker),
        dynamodb_table_name: env.DYNAMODB_TABLE_NAME || '',
        dynamodb_pk: `c#${callId}`,
    };
}

let lambdaClient: LambdaClient | null = null;

/**
 * Ask the Strands meeting-assist Lambda, as the voice assistant's lookup does,
 * in the meeting chat's tool scope.
 */
export const askStrandsMeetingAssistant: AskMeetingAssistant = async (question, asker) => {
    const arn = process.env.STRANDS_LAMBDA_ARN;
    if (!arn) throw new Error('STRANDS_LAMBDA_ARN is not set');
    lambdaClient ??= new LambdaClient({ region: process.env.AWS_REGION || process.env.VP_AWS_REGION || 'us-east-1' });
    const payload = chatQuestionPayload(question, asker);
    const response = await lambdaClient.send(
        new InvokeCommand({ FunctionName: arn, InvocationType: 'RequestResponse', Payload: JSON.stringify(payload) }),
    );
    const body = JSON.parse(new TextDecoder().decode(response.Payload));
    if (typeof body?.message === 'string' && body.message.trim()) {
        return body.message;
    }
    // Only the shape, so no echoed text reaches the Virtual Participant's log.
    const shape = body && typeof body === 'object' ? Object.keys(body).join(',') : typeof body;
    throw new Error(`meeting assistant returned no answer (statusCode=${body?.statusCode ?? '-'} keys=${shape})`);
};

/** What the answerer replies to a question; see `respond` in `createChatAnswerer`. */
export interface ChatReply {
    messages: string[];
    /**
     * The messages are the meeting assistant's answer. False for the VP's own
     * notices (no assistant, paused, rate limited, busy, too long, failed),
     * which are posted but are not an answer to add to the transcript.
     */
    answered: boolean;
}

/** Nothing to post. */
const noReply = (): ChatReply => ({ messages: [], answered: false });

/**
 * One answerer per meeting: the per-sender rate limit and the one-at-a-time
 * rule live here, with the agent call injectable for tests.
 */
export function createChatAnswerer(
    ask: AskMeetingAssistant = askStrandsMeetingAssistant,
    options: {
        intervalMs?: number;
        now?: () => number;
        available?: () => boolean;
        timeoutMs?: number;
        paused?: () => boolean;
    } = {},
) {
    // While `LMA pause` is in effect nothing from the meeting is sent on: a
    // question is neither sent nor queued until `LMA start`.
    const paused = options.paused ?? (() => !details.start);
    const intervalMs = options.intervalMs ?? PER_SENDER_INTERVAL_MS;
    const timeoutMs = options.timeoutMs ?? ANSWER_TIMEOUT_MS;
    const now = options.now ?? Date.now;
    const available = options.available ?? (() => chatAskAvailable());
    const lastAskedAt = new Map<string, number>();
    const toldToWait = new Set<string>();
    const toldBusy = new Set<string>();
    let busy = false;

    /**
     * The reply to a question: the messages to post (or none), and whether
     * they are the meeting assistant's answer rather than a notice (a refusal,
     * a rate limit, a failure). Resolves once the agent has answered (or
     * failed); never rejects.
     */
    async function respond(
        parsed: ChatQuestion,
        sender: string | null | undefined,
        platform: ChatPlatform,
    ): Promise<ChatReply> {
        if (isOwnChatSender(sender)) return noReply();
        const limit = CHAT_MESSAGE_LIMITS[platform];
        // A notice from the VP, never the agent's answer.
        const reply = (text: string): ChatReply => ({ messages: splitForChat(text, limit, 1), answered: false });

        if (!available()) {
            // Only an explicit `LMA ask` is told; an `@LMA` aside is left alone.
            return parsed.explicit
                ? reply('questions in the chat need the Strands meeting assistant, which this deployment does not use.')
                : noReply();
        }
        if (paused()) {
            return parsed.explicit
                ? reply('the transcript is paused; ask again after someone types "LMA start".')
                : noReply();
        }
        if (!parsed.question) {
            return reply('type your question after "LMA ask", for example "LMA ask what did we decide about the budget?"');
        }
        if (parsed.question.length > MAX_QUESTION_LENGTH) {
            return reply(`please keep questions under ${MAX_QUESTION_LENGTH} characters.`);
        }

        const key = normalizeChatText(sender || '');
        const at = now();
        const last = lastAskedAt.get(key);
        if (last !== undefined && at - last < intervalMs) {
            // Told once per window, so repeated asking does not become a
            // stream of refusals.
            if (toldToWait.has(key)) return noReply();
            toldToWait.add(key);
            return reply(`one question every ${Math.round(intervalMs / 1000)} seconds, please.`);
        }
        if (busy) {
            // Once per sender per answer in progress, for the same reason.
            if (toldBusy.has(key)) return noReply();
            toldBusy.add(key);
            return reply('still answering the previous question; ask again in a moment.');
        }

        // Forget senders whose interval has passed, so the maps stay bounded.
        for (const [sender, askedAt] of lastAskedAt) {
            if (at - askedAt >= intervalMs) {
                lastAskedAt.delete(sender);
                toldToWait.delete(sender);
            }
        }
        lastAskedAt.set(key, at);
        toldToWait.delete(key);
        toldBusy.clear();
        busy = true;
        const started = now();
        try {
            let timer: NodeJS.Timeout | undefined;
            const raw = await Promise.race([
                ask(parsed.question, { sender, platform }),
                new Promise<never>((_, reject) => {
                    timer = setTimeout(() => reject(new Error(`no answer within ${timeoutMs} ms`)), timeoutMs);
                }),
            ]).finally(() => clearTimeout(timer));
            // Read rather than spoken, so links keep their addresses.
            const plain = toSpokenText(raw, { keepUrls: true });
            const messages = splitForChat(plain || 'I have no answer to that.', limit);
            // An answer with no text left once reduced to plain text is posted
            // as a notice, not recorded as the agent's answer.
            log.info(
                `[chat-ask] answered platform=${platform} chars=${plain.length} messages=${messages.length}`
                    + ` ms=${now() - started}`,
            );
            return { messages, answered: plain.length > 0 };
        } catch (error) {
            log.warn('[chat-ask] the meeting assistant did not answer:', error);
            return reply('I could not get an answer from the meeting assistant just now.');
        } finally {
            busy = false;
        }
    }

    /** The messages to post for a question, or none; see `respond`. */
    async function answer(
        parsed: ChatQuestion,
        sender: string | null | undefined,
        platform: ChatPlatform,
    ): Promise<string[]> {
        return (await respond(parsed, sender, platform)).messages;
    }

    return { answer, respond };
}

export type ChatAnswerer = ReturnType<typeof createChatAnswerer>;

const meetingAnswerer: ChatAnswerer = createChatAnswerer();

/**
 * Answer a chat question in the background and post the reply with `send`,
 * which is told whether the reply is the meeting assistant's answer (see
 * `ChatReply.answered`): only an answer is added to the transcript. Returns at
 * once; errors are logged, never thrown into the chat handler.
 */
export function startChatAnswer(
    parsed: ChatQuestion,
    sender: string | null | undefined,
    platform: ChatPlatform,
    send: (messages: string[], answered: boolean) => Promise<void>,
    answerer: ChatAnswerer = meetingAnswerer,
): Promise<void> {
    log.info(`[chat-ask] question platform=${platform} explicit=${parsed.explicit} chars=${parsed.question.length}`);
    return answerer
        .respond(parsed, sender, platform)
        .then((reply) => (reply.messages.length ? send(reply.messages, reply.answered) : undefined))
        .catch((error) => log.warn('[chat-ask] could not post the answer:', error));
}
