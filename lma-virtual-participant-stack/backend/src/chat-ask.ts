/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * `LMA ask <question>` (and `@LMA <question>`): a question typed in the meeting
 * chat, answered in the meeting chat by the Meeting Assistant.
 *
 * The answer comes from the same Strands meeting-assist Lambda the voice
 * assistant's lookup tool calls (`STRANDS_LAMBDA_ARN`), with the meeting's
 * transcript as context. It does not depend on a voice assistant: the Lambda
 * ARN and the permission to invoke it are set whenever the deployment's
 * meeting assistant is a Strands one, whatever the voice-assistant provider.
 *
 * Questions from the chat are asked with `toolScope: 'meeting_chat'` and no
 * user email, so the agent answers from the current meeting's transcript, the
 * document knowledge base and web search. It is not given the owner's meeting
 * history, MCP servers, or the browser and VNC controls: a chat sender is known
 * only by the display name the meeting shows, so a question in the chat is
 * answered as any participant's question rather than as the LMA user's.
 *
 * The answer is posted as plain text (markdown reduced by `toSpokenText`, which
 * also makes it a single line — the one shape every platform's send path
 * delivers intact, see chat-help.ts) and split into messages no longer than
 * the platform's limit. Each sender may ask once per `PER_SENDER_INTERVAL_MS`,
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
import { toSpokenText } from './spoken-text.js';

/** The meeting platforms, as the chat handlers name themselves. */
export type ChatPlatform = 'zoom' | 'zoom-sdk' | 'teams' | 'teams-sdk' | 'webex' | 'chime';

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
    /** `LMA ask ...` rather than `@LMA ...`. */
    explicit: boolean;
}

const EXPLICIT_ASK = /^\s*@?lma\b[\s,:;.!-]*ask\b[\s,:;.!?-]*(.*)$/is;
const MENTION = /^\s*@lma\b[\s,:;.!-]*(.*)$/is;

/** Whether `message` is one of the other chat commands, which win over a question. */
function isOtherChatCommand(message: string): boolean {
    return (
        matchesEndCommand(message)
        || parseAssistantChatCommand(message) !== null
        || matchesChatCommand(message, details.helpCommand)
        || matchesChatCommand(message, details.pauseCommand)
        || matchesChatCommand(message, details.startCommand)
    );
}

/**
 * The question in `message`, if it is one.
 *
 * `LMA ask <question>` (also `@LMA ask`, `LMA, ask:`) always is. `@LMA
 * <question>` is one only when the text is not another chat command, so
 * `@LMA help` is still help and `@LMA translate` still changes mode. A bare
 * "LMA <words>" is not a question: without the `@` or the word "ask" it is as
 * likely to be someone talking about LMA as to it.
 *
 * Neither form can match a message this module posts, because those all start
 * with `ANSWER_PREFIX`. With no Strands meeting assistant only the explicit
 * form is recognised (and answered with a note saying so).
 */
export function parseChatQuestion(message: string, available: boolean = chatAskAvailable()): ChatQuestion | null {
    if (!message) return null;
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

/** Calls the meeting-assist agent with a question and returns its answer text. */
export type AskMeetingAssistant = (question: string) => Promise<string>;

let lambdaClient: LambdaClient | null = null;

/**
 * Ask the Strands meeting-assist Lambda, as the voice assistant's lookup does,
 * with the chat's narrower tool scope.
 */
export const askStrandsMeetingAssistant: AskMeetingAssistant = async (question) => {
    const arn = process.env.STRANDS_LAMBDA_ARN;
    if (!arn) throw new Error('STRANDS_LAMBDA_ARN is not set');
    lambdaClient ??= new LambdaClient({ region: process.env.AWS_REGION || process.env.VP_AWS_REGION || 'us-east-1' });
    const callId = process.env.VP_CALL_ID || process.env.MEETING_NAME || 'vp-chat';
    const payload = {
        text: question,
        call_id: callId,
        conversation_history: [],
        userEmail: '',
        toolScope: 'meeting_chat',
        dynamodb_table_name: process.env.DYNAMODB_TABLE_NAME || '',
        dynamodb_pk: `c#${callId}`,
    };
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
    // While `LMA pause` is in effect nothing from the meeting is sent on, so a
    // question waits for `LMA start` like the transcript does.
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
     * The messages to post for a question, or none. Resolves once the agent
     * has answered (or failed); never rejects.
     */
    async function answer(
        parsed: ChatQuestion,
        sender: string | null | undefined,
        platform: ChatPlatform,
    ): Promise<string[]> {
        if (isOwnChatSender(sender)) return [];
        const limit = CHAT_MESSAGE_LIMITS[platform];
        const reply = (text: string) => splitForChat(text, limit, 1);

        if (!available()) {
            // Only an explicit `LMA ask` is told; an `@LMA` aside is left alone.
            return parsed.explicit
                ? reply('questions in the chat need the Strands meeting assistant, which this deployment does not use.')
                : [];
        }
        if (paused()) {
            return parsed.explicit
                ? reply('the transcript is paused, so questions wait until someone types "LMA start".')
                : [];
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
            if (toldToWait.has(key)) return [];
            toldToWait.add(key);
            return reply(`one question every ${Math.round(intervalMs / 1000)} seconds, please.`);
        }
        if (busy) {
            // Once per sender per answer in progress, for the same reason.
            if (toldBusy.has(key)) return [];
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
                ask(parsed.question),
                new Promise<never>((_, reject) => {
                    timer = setTimeout(() => reject(new Error(`no answer within ${timeoutMs} ms`)), timeoutMs);
                }),
            ]).finally(() => clearTimeout(timer));
            const plain = toSpokenText(raw);
            const messages = splitForChat(plain || 'I have no answer to that.', limit);
            console.log(
                `[chat-ask] answered platform=${platform} chars=${plain.length} messages=${messages.length}`
                    + ` ms=${now() - started}`,
            );
            return messages;
        } catch (error) {
            console.warn('[chat-ask] the meeting assistant did not answer:', error);
            return reply('I could not get an answer from the meeting assistant just now.');
        } finally {
            busy = false;
        }
    }

    return { answer };
}

export type ChatAnswerer = ReturnType<typeof createChatAnswerer>;

const meetingAnswerer: ChatAnswerer = createChatAnswerer();

/**
 * Answer a chat question in the background and post the reply with `send`.
 * Returns at once; errors are logged, never thrown into the chat handler.
 */
export function startChatAnswer(
    parsed: ChatQuestion,
    sender: string | null | undefined,
    platform: ChatPlatform,
    send: (messages: string[]) => Promise<void>,
    answerer: ChatAnswerer = meetingAnswerer,
): Promise<void> {
    console.log(`[chat-ask] question platform=${platform} explicit=${parsed.explicit} chars=${parsed.question.length}`);
    return answerer
        .answer(parsed, sender, platform)
        .then((messages) => (messages.length ? send(messages) : undefined))
        .catch((error) => console.warn('[chat-ask] could not post the answer:', error));
}
